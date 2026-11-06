package store_test

// A session that never got a turn is not a session yet: the listings must
// not show it (owner, 2026-11-06). A summary of one session by id still
// works, so a just-created session can be announced to its creator.

import (
	"errors"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func TestAZeroTurnSessionIsNotListed(t *testing.T) {
	st := newStore(t)
	empty, err := st.CreateSession(ctx, store.Session{Kind: "interactive", Source: "test", Cwd: "/w/empty"})
	if err != nil {
		t.Fatal(err)
	}
	with, err := st.CreateSession(ctx, store.Session{Kind: "interactive", Source: "test", Cwd: "/w/with"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.AppendMessages(ctx, with.ID, store.Message{Role: "user", Content: "hello"}); err != nil {
		t.Fatal(err)
	}

	list, err := st.ListSessions(ctx, []string{"interactive"}, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 1 || list[0].Session.ID != with.ID {
		t.Fatalf("ListSessions = %+v; only the session with a turn counts", list)
	}

	// One session by id still resolves, row or no rows: session.create
	// answers its caller with the fresh row's summary.
	got, err := st.SessionSummary(ctx, empty.ID)
	if err != nil {
		t.Fatalf("SessionSummary on a zero-turn session: %v", err)
	}
	if got.Session.ID != empty.ID {
		t.Fatalf("summary = %+v", got)
	}
	if _, err := st.SessionSummary(ctx, "no such one"); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("an unknown session = %v", err)
	}
}
