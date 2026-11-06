package store_test

import (
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// Phase 4a: a subagent is a child session (parent_id, the agent type); the
// parent's children list in creation order, and session lists stay top-level.
func TestChildSessions(t *testing.T) {
	st := newStore(t)
	parent, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	if _, err := st.AppendMessages(ctx, parent.ID, store.Message{Role: "user", Content: "go"}); err != nil {
		t.Fatal(err)
	}
	a, _ := st.CreateSession(ctx, store.Session{Kind: "child", ParentID: parent.ID, Agent: "explore", Title: "Find it (@explore subagent)"})
	b, _ := st.CreateSession(ctx, store.Session{Kind: "child", ParentID: parent.ID, Agent: "general"})
	if got, _ := st.Session(ctx, a.ID); got.Agent != "explore" || got.ParentID != parent.ID {
		t.Fatalf("child = %+v", got)
	}
	kids, err := st.Children(ctx, parent.ID)
	if err != nil || len(kids) != 2 || kids[0].ID != a.ID || kids[1].ID != b.ID {
		t.Fatalf("Children = %+v, %v", kids, err)
	}
	if list, _ := st.ListSessions(ctx, []string{"interactive", "child"}, 10); len(list) != 1 {
		t.Fatalf("session lists hold only top-level sessions: %+v", list)
	}
}

// A subagent's run is never a search hit (the Python hid "subagent"
// sessions the same way): its answer is already the parent's tool result.
func TestChildSessionsAreNeverSearchHits(t *testing.T) {
	st := newStore(t)
	parent, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	child, _ := st.CreateSession(ctx, store.Session{Kind: "child", ParentID: parent.ID, Agent: "explore"})
	_, _ = st.AppendMessages(ctx, parent.ID, store.Message{Role: "user", Content: "where is the wombat ledger"})
	_, _ = st.AppendMessages(ctx, child.ID, store.Message{Role: "user", Content: "find the wombat ledger"},
		store.Message{Role: "assistant", Content: "the wombat ledger is in /srv"})
	for _, per := range []bool{false, true} {
		hits, err := st.SearchMessages(ctx, store.MessageQuery{Query: "wombat", Caller: "common", PerSession: per})
		if err != nil || len(hits) != 1 || hits[0].SessionID != parent.ID {
			t.Fatalf("PerSession %v: hits = %+v, %v; only the parent's row", per, hits, err)
		}
	}
}
