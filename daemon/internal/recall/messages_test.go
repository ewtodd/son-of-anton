package recall_test

// Phase 3c: conversation text is indexed for session_search only: user rows
// and final assistant replies, never tool calls, tool output or reasoning;
// stamped with the session's scope; never recalled into a turn.

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/recall"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func TestConversationRowsAreIndexedForSearch(t *testing.T) {
	fake := fakellm.New(t)
	home := t.TempDir()
	x, err := recall.Open(home, recall.Config{BaseURL: fake.URL, Model: "embed", TopK: 5, MinScore: 0.4,
		SearchTimeout: 3 * time.Second, FailureCooldown: time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	defer x.Close()
	rows := []store.Message{
		{Seq: 1, Role: "user", Content: "zebra router channel question"},
		{Seq: 2, Role: "assistant", Reasoning: "think about wombats", ToolCalls: json.RawMessage(`[{"id":"c1","type":"function","function":{"name":"terminal","arguments":"{\"command\":\"wombat\"}"}}]`)},
		{Seq: 3, Role: "tool", Content: "wombat tool output", ToolCallID: "c1", ToolName: "terminal"},
		{Seq: 4, Role: "assistant", Content: "set the zebra router channel to 6"},
	}
	if err := x.IndexMessages(ctx, "S1", "common", rows); err != nil {
		t.Fatal(err)
	}
	if err := x.IndexMessages(ctx, "G1", "gateway", []store.Message{{Seq: 1, Role: "user", Content: "zebra router channel private"}}); err != nil {
		t.Fatal(err)
	}
	for _, call := range fake.EmbedCalls() {
		for _, in := range call {
			if strings.Contains(in, "wombat") {
				t.Fatalf("embedded %q; tool calls, tool output and reasoning are never indexed", in)
			}
		}
	}
	hits, err := x.SearchMessages(ctx, "common", "zebra router channel", 5)
	if err != nil {
		t.Fatal(err)
	}
	seqs := map[int64]bool{}
	for _, h := range hits {
		if h.SessionID != "S1" {
			t.Fatalf("a common caller found %s: %+v", h.SessionID, hits)
		}
		seqs[h.Seq] = true
	}
	if !seqs[1] || !seqs[4] || len(seqs) != 2 {
		t.Fatalf("hits = %+v; the user row and the final reply", hits)
	}
	if hits, _ := x.SearchMessages(ctx, "gateway", "zebra router channel", 5); len(hits) != 3 {
		t.Fatalf("a gateway caller searches every session: %+v", hits)
	}
	calls := len(fake.EmbedCalls())
	if err := x.IndexMessages(ctx, "S1", "common", rows); err != nil {
		t.Fatal(err)
	}
	if len(fake.EmbedCalls()) != calls {
		t.Fatal("rows already indexed are not embedded again")
	}
	if got := x.Recall(ctx, "common", "zebra router channel"); got != "" {
		t.Fatalf("conversation text is never recalled into a turn: %q", got)
	}
	if hits, _ := x.SearchMessages(ctx, "common", "zebra router channel", 5); len(hits) != 2 {
		t.Fatalf("a facts-and-notes refresh keeps the conversation items: %+v", hits)
	}
}
