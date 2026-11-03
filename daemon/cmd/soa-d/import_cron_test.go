package main

// Review fix to phase 8a: a Python cron session is behind the wall when its
// job delivers to Signal, or when its job is gone (unknown fails closed).

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func TestCronSessionsTakeTheScopeOfWhereTheirJobDelivers(t *testing.T) {
	src := t.TempDir()
	db, err := sql.Open("sqlite", filepath.Join(src, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(pySchema); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"cron_aaaaaaaaaaaa_20261010_070035", "cron_bbbbbbbbbbbb_20261010_070035", "cron_cccccccccccc_20261010_070035", "cron_dddddddddddd_20261010_070035"} {
		if _, err := db.Exec(`INSERT INTO sessions (id, source, started_at) VALUES (?, 'cron', 1760000000)`, id); err != nil {
			t.Fatal(err)
		}
	}
	db.Close()
	if err := os.MkdirAll(filepath.Join(src, "cron"), 0o700); err != nil {
		t.Fatal(err)
	}
	jobs := `{"jobs": [
		{"id": "aaaaaaaaaaaa", "deliver": "origin", "origin": {"platform": "signal", "chat_id": "group:gW"}},
		{"id": "bbbbbbbbbbbb", "deliver": "local", "origin": null},
		{"id": "cccccccccccc", "deliver": "signal:+15550000000", "origin": null}
	], "updated_at": "2026-11-01T00:00:00+00:00"}`
	if err := os.WriteFile(filepath.Join(src, "cron", "jobs.json"), []byte(jobs), 0o600); err != nil {
		t.Fatal(err)
	}
	home := t.TempDir()
	if code, _, errOut := runIn(t, "import", "--from", src, "--home", home); code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	st, err := store.Open(filepath.Join(home, "soa-d.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	for id, want := range map[string]string{
		"cron_aaaaaaaaaaaa_20261010_070035": "gateway", // delivers to a Signal group
		"cron_bbbbbbbbbbbb_20261010_070035": "common",  // local
		"cron_cccccccccccc_20261010_070035": "gateway", // a Signal target
		"cron_dddddddddddd_20261010_070035": "gateway", // its job is gone
	} {
		s, err := st.Session(context.Background(), id)
		if err != nil || s.Scope != want {
			t.Errorf("%s: scope %q, want %q (%v)", id, s.Scope, want, err)
		}
	}
}
