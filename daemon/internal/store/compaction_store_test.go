package store_test

// Phase 3a: compactions are recorded on the session (the rows stay), and a
// title never gives way to one from a lower source.

import (
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func TestCompactionsAreRecordedInOrderAndKeepTheRows(t *testing.T) {
	st := newStore(t)
	s, _ := st.CreateSession(ctx, store.Session{Kind: "interactive", PromptHash: "p1"})
	for _, role := range []string{"user", "assistant", "user", "assistant"} {
		if _, err := st.AppendMessages(ctx, s.ID, store.Message{Role: role, Content: role}); err != nil {
			t.Fatal(err)
		}
	}
	if err := st.AddCompaction(ctx, s.ID, store.Compaction{ThroughSeq: 2, Summary: "one", BeforeTokens: 900}, "p2"); err != nil {
		t.Fatal(err)
	}
	if err := st.AddCompaction(ctx, s.ID, store.Compaction{ThroughSeq: 4, Summary: "two"}, "p3"); err != nil {
		t.Fatal(err)
	}
	list, err := st.Compactions(ctx, s.ID)
	if err != nil || len(list) != 2 || list[0].ThroughSeq != 2 || list[0].Summary != "one" || list[0].BeforeTokens != 900 ||
		list[1].ThroughSeq != 4 || list[1].Summary != "two" || list[0].Created.IsZero() {
		t.Fatalf("Compactions = %+v, %v; oldest first", list, err)
	}
	if got, _ := st.Session(ctx, s.ID); got.PromptHash != "p3" {
		t.Fatalf("prompt hash = %q; a compaction stores the rebuilt prompt", got.PromptHash)
	}
	if rows, _ := st.Messages(ctx, s.ID); len(rows) != 4 {
		t.Fatalf("rows = %d; compaction never deletes or rewrites rows", len(rows))
	}
	if err := st.AddCompaction(ctx, s.ID, store.Compaction{ThroughSeq: 3, Summary: "back"}, "p4"); err == nil {
		t.Fatal("a compaction must cover more than the previous one")
	}
	if err := st.AddCompaction(ctx, s.ID, store.Compaction{ThroughSeq: 9, Summary: "past the end"}, "p4"); err == nil {
		t.Fatal("a compaction cannot cover rows that do not exist")
	}
	if err := st.AddCompaction(ctx, "20260101_000000_000000", store.Compaction{ThroughSeq: 1, Summary: "x"}, "p"); err == nil {
		t.Fatal("unknown session")
	}
}

func TestATitleNeverLosesToALowerSource(t *testing.T) {
	st := newStore(t)
	s, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	for _, step := range []struct {
		title, source string
		changed       bool
		want          string
	}{
		{"fix the login button…", "derived", true, "fix the login button…"},
		{"Fix login button on mobile", "llm", true, "Fix login button on mobile"},
		{"derived again", "derived", false, "Fix login button on mobile"},
		{"Login work", "user", true, "Login work"},
		{"An llm title", "llm", false, "Login work"},
		{"Renamed", "user", true, "Renamed"},
		{"User had the last word", "user", true, "User had the last word"},
	} {
		changed, err := st.SetTitle(ctx, s.ID, step.title, step.source)
		got, _ := st.Session(ctx, s.ID)
		if err != nil || changed != step.changed || got.Title != step.want || (changed && got.TitleSource != step.source) {
			t.Fatalf("SetTitle(%q, %s) = %v, %v; session %q (%s)", step.title, step.source, changed, err, got.Title, got.TitleSource)
		}
	}
	// A title the retired model-driven retitle set lives on as a user rename
	// (title_store_test.go); the store takes only these three sources now.
	if _, err := st.SetTitle(ctx, s.ID, "x", "retitle"); err == nil {
		t.Fatal("retitle is gone: the source is derived, llm or user")
	}
	if _, err := st.SetTitle(ctx, s.ID, "x", "guess"); err == nil {
		t.Fatal("the source is derived, llm or user")
	}
}
