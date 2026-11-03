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
