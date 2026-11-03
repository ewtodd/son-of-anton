// Package signalfake is a fake signal-cli HTTP daemon for tests: the health
// check, the SSE event stream and the JSON-RPC endpoint, as
// gateway/platforms/signal.py uses them. Tests push envelopes onto the
// stream and read back the RPC calls made.
package signalfake

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

// Call is one JSON-RPC call received.
type Call struct {
	Method string
	Params map[string]any
	TS     int64 // a send's timestamp
}

// Server is the fake daemon.
type Server struct {
	URL string

	mu          sync.Mutex
	calls       []Call
	streams     map[chan []byte]bool
	pending     [][]byte // pushed while no stream was connected
	connects    int
	attachments map[string]string // id -> the result, as getAttachment returns it
	wrapped     map[string]bool
	nextTS      int64
	sendFails   bool
	checkStatus int
	account     string // the last event stream's account
}

// New starts a fake daemon, closed when the test ends.
func New(t *testing.T) *Server {
	t.Helper()
	s := &Server{streams: map[chan []byte]bool{}, attachments: map[string]string{}, wrapped: map[string]bool{},
		nextTS: 1_760_000_000_000, checkStatus: http.StatusOK}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/check", func(w http.ResponseWriter, _ *http.Request) {
		s.mu.Lock()
		code := s.checkStatus
		s.mu.Unlock()
		w.WriteHeader(code)
	})
	mux.HandleFunc("/api/v1/events", s.events)
	mux.HandleFunc("/api/v1/rpc", s.rpc)
	srv := httptest.NewServer(mux)
	t.Cleanup(func() {
		s.DropStreams()
		srv.Close()
	})
	s.URL = srv.URL
	return s
}

func (s *Server) events(w http.ResponseWriter, r *http.Request) {
	if r.URL.Query().Get("account") == "" {
		http.Error(w, "no account", http.StatusBadRequest)
		return
	}
	fl, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "no flush", http.StatusInternalServerError)
		return
	}
	ch := make(chan []byte, 256)
	s.mu.Lock()
	s.streams[ch] = true
	s.connects++
	s.account = r.URL.Query().Get("account")
	backlog := s.pending
	s.pending = nil
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		delete(s.streams, ch)
		s.mu.Unlock()
	}()
	w.Header().Set("Content-Type", "text/event-stream")
	w.WriteHeader(http.StatusOK)
	_, _ = io.WriteString(w, ":\n")
	fl.Flush()
	for _, b := range backlog {
		_, _ = fmt.Fprintf(w, "event:receive\ndata:%s\n\n", b)
	}
	fl.Flush()
	for {
		select {
		case <-r.Context().Done():
			return
		case b, ok := <-ch:
			if !ok {
				return
			}
			_, _ = fmt.Fprintf(w, "event:receive\ndata:%s\n\n", b)
			fl.Flush()
		}
	}
}

func (s *Server) rpc(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Method string         `json:"method"`
		Params map[string]any `json:"params"`
		ID     any            `json:"id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	s.mu.Lock()
	s.calls = append(s.calls, Call{Method: req.Method, Params: req.Params})
	var result any
	var rpcErr any
	switch req.Method {
	case "send":
		if s.sendFails {
			rpcErr = map[string]any{"code": -1, "message": "send failed"}
			break
		}
		s.nextTS++
		s.calls[len(s.calls)-1].TS = s.nextTS
		result = map[string]any{"timestamp": s.nextTS, "results": []any{map[string]any{"type": "SUCCESS"}}}
	case "getAttachment":
		id, _ := req.Params["id"].(string)
		data, ok := s.attachments[id]
		switch {
		case !ok:
			rpcErr = map[string]any{"code": -1, "message": "no attachment " + id}
		case s.wrapped[id]:
			result = map[string]any{"data": data}
		default:
			result = data
		}
	default:
		result = map[string]any{}
	}
	s.mu.Unlock()
	out := map[string]any{"jsonrpc": "2.0", "id": req.ID}
	if rpcErr != nil {
		out["error"] = rpcErr
	} else {
		out["result"] = result
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(out)
}

// Push sends one envelope (any JSON value) as a stream event, to the
// connected streams, or to the next one that connects.
func (s *Server) Push(envelope any) {
	b, err := json.Marshal(envelope)
	if err != nil {
		panic(err)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.streams) == 0 {
		s.pending = append(s.pending, b)
		return
	}
	for ch := range s.streams {
		ch <- b
	}
}

// DropStreams closes every open event stream (the client must reconnect).
func (s *Server) DropStreams() {
	s.mu.Lock()
	defer s.mu.Unlock()
	for ch := range s.streams {
		close(ch)
		delete(s.streams, ch)
	}
}

// Connects is how many times a client opened the event stream.
func (s *Server) Connects() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.connects
}

// SetAttachment makes getAttachment return data in base64, bare or wrapped
// in {"data": ...}.
func (s *Server) SetAttachment(id string, data []byte, wrapped bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.attachments[id] = base64.StdEncoding.EncodeToString(data)
	s.wrapped[id] = wrapped
}

// SetSendFails makes every send answer with an error.
func (s *Server) SetSendFails(fail bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sendFails = fail
}

// SetCheckStatus sets the health check's HTTP status.
func (s *Server) SetCheckStatus(code int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.checkStatus = code
}

// Calls returns the params of every call of method, in order ("" for all).
func (s *Server) Calls(method string) []map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []map[string]any
	for _, c := range s.calls {
		if method == "" || c.Method == method {
			out = append(out, c.Params)
		}
	}
	return out
}

// Sent is one message the client sent.
type Sent struct {
	To     string // "group:<id>" or the recipient
	Text   string
	Styles []string
	TS     int64 // its timestamp (what an echo of it carries)
}

// Sends returns the text messages sent successfully, in order.
func (s *Server) Sends() []Sent {
	s.mu.Lock()
	calls := append([]Call(nil), s.calls...)
	s.mu.Unlock()
	var out []Sent
	for _, c := range calls {
		if c.Method != "send" || c.TS == 0 {
			continue
		}
		p := c.Params
		m := Sent{TS: c.TS}
		if g, ok := p["groupId"].(string); ok {
			m.To = "group:" + g
		} else if r, ok := p["recipient"].([]any); ok && len(r) > 0 {
			m.To, _ = r[0].(string)
		}
		m.Text, _ = p["message"].(string)
		if st, ok := p["textStyle"].(string); ok {
			m.Styles = []string{st}
		}
		if sts, ok := p["textStyles"].([]any); ok {
			for _, x := range sts {
				if v, ok := x.(string); ok {
					m.Styles = append(m.Styles, v)
				}
			}
		}
		out = append(out, m)
	}
	return out
}

// WaitSend waits until a sent message satisfies pred, and returns it.
func (s *Server) WaitSend(t *testing.T, what string, pred func(Sent) bool) Sent {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		for _, m := range s.Sends() {
			if pred(m) {
				return m
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("no send %s; sent: %+v", what, s.Sends())
	return Sent{}
}

// WaitConnected waits until a client has the event stream open.
func (s *Server) WaitConnected(t *testing.T) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		s.mu.Lock()
		n := len(s.streams)
		s.mu.Unlock()
		if n > 0 {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("no client connected to the event stream")
}

// ---- envelopes, as signal-cli sends them ----

// GroupMessage is a group message from another member's phone.
func GroupMessage(sender, group, text string, ts int64) map[string]any {
	return map[string]any{"envelope": map[string]any{
		"source": sender, "sourceNumber": sender, "sourceName": "Member", "timestamp": ts,
		"dataMessage": map[string]any{"timestamp": ts, "message": text, "groupInfo": map[string]any{"groupId": group, "groupName": "The Group"}},
	}}
}

// OwnerInGroup is the account's owner writing in a group from their phone:
// signal-cli, a linked device of that account, sees a sync message.
func OwnerInGroup(account, group, text string, ts int64) map[string]any {
	return map[string]any{"envelope": map[string]any{
		"source": account, "sourceNumber": account, "timestamp": ts,
		"syncMessage": map[string]any{"sentMessage": map[string]any{
			"timestamp": ts, "message": text, "groupInfo": map[string]any{"groupId": group, "groupName": "The Group"},
		}},
	}}
}

// NoteToSelf is the owner's Note to Self.
func NoteToSelf(account, text string, ts int64) map[string]any {
	return map[string]any{"envelope": map[string]any{
		"source": account, "sourceNumber": account, "timestamp": ts,
		"syncMessage": map[string]any{"sentMessage": map[string]any{
			"timestamp": ts, "message": text, "destination": account, "destinationNumber": account,
		}},
	}}
}

// DM is a direct message from sender.
func DM(sender, text string, ts int64) map[string]any {
	return map[string]any{"envelope": map[string]any{
		"source": sender, "sourceNumber": sender, "timestamp": ts,
		"dataMessage": map[string]any{"timestamp": ts, "message": text},
	}}
}

// StreamAccount is the account the last event stream asked for.
func (s *Server) StreamAccount() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.account
}
