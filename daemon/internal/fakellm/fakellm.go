// Package fakellm is a scripted OpenAI-compatible chat-completions server for
// tests. Each request consumes the next scripted Turn; a request past the end
// of the script fails the test. Responses are always streamed (SSE), the way
// the daemon talks to maitred, and every request body is recorded byte for
// byte so tests can assert prompt-cache invariants on the exact wire bytes.
//
// It deliberately has no dependency on the daemon's own packages.
package fakellm

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sort"
	"sync"
	"testing"
	"time"
)

// ToolCall is a tool call the scripted assistant makes.
type ToolCall struct {
	ID        string
	Name      string
	Arguments string
	// ArgumentsFunc, when set, gives the arguments at the moment the turn is
	// sent (for a value the test learns only while running, like a new id).
	ArgumentsFunc func() string
}

// Turn scripts one response.
type Turn struct {
	Content   string
	Reasoning string
	ToolCalls []ToolCall

	// Status other than 0 or 200 answers with that status and Body, unstreamed.
	Status     int
	Body       string
	RetryAfter string

	// ChunkRunes splits Reasoning and Content into deltas of this many runes.
	// 0 sends each as a single delta.
	ChunkRunes int

	PromptTokens     int
	CompletionTokens int
	ReasoningTokens  int
	CachedTokens     int

	// Hang never answers; the handler returns when the client goes away.
	Hang bool
	// FailMidStream sends the first content delta, then ends the response
	// without a finish_reason, usage or [DONE].
	FailMidStream bool
	// StallAfterFirst sends the first content delta, then goes silent until
	// the client gives up (idle-timeout tests).
	StallAfterFirst bool
	// When, if set, makes this turn answer only a request whose body contains
	// it (the first such request, in any order); turns without When answer the
	// other requests in order. Concurrent sessions each take their own turns.
	When string
	// ServedModel, when set, is sent as the x-maitred-model header (maitred
	// naming the model that answered).
	ServedModel string
	// Release, when non-nil, sends the first content delta and then waits
	// until the channel is closed (or the client goes away) before sending
	// the rest: a turn the test holds open mid-stream.
	Release chan struct{}
	// LateReasoning, when set, is sent as a reasoning delta right after the
	// first content delta: a server releasing its reasoning's tail late.
	LateReasoning string
}

// Server is a running fake. URL is the base URL, ending in /v1.
type Server struct {
	URL string

	t             testing.TB
	mu            sync.Mutex
	turns         []Turn
	used          []bool // turns already answered
	bodies        [][]byte
	headers       []http.Header
	models        []string
	meta          map[string]map[string]any
	modelRequests int
	modelsStatus  int           // when set, /v1/models answers with this status
	modelsDelay   time.Duration // when set, /v1/models sleeps this long first
	mux           *http.ServeMux

	embedStatus int        // when set, /v1/embeddings answers with this status
	embedCalls  [][]string // the inputs of every embeddings request

	rerankStatus int                             // when set, /v1/rerank answers with this status
	rerankDelay  time.Duration                   // when set, /v1/rerank sleeps this long first
	rerankScore  func(query, doc string) float64 // nil means RerankScore
	rerankCalls  []RerankCall
}

// New starts a fake that answers the given turns in order.
func New(t testing.TB, turns ...Turn) *Server {
	t.Helper()
	s := &Server{t: t, turns: turns}
	s.mux = http.NewServeMux()
	s.mux.HandleFunc("/v1/chat/completions", s.handle)
	s.mux.HandleFunc("/v1/models", s.handleModels)
	s.mux.HandleFunc("/v1/embeddings", s.handleEmbeddings)
	s.mux.HandleFunc("/v1/rerank", s.handleRerank)
	srv := httptest.NewServer(s.mux)
	t.Cleanup(srv.Close)
	s.URL = srv.URL + "/v1"
	return s
}

// Handler is the fake's http.Handler, for a test that serves it behind its
// own middleware.
func (s *Server) Handler() http.Handler { return s.mux }

// Count is the number of requests received so far.
func (s *Server) Count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.bodies)
}

// ModelRequests is the number of GET /v1/models listings served.
func (s *Server) ModelRequests() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.modelRequests
}

// Bodies returns a copy of every request body, in order.
func (s *Server) Bodies() [][]byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([][]byte, len(s.bodies))
	for i, b := range s.bodies {
		out[i] = append([]byte(nil), b...)
	}
	return out
}

// Headers returns the headers of every request, in order.
func (s *Server) Headers() []http.Header {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]http.Header, len(s.headers))
	copy(out, s.headers)
	return out
}

// Request decodes request i into a generic map.
func (s *Server) Request(i int) map[string]any {
	s.t.Helper()
	var m map[string]any
	if err := json.Unmarshal(s.body(i), &m); err != nil {
		s.t.Fatalf("fakellm: request %d is not JSON: %v", i, err)
	}
	return m
}

// Messages returns the raw bytes of each element of request i's "messages"
// array, exactly as the client serialized them.
func (s *Server) Messages(i int) []json.RawMessage {
	s.t.Helper()
	var req struct {
		Messages []json.RawMessage `json:"messages"`
	}
	if err := json.Unmarshal(s.body(i), &req); err != nil {
		s.t.Fatalf("fakellm: request %d is not JSON: %v", i, err)
	}
	return req.Messages
}

func (s *Server) body(i int) []byte {
	s.t.Helper()
	s.mu.Lock()
	defer s.mu.Unlock()
	if i < 0 || i >= len(s.bodies) {
		s.t.Fatalf("fakellm: no request %d (got %d)", i, len(s.bodies))
	}
	return s.bodies[i]
}

// streamCheck reads a chat request's body for the stream the daemon always
// asks for (owner, 2026-11-07). A body that would not parse is a body that
// never arrived as a request, so its refusal says that instead of posing as a
// plain non-streaming one — the refusal names the request number, the path,
// the body length and the JSON error. refused is "" for a request the fake
// answers, with model taken from the body ("" when the body names none).
func streamCheck(idx int, path string, body []byte) (model, refused string) {
	var req struct {
		Model  string `json:"model"`
		Stream bool   `json:"stream"`
	}
	jerr := json.Unmarshal(body, &req)
	if jerr != nil {
		return "", fmt.Sprintf("fakellm: request %d to %s (%d bytes) did not parse as a chat-completions request (%v); body: %.200q",
			idx, path, len(body), jerr, body)
	}
	if !req.Stream {
		return req.Model, fmt.Sprintf("fakellm: request %d to %s (%d bytes) did not ask for a stream; body: %.200q",
			idx, path, len(body), body)
	}
	return req.Model, ""
}

func (s *Server) handle(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	s.mu.Lock()
	idx := len(s.bodies)
	s.bodies = append(s.bodies, body)
	s.headers = append(s.headers, r.Header.Clone())
	var turn Turn
	scripted := false
	if len(s.used) < len(s.turns) {
		s.used = append(s.used, make([]bool, len(s.turns)-len(s.used))...)
	}
	// The first unused turn this request may take: one without When, or one
	// whose When appears in the request (concurrent sessions take their own).
	for i, t := range s.turns {
		if !s.used[i] && (t.When == "" || bytes.Contains(body, []byte(t.When))) {
			s.used[i], turn, scripted = true, t, true
			break
		}
	}
	s.mu.Unlock()

	if !scripted {
		s.t.Errorf("fakellm: unexpected request %d (only %d scripted)", idx+1, len(s.turns))
		http.Error(w, "no scripted turn", http.StatusInternalServerError)
		return
	}
	if turn.Hang {
		<-r.Context().Done()
		return
	}
	if turn.Status != 0 && turn.Status != http.StatusOK {
		if turn.RetryAfter != "" {
			w.Header().Set("Retry-After", turn.RetryAfter)
		}
		w.WriteHeader(turn.Status)
		_, _ = io.WriteString(w, turn.Body)
		return
	}

	// streamCheck reads what arrived (owner, 2026-11-07): a body that would
	// not parse is named as that, not as a plain non-streaming request —
	// the daemon asks for a stream on every request — with the path and the
	// length beside it. The fake accepts and refuses exactly what it did
	// before: an unparsed or stream-less request still fails with the same
	// status and body.
	model, refused := streamCheck(idx+1, r.URL.Path, body)
	if refused != "" {
		s.t.Error(refused)
		http.Error(w, `{"error":"stream required"}`, http.StatusBadRequest)
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	if turn.ServedModel != "" {
		w.Header().Set("X-Maitred-Model", turn.ServedModel)
	}
	flusher, _ := w.(http.Flusher)
	id := fmt.Sprintf("chatcmpl-%d", idx+1)
	send := func(v any) {
		b, _ := json.Marshal(v)
		fmt.Fprintf(w, "data: %s\n\n", b)
		if flusher != nil {
			flusher.Flush()
		}
	}
	chunk := func(delta map[string]any, finish any) map[string]any {
		return map[string]any{
			"id":     id,
			"object": "chat.completion.chunk",
			"model":  model,
			"choices": []any{map[string]any{
				"index": 0, "delta": delta, "finish_reason": finish,
			}},
		}
	}

	send(chunk(map[string]any{"role": "assistant"}, nil))
	for _, part := range split(turn.Reasoning, turn.ChunkRunes) {
		send(chunk(map[string]any{"reasoning_content": part}, nil))
	}
	for i, part := range split(turn.Content, turn.ChunkRunes) {
		send(chunk(map[string]any{"content": part}, nil))
		if turn.LateReasoning != "" && i == 0 {
			send(chunk(map[string]any{"reasoning_content": turn.LateReasoning}, nil))
		}
		if turn.FailMidStream && i == 0 {
			return
		}
		if turn.StallAfterFirst && i == 0 {
			<-r.Context().Done()
			return
		}
		if turn.Release != nil && i == 0 {
			select {
			case <-turn.Release:
			case <-r.Context().Done():
				return
			}
		}
	}
	for i, tc := range turn.ToolCalls {
		if tc.ArgumentsFunc != nil {
			tc.Arguments = tc.ArgumentsFunc()
		}
		send(chunk(map[string]any{"tool_calls": []any{map[string]any{
			"index": i, "id": tc.ID, "type": "function",
			"function": map[string]any{"name": tc.Name, "arguments": ""},
		}}}, nil))
		half := len(tc.Arguments) / 2
		for _, piece := range []string{tc.Arguments[:half], tc.Arguments[half:]} {
			if piece == "" {
				continue
			}
			send(chunk(map[string]any{"tool_calls": []any{map[string]any{
				"index": i, "function": map[string]any{"arguments": piece},
			}}}, nil))
		}
	}
	finish := "stop"
	if len(turn.ToolCalls) > 0 {
		finish = "tool_calls"
	}
	send(chunk(map[string]any{}, finish))
	send(map[string]any{
		"id": id, "object": "chat.completion.chunk", "model": model,
		"choices": []any{},
		"usage": map[string]any{
			"prompt_tokens":     turn.PromptTokens,
			"completion_tokens": turn.CompletionTokens,
			"total_tokens":      turn.PromptTokens + turn.CompletionTokens,
			"completion_tokens_details": map[string]any{
				"reasoning_tokens": turn.ReasoningTokens,
			},
			"prompt_tokens_details": map[string]any{
				"cached_tokens": turn.CachedTokens,
			},
		},
	})
	_, _ = io.WriteString(w, "data: [DONE]\n\n")
	if flusher != nil {
		flusher.Flush()
	}
}

func split(s string, n int) []string {
	if s == "" {
		return nil
	}
	if n <= 0 {
		return []string{s}
	}
	runes := []rune(s)
	var out []string
	for len(runes) > 0 {
		k := min(n, len(runes))
		out = append(out, string(runes[:k]))
		runes = runes[k:]
	}
	return out
}

// SetModels sets the ids GET /v1/models lists (none by default).
func (s *Server) SetModels(ids ...string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.models = append([]string(nil), ids...)
}

// SetModelMeta adds fields (context_length, reasoning_efforts, ...) to a listed
// model's /v1/models entry, as maitred reports them.
func (s *Server) SetModelMeta(id string, meta map[string]any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.meta == nil {
		s.meta = map[string]map[string]any{}
	}
	s.meta[id] = meta
}

// SetModelsStatus makes GET /v1/models fail with status (0 restores it).
func (s *Server) SetModelsStatus(status int) {
	s.mu.Lock()
	s.modelsStatus = status
	s.mu.Unlock()
}

// SetModelsDelay makes GET /v1/models sleep d before answering (0 restores it).
func (s *Server) SetModelsDelay(d time.Duration) {
	s.mu.Lock()
	s.modelsDelay = d
	s.mu.Unlock()
}

func (s *Server) handleModels(w http.ResponseWriter, _ *http.Request) {
	s.mu.Lock()
	s.modelRequests++
	status := s.modelsStatus
	delay := s.modelsDelay
	data := make([]any, 0, len(s.models))
	for _, id := range s.models {
		entry := map[string]any{"id": id, "object": "model"}
		for k, v := range s.meta[id] {
			entry[k] = v
		}
		data = append(data, entry)
	}
	s.mu.Unlock()
	if delay > 0 { // after the unlock: a slow listing must not block the rest
		time.Sleep(delay)
	}
	if status != 0 {
		http.Error(w, `{"error":"models unavailable"}`, status)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"object": "list", "data": data})
}

// EmbedDims is the size of the fake's embedding vectors.
const EmbedDims = 512

// WordVector is the fake's embedding: a bag of lowercased words hashed into
// EmbedDims buckets, so texts sharing words are close and unrelated ones are
// not. Tests can compute expected similarities with it.
func WordVector(text string) []float64 {
	v := make([]float64, EmbedDims)
	word := []rune{}
	flush := func() {
		if len(word) == 0 {
			return
		}
		h := uint32(2166136261)
		for _, r := range string(word) {
			h = (h ^ uint32(r)) * 16777619
		}
		v[h%EmbedDims]++
		word = word[:0]
	}
	for _, r := range text {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			word = append(word, r)
		case r >= 'A' && r <= 'Z':
			word = append(word, r+'a'-'A')
		default:
			flush()
		}
	}
	flush()
	return v
}

// SetEmbedStatus makes /v1/embeddings fail with status (0 restores it).
func (s *Server) SetEmbedStatus(status int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.embedStatus = status
}

// EmbedCalls returns the inputs of every embeddings request so far.
func (s *Server) EmbedCalls() [][]string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([][]string, len(s.embedCalls))
	copy(out, s.embedCalls)
	return out
}

func (s *Server) handleEmbeddings(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Model string   `json:"model"`
		Input []string `json:"input"`
	}
	body, _ := io.ReadAll(r.Body)
	if err := json.Unmarshal(body, &req); err != nil {
		var one struct {
			Input string `json:"input"`
		}
		if json.Unmarshal(body, &one) != nil {
			http.Error(w, `{"error":"bad request"}`, http.StatusBadRequest)
			return
		}
		req.Input = []string{one.Input}
	}
	s.mu.Lock()
	status := s.embedStatus
	s.embedCalls = append(s.embedCalls, req.Input)
	s.mu.Unlock()
	if status != 0 {
		http.Error(w, `{"error":"embedder down"}`, status)
		return
	}
	data := make([]any, len(req.Input))
	for i, in := range req.Input {
		data[i] = map[string]any{"object": "embedding", "index": i, "embedding": WordVector(in)}
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"object": "list", "model": req.Model, "data": data})
}

// RerankCall is one /v1/rerank request as the fake received it.
type RerankCall struct {
	Model, Query string
	Documents    []string
	TopN         int
}

// RerankScore is the fake reranker's default score: the share of the query's
// distinct words that appear in the document, so a document holding every
// word of the query scores 1.
func RerankScore(query, doc string) float64 {
	words := func(text string) map[int]bool {
		m := map[int]bool{}
		for i, v := range WordVector(text) {
			if v > 0 {
				m[i] = true
			}
		}
		return m
	}
	q, d := words(query), words(doc)
	if len(q) == 0 {
		return 0
	}
	n := 0
	for w := range q {
		if d[w] {
			n++
		}
	}
	return float64(n) / float64(len(q))
}

// SetRerankScore replaces the fake reranker's scoring (nil restores RerankScore).
func (s *Server) SetRerankScore(f func(query, doc string) float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.rerankScore = f
}

// SetRerankStatus makes /v1/rerank fail with status (0 restores it).
func (s *Server) SetRerankStatus(status int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.rerankStatus = status
}

// SetRerankDelay makes /v1/rerank wait d before answering.
func (s *Server) SetRerankDelay(d time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.rerankDelay = d
}

// RerankCalls returns every rerank request so far.
func (s *Server) RerankCalls() []RerankCall {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]RerankCall, len(s.rerankCalls))
	copy(out, s.rerankCalls)
	return out
}

// handleRerank answers in the Cohere/Jina shape llama.cpp's server uses:
// results sorted by relevance_score, each with its document's index.
func (s *Server) handleRerank(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Model     string   `json:"model"`
		Query     string   `json:"query"`
		Documents []string `json:"documents"`
		TopN      int      `json:"top_n"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, `{"error":"bad request"}`, http.StatusBadRequest)
		return
	}
	s.mu.Lock()
	status, delay, score := s.rerankStatus, s.rerankDelay, s.rerankScore
	s.rerankCalls = append(s.rerankCalls, RerankCall{req.Model, req.Query, req.Documents, req.TopN})
	s.mu.Unlock()
	if delay > 0 {
		select {
		case <-time.After(delay):
		case <-r.Context().Done():
			return
		}
	}
	if status != 0 {
		http.Error(w, `{"error":"reranker down"}`, status)
		return
	}
	if score == nil {
		score = RerankScore
	}
	type result struct {
		Index int     `json:"index"`
		Score float64 `json:"relevance_score"`
	}
	results := make([]result, len(req.Documents))
	for i, d := range req.Documents {
		results[i] = result{i, score(req.Query, d)}
	}
	sort.SliceStable(results, func(i, j int) bool { return results[i].Score > results[j].Score })
	if req.TopN > 0 && req.TopN < len(results) {
		results = results[:req.TopN]
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"model": req.Model, "results": results})
}
