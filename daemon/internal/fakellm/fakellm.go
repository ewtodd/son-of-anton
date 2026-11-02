// Package fakellm is a scripted OpenAI-compatible chat-completions server for
// tests. Each request consumes the next scripted Turn; a request past the end
// of the script fails the test. Responses are always streamed (SSE), the way
// the daemon talks to maitred, and every request body is recorded byte for
// byte so tests can assert prompt-cache invariants on the exact wire bytes.
//
// It deliberately has no dependency on the daemon's own packages.
package fakellm

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
)

// ToolCall is a tool call the scripted assistant makes.
type ToolCall struct {
	ID        string
	Name      string
	Arguments string
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
}

// Server is a running fake. URL is the base URL, ending in /v1.
type Server struct {
	URL string

	t       testing.TB
	mu      sync.Mutex
	turns   []Turn
	bodies  [][]byte
	headers []http.Header
}

// New starts a fake that answers the given turns in order.
func New(t testing.TB, turns ...Turn) *Server {
	t.Helper()
	s := &Server{t: t, turns: turns}
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/chat/completions", s.handle)
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	s.URL = srv.URL + "/v1"
	return s
}

// Count is the number of requests received so far.
func (s *Server) Count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.bodies)
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

func (s *Server) handle(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	s.mu.Lock()
	idx := len(s.bodies)
	s.bodies = append(s.bodies, body)
	s.headers = append(s.headers, r.Header.Clone())
	var turn Turn
	scripted := idx < len(s.turns)
	if scripted {
		turn = s.turns[idx]
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

	var req struct {
		Model  string `json:"model"`
		Stream bool   `json:"stream"`
	}
	_ = json.Unmarshal(body, &req)
	if !req.Stream {
		s.t.Errorf("fakellm: request %d did not ask for a stream", idx+1)
		http.Error(w, `{"error":"stream required"}`, http.StatusBadRequest)
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
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
			"model":  req.Model,
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
		if turn.FailMidStream && i == 0 {
			return
		}
		if turn.StallAfterFirst && i == 0 {
			<-r.Context().Done()
			return
		}
	}
	for i, tc := range turn.ToolCalls {
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
		"id": id, "object": "chat.completion.chunk", "model": req.Model,
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
