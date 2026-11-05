package store_test

import (
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func TestAChatTakesOnAndLetsGoOfASession(t *testing.T) {
	st := newStore(t)
	worker, _ := st.CreateSession(ctx, store.Session{Kind: "worker"})
	desk, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	for _, id := range []string{worker.ID, desk.ID} {
		if _, err := st.QueuePush(ctx, id, "user", "next"); err != nil {
			t.Fatal(err)
		}
	}
	queued := func() []string {
		t.Helper()
		ids, err := st.QueuedWorkers(ctx)
		if err != nil {
			t.Fatal(err)
		}
		return ids
	}
	if ids := queued(); !equal(ids, []string{worker.ID}) {
		t.Fatalf("nobody runs a terminal's queue: %v", ids)
	}
	if err := st.SetOwner(ctx, desk.ID, "signal:g"); err != nil {
		t.Fatal(err)
	}
	if got, err := st.Session(ctx, desk.ID); err != nil || got.Owner != "signal:g" {
		t.Fatalf("the session = %+v, %v", got, err)
	}
	if ids := queued(); len(ids) != 2 {
		t.Fatalf("a chat runs the queue of a session it took on: %v", ids)
	}
	if err := st.SetOwner(ctx, desk.ID, ""); err != nil {
		t.Fatal(err)
	}
	if got, err := st.Session(ctx, desk.ID); err != nil || got.Owner != "" {
		t.Fatalf("the session = %+v, %v", got, err)
	}
	if ids := queued(); !equal(ids, []string{worker.ID}) {
		t.Fatalf("letting go stops it: %v", ids)
	}
}
