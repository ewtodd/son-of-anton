package store_test

import (
	"errors"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// Phase 10a: deleting a session removes it, its children and everything
// stored for them, search entries included, and nothing of another session.
func TestDeleteSessionsRemovesEverythingStoredForThem(t *testing.T) {
	st := newStore(t)
	gone, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	kid, _ := st.CreateSession(ctx, store.Session{Kind: "child", ParentID: gone.ID, Agent: "explore"})
	kept, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	for _, id := range []string{gone.ID, kid.ID, kept.ID} {
		if _, err := st.AppendMessages(ctx, id, store.Message{Role: "user", Content: "the wombat ledger"}); err != nil {
			t.Fatal(err)
		}
	}
	ids, err := st.SessionTree(ctx, gone.ID)
	if err != nil || len(ids) != 2 || ids[0] != gone.ID || ids[1] != kid.ID {
		t.Fatalf("SessionTree = %v, %v", ids, err)
	}
	if err := st.DeleteSessions(ctx, ids); err != nil {
		t.Fatal(err)
	}
	for _, id := range ids {
		if _, err := st.Session(ctx, id); !errors.Is(err, store.ErrNotFound) {
			t.Fatalf("session %s after delete: %v", id, err)
		}
		if rows, _ := st.Messages(ctx, id); len(rows) != 0 {
			t.Fatalf("messages of %s remain", id)
		}
	}
	hits, err := st.SearchMessages(ctx, store.MessageQuery{Query: "wombat", Caller: "common"})
	if err != nil || len(hits) != 1 || hits[0].SessionID != kept.ID {
		t.Fatalf("search after delete = %+v, %v; only the kept session's row", hits, err)
	}
	if _, err := st.SessionTree(ctx, gone.ID); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("SessionTree of a deleted session: %v", err)
	}
}
