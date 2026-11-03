package store_test

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// Phase 4b: a session's usage and context are stored, so they outlive the
// loaded session (a resumed session, a finished child).
func TestUsageIsStoredOnTheSession(t *testing.T) {
	st := newStore(t)
	s, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	if got, _ := st.Session(ctx, s.ID); got.Usage != (store.Usage{}) || got.ContextTokens != 0 {
		t.Fatalf("a new session = %+v, %d", got.Usage, got.ContextTokens)
	}
	if err := st.AddUsage(ctx, s.ID, store.Usage{Input: 100, Output: 20, Cached: 50, Reasoning: 5, Calls: 2}, 120); err != nil {
		t.Fatal(err)
	}
	if err := st.AddUsage(ctx, s.ID, store.Usage{Input: 10, Output: 2, Calls: 1}, 0); err != nil {
		t.Fatal(err)
	}
	got, err := st.Session(ctx, s.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Usage != (store.Usage{Input: 110, Output: 22, Cached: 50, Reasoning: 5, Calls: 3}) || got.ContextTokens != 120 {
		t.Fatalf("usage = %+v, context %d; totals add up, and a 0 context keeps the last one", got.Usage, got.ContextTokens)
	}
	if err := st.AddUsage(ctx, "20260101_000000_000000", store.Usage{Calls: 1}, 1); err == nil {
		t.Fatal("an unknown session is an error")
	}
}

// RunStats is a child's tool calls and how long its transcript spans.
func TestRunStats(t *testing.T) {
	st := newStore(t)
	s, _ := st.CreateSession(ctx, store.Session{Kind: "child"})
	if rs, err := st.RunStats(ctx, s.ID); err != nil || rs != (store.RunStats{}) {
		t.Fatalf("empty: %+v, %v", rs, err)
	}
	calls := json.RawMessage(`[{"id":"a","type":"function","function":{"name":"terminal","arguments":"{}"}},{"id":"b","type":"function","function":{"name":"read_file","arguments":"{}"}}]`)
	_, _ = st.AppendMessages(ctx, s.ID, store.Message{Role: "user", Content: "look"},
		store.Message{Role: "assistant", ToolCalls: calls})
	time.Sleep(60 * time.Millisecond)
	_, _ = st.AppendMessages(ctx, s.ID, store.Message{Role: "tool", ToolCallID: "a", ToolName: "terminal", Content: "x"},
		store.Message{Role: "tool", ToolCallID: "b", ToolName: "read_file", Content: "y"},
		store.Message{Role: "assistant", Content: "found it"})
	rs, err := st.RunStats(ctx, s.ID)
	if err != nil || rs.ToolCalls != 2 || rs.Duration < 60*time.Millisecond || rs.Duration > 5*time.Second {
		t.Fatalf("RunStats = %+v, %v", rs, err)
	}
}
