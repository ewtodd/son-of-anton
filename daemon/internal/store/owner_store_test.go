package store_test

import (
	"database/sql"
	"path/filepath"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func TestAChatTakesOnAndLetsGoOfASession(t *testing.T) {
	st := newStore(t)
	started, _ := st.CreateSession(ctx, store.Session{Kind: "interactive", Owner: "signal:g"})
	desk, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	for _, id := range []string{started.ID, desk.ID} {
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
	if ids := queued(); !equal(ids, []string{started.ID}) {
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
	if ids := queued(); !equal(ids, []string{started.ID}) {
		t.Fatalf("letting go stops it: %v", ids)
	}
}

// Sessions stored while a worker was a kind of its own become plain sessions
// that keep their owner, and nothing else about any session changes.
func TestWorkersBecomeSessionsWithAnOwner(t *testing.T) {
	path := filepath.Join(t.TempDir(), "soa-d.db")
	st := open(t, path)
	worker, _ := st.CreateSession(ctx, store.Session{Kind: "interactive", Owner: "signal:g", Title: "A worker"})
	chat, _ := st.CreateSession(ctx, store.Session{Kind: "chat", Scope: "gateway", Chat: "signal:g"})
	st.Close()

	db, err := sql.Open("sqlite", "file:"+path)
	if err != nil {
		t.Fatal(err)
	}
	for _, q := range []string{`UPDATE sessions SET kind = 'worker' WHERE id = '` + worker.ID + `'`, `UPDATE schema_version SET version = 16`} {
		if _, err := db.Exec(q); err != nil {
			t.Fatal(err)
		}
	}
	db.Close()

	st = open(t, path)
	if v, err := st.SchemaVersion(); err != nil || v != 17 {
		t.Fatalf("schema version %d, %v", v, err)
	}
	got, err := st.Session(ctx, worker.ID)
	if err != nil || got.Kind != "interactive" || got.Owner != "signal:g" || got.Title != "A worker" {
		t.Fatalf("the worker = %+v, %v", got, err)
	}
	if got, err := st.Session(ctx, chat.ID); err != nil || got.Kind != "chat" {
		t.Fatalf("the chat = %+v, %v", got, err)
	}
}
