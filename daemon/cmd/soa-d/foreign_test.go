package main

// Phase 7e: soa-d import --from-claude / --from-codex / --from-opencode
// (daemon/phases/07e-foreign.md), on the fixtures of internal/foreign.

import (
	"context"
	"database/sql"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

var fixtures = filepath.Join("..", "..", "internal", "foreign", "testdata")

func TestImportFromClaudeCodexAndOpencode(t *testing.T) {
	home := t.TempDir()
	db := filepath.Join(fixtures, "opencode.db")
	for _, c := range []struct{ flag, from, out string }{
		{"--from-claude", filepath.Join(fixtures, "claude"), "imported 2 sessions (7 messages); 0 already present, 0 skipped\n"},
		{"--from-codex", filepath.Join(fixtures, "codex"), "imported 1 sessions (2 messages); 0 already present, 0 skipped\n"},
		{"--from-opencode", db, "imported 2 sessions (6 messages); 0 already present, 0 skipped\n"},
	} {
		code, out, errOut := runIn(t, "import", c.flag, c.from, "--home", home)
		if code != 0 || out != c.out {
			t.Fatalf("%s: exit %d, %q, %q", c.flag, code, out, errOut)
		}
	}
	st, err := store.Open(filepath.Join(home, "soa-d.db"))
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	sums, err := st.ListSessions(ctx, []string{"interactive"}, 50)
	if err != nil {
		t.Fatal(err)
	}
	var titles []string
	for _, s := range sums {
		titles = append(titles, s.Session.Source+" | "+s.Session.Title)
		if s.Session.PromptHash == "" {
			t.Errorf("%s has no prompt to resume with", s.Session.Title)
		} else if p, _ := st.Prompt(ctx, s.Session.PromptHash); p == "" {
			t.Errorf("%s: empty prompt", s.Session.Title)
		}
	}
	sort.Strings(titles)
	want := []string{
		"claude-code | Imported from Claude Code: Fix the parser",
		"claude-code | Imported from Claude Code: continue after the compact",
		"codex-cli | Imported from Codex CLI: list the files",
		"opencode | Imported from opencode: Refactor the loader",
		"opencode | Imported from opencode: write a test",
	}
	if strings.Join(titles, "\n") != strings.Join(want, "\n") {
		t.Fatalf("sessions:\n%s", strings.Join(titles, "\n"))
	}
	for _, s := range sums {
		if s.Session.Title == "Imported from opencode: Refactor the loader" {
			msgs, _ := st.Messages(ctx, s.Session.ID)
			if len(msgs) != 4 || msgs[0].Content != "refactor the loader\n\n[attached file: loader.go]" || s.Session.Cwd != "/home/u/oc" ||
				s.Session.Usage.Cached != 9000 || s.Session.ContextTokens != 150 {
				t.Fatalf("an opencode session = %+v, %+v", s.Session, msgs)
			}
		}
	}
	st.Close()

	code, out, _ := runIn(t, "import", "--from-opencode", db, "--home", home)
	if code != 0 || out != "imported 0 sessions (0 messages); 2 already present, 0 skipped\n" {
		t.Fatalf("again: %q", out)
	}
	code, out, _ = runIn(t, "import", "--from-codex", filepath.Join(fixtures, "codex"), "--home", home, "--force")
	if code != 0 || out != "imported 1 sessions (2 messages); 0 already present, 0 skipped\n" {
		t.Fatalf("--force: %q", out)
	}
}

func TestASessionThePythonImportedIsNotImportedTwice(t *testing.T) {
	src := t.TempDir()
	pdb, err := sql.Open("sqlite", filepath.Join(src, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := pdb.Exec(pySchema); err != nil {
		t.Fatal(err)
	}
	if _, err := pdb.Exec(`INSERT INTO sessions (id, source, started_at, title, origin_json) VALUES
		('20261003_100000_cccccccc', 'opencode', 1759485600, 'Imported from opencode: Refactor the loader',
		 '{"imported_from": {"tool": "opencode", "path": "/x/opencode.db", "foreign_session_id": "ses_a"}}')`); err != nil {
		t.Fatal(err)
	}
	if _, err := pdb.Exec(`INSERT INTO messages (session_id, role, content, timestamp) VALUES
		('20261003_100000_cccccccc', 'user', 'refactor the loader', 1759485601)`); err != nil {
		t.Fatal(err)
	}
	pdb.Close()
	home := t.TempDir()
	if code, _, errOut := runIn(t, "import", "--from", src, "--home", home); code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	code, out, _ := runIn(t, "import", "--from-opencode", filepath.Join(fixtures, "opencode.db"), "--home", home)
	if code != 0 || out != "imported 1 sessions (2 messages); 1 already present, 0 skipped\n" {
		t.Fatalf("after the Python's own import: %q", out)
	}
}
