package store_test

// Phase 3c: sessions carry a scope and a chat stamp, and message search
// applies the wall in the query itself, so walled rows never crowd out the
// readable ones (the Python learned that from 350 walled rows burying one).

import (
	"fmt"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func TestSessionsHaveAScopeAndAChat(t *testing.T) {
	st := newStore(t)
	plain, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	if got, _ := st.Session(ctx, plain.ID); got.Scope != "common" || got.Chat != "" {
		t.Fatalf("default = %q / %q; a session is common with no chat unless stamped", got.Scope, got.Chat)
	}
	chat, _ := st.CreateSession(ctx, store.Session{Kind: "chat", Scope: "gateway", Chat: "signal:group-1"})
	if got, _ := st.Session(ctx, chat.ID); got.Scope != "gateway" || got.Chat != "signal:group-1" {
		t.Fatalf("stamped = %q / %q", got.Scope, got.Chat)
	}
}

func TestReadableIsTheWall(t *testing.T) {
	common := store.Session{Scope: "common"}
	gateway := store.Session{Scope: "gateway", Chat: "signal:x"}
	chatty := store.Session{Scope: "common", Chat: "signal:y"}
	for _, c := range []struct {
		caller string
		s      store.Session
		want   bool
	}{
		{"common", common, true}, {"common", gateway, false}, {"common", chatty, false},
		{"gateway", common, true}, {"gateway", gateway, true}, {"gateway", chatty, true},
		{"", common, true}, {"", gateway, false}, // an unknown caller is the terminal
	} {
		if got := store.Readable(c.caller, c.s); got != c.want {
			t.Errorf("Readable(%q, %+v) = %v, want %v", c.caller, c.s, got, c.want)
		}
	}
}

func TestSearchHonorsTheWallInTheQuery(t *testing.T) {
	st := newStore(t)
	walled, _ := st.CreateSession(ctx, store.Session{Kind: "chat", Scope: "gateway", Chat: "signal:g"})
	for i := range 350 {
		if _, err := st.AppendMessages(ctx, walled.ID, store.Message{Role: "user", Content: fmt.Sprintf("zebra walled %d", i)}); err != nil {
			t.Fatal(err)
		}
	}
	open, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	if _, err := st.AppendMessages(ctx, open.ID, store.Message{Role: "user", Content: "zebra readable"}); err != nil {
		t.Fatal(err)
	}
	hits, err := st.SearchMessages(ctx, store.MessageQuery{Query: "zebra", Caller: "common", Roles: []string{"user", "assistant"}, Limit: 10})
	if err != nil || len(hits) != 1 || hits[0].SessionID != open.ID || hits[0].Role != "user" || hits[0].TS.IsZero() {
		t.Fatalf("common search = %+v, %v; the one readable hit, never a walled one", hits, err)
	}
	if hits, _ := st.SearchMessages(ctx, store.MessageQuery{Query: "zebra", Caller: "gateway", Roles: []string{"user"}, Limit: 500}); len(hits) != 351 {
		t.Fatalf("a gateway caller reads everything: %d hits", len(hits))
	}
}

func TestSearchFiltersRolesSortsExcludesAndSurvivesBadSyntax(t *testing.T) {
	st := newStore(t)
	a, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	b, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	_, _ = st.AppendMessages(ctx, a.ID,
		store.Message{Role: "user", Content: "first mention of the quokka"},
		store.Message{Role: "assistant", Content: "noted"},
		store.Message{Role: "tool", Content: "quokka tool output", ToolCallID: "c1", ToolName: "terminal"})
	_, _ = st.AppendMessages(ctx, b.ID, store.Message{Role: "user", Content: "a later quokka"})
	q := store.MessageQuery{Query: "quokka", Caller: "common", Roles: []string{"user", "assistant"}, Limit: 10}
	if hits, _ := st.SearchMessages(ctx, q); len(hits) != 2 {
		t.Fatalf("tool rows are searched only when asked: %+v", hits)
	}
	q.Roles = []string{"user", "assistant", "tool"}
	if hits, _ := st.SearchMessages(ctx, q); len(hits) != 3 {
		t.Fatalf("with tool: %+v", hits)
	}
	q.Roles, q.Sort = []string{"user"}, "newest"
	if hits, _ := st.SearchMessages(ctx, q); len(hits) != 2 || hits[0].SessionID != b.ID {
		t.Fatalf("newest first: %+v", hits)
	}
	q.Sort = "oldest"
	if hits, _ := st.SearchMessages(ctx, q); hits[0].SessionID != a.ID {
		t.Fatalf("oldest first: %+v", hits)
	}
	q.Sort, q.Exclude = "", a.ID
	if hits, _ := st.SearchMessages(ctx, q); len(hits) != 1 || hits[0].SessionID != b.ID {
		t.Fatalf("Exclude leaves a session out: %+v", hits)
	}
	q.Exclude, q.Query = "", `"quokka`
	if hits, err := st.SearchMessages(ctx, q); err != nil || len(hits) != 2 {
		t.Fatalf("an FTS5 syntax error falls back to quoted terms: %+v, %v", hits, err)
	}
	q.Query = "quokka OR wombat"
	if hits, err := st.SearchMessages(ctx, q); err != nil || len(hits) != 2 {
		t.Fatalf("FTS5 expressions pass through: %+v, %v", hits, err)
	}
}

func TestListingByCallerAndScope(t *testing.T) {
	st := newStore(t)
	inter, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	chat, _ := st.CreateSession(ctx, store.Session{Kind: "chat", Scope: "gateway", Chat: "signal:g"})
	for _, id := range []string{inter.ID, chat.ID} {
		if _, err := st.AppendMessages(ctx, id, store.Message{Role: "user", Content: "started"}); err != nil {
			t.Fatal(err)
		}
	}
	if list, _ := st.ListReadable(ctx, "common", []string{"interactive", "chat"}, 10); len(list) != 1 {
		t.Fatalf("common caller lists %d sessions, want 1", len(list))
	}
	if list, _ := st.ListReadable(ctx, "gateway", []string{"interactive", "chat"}, 10); len(list) != 2 {
		t.Fatalf("gateway caller lists %d sessions, want 2", len(list))
	}
}
