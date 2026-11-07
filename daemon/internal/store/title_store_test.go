package store_test

// The model-driven retitle is gone (06f55b97): /retitle is the TUI's plain
// rename, stored as source "user". Sessions the retired ask had named carry
// title_source "retitle", so Open migrates them: they keep their title, the
// compaction tip falls silent on them (it never tips over a user title) and
// a later llm title cannot take the name.

import (
	"database/sql"
	"path/filepath"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func TestARetitledRowBecomesUser(t *testing.T) {
	path := filepath.Join(t.TempDir(), "soa-d.db")
	st := open(t, path)
	s, err := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	if err != nil {
		t.Fatal(err)
	}
	st.Close()

	// Pretend the row was written by the retired retitle source, a version
	// behind, so the next Open applies the migration.
	db, err := sql.Open("sqlite", "file:"+path)
	if err != nil {
		t.Fatal(err)
	}
	for _, q := range []string{
		`UPDATE sessions SET title = 'Named by its model', title_source = 'retitle' WHERE id = '` + s.ID + `'`,
		`UPDATE schema_version SET version = 18`,
	} {
		if _, err := db.Exec(q); err != nil {
			t.Fatal(err)
		}
	}
	db.Close()

	st = open(t, path)
	if v, err := st.SchemaVersion(); err != nil || v != 19 {
		t.Fatalf("schema version %d, %v", v, err)
	}
	got, err := st.Session(ctx, s.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Title != "Named by its model" {
		t.Fatalf("title = %q; the migration must keep what the ask named", got.Title)
	}
	if got.TitleSource != "user" {
		t.Fatalf("title source = %q; a retired retitle becomes a user rename", got.TitleSource)
	}

	// An llm title must still not overwrite it.
	if changed, err := st.SetTitle(ctx, s.ID, "The model is back", "llm"); err != nil || changed {
		t.Fatalf("SetTitle(llm) = %v, %v; the migrated row is a user rename", changed, err)
	}
	if got, _ := st.Session(ctx, s.ID); got.Title != "Named by its model" {
		t.Fatalf("title = %q; the llm ask rewrote it", got.Title)
	}
}
