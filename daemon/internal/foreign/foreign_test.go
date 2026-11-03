package foreign_test

// Phase 7e: the parsers are the Python's. testdata/expected.json is what
// son_of_anton_cli/foreign_sessions.py produced on the fixtures beside it.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/foreign"
)

type pyTurn struct {
	Role      string   `json:"role"`
	Content   string   `json:"content"`
	Timestamp *float64 `json:"timestamp"`
}

type pyParsed struct {
	Turns []pyTurn `json:"turns"`
	Cwd   string   `json:"cwd"`
	Title string   `json:"title_guess"`
	ID    string   `json:"session_id"`
	Usage struct {
		Input      int `json:"input_tokens"`
		Output     int `json:"output_tokens"`
		CacheRead  int `json:"cache_read_tokens"`
		CacheWrite int `json:"cache_write_tokens"`
		Reasoning  int `json:"reasoning_tokens"`
		Calls      int `json:"api_call_count"`
		LastPrompt int `json:"last_prompt_tokens"`
	} `json:"carried_usage"`
}

func expected(t *testing.T) (map[string]map[string]pyParsed, []string, []string) {
	t.Helper()
	b, err := os.ReadFile("testdata/expected.json")
	if err != nil {
		t.Fatal(err)
	}
	var all struct {
		Claude         map[string]pyParsed `json:"claude"`
		Codex          map[string]pyParsed `json:"codex"`
		Opencode       map[string]pyParsed `json:"opencode"`
		ClaudeListed   []string            `json:"claude_listed"`
		OpencodeListed []string            `json:"opencode_listed"`
	}
	if err := json.Unmarshal(b, &all); err != nil {
		t.Fatal(err)
	}
	return map[string]map[string]pyParsed{"claude": all.Claude, "codex": all.Codex, "opencode": all.Opencode}, all.ClaudeListed, all.OpencodeListed
}

func same(t *testing.T, what string, got foreign.Session, want pyParsed) {
	t.Helper()
	if got.Cwd != want.Cwd || got.Title != want.Title || got.ID != want.ID {
		t.Errorf("%s: cwd/title/id = %q %q %q, want %q %q %q", what, got.Cwd, got.Title, got.ID, want.Cwd, want.Title, want.ID)
	}
	if len(got.Turns) != len(want.Turns) {
		t.Fatalf("%s: %d turns, want %d: %+v", what, len(got.Turns), len(want.Turns), got.Turns)
	}
	for i, w := range want.Turns {
		g := got.Turns[i]
		if g.Role != w.Role || g.Content != w.Content || (g.Timestamp == nil) != (w.Timestamp == nil) ||
			(g.Timestamp != nil && *g.Timestamp != *w.Timestamp) {
			t.Errorf("%s turn %d = %+v (ts %v), want %+v (ts %v)", what, i, g, g.Timestamp, w, w.Timestamp)
		}
	}
	u := got.Usage
	wu := want.Usage
	if u.Input != wu.Input || u.Output != wu.Output || u.CacheRead != wu.CacheRead || u.CacheWrite != wu.CacheWrite ||
		u.Reasoning != wu.Reasoning || u.Calls != wu.Calls || u.LastPrompt != wu.LastPrompt {
		t.Errorf("%s usage = %+v, want %+v", what, u, wu)
	}
}

func TestClaudeCodeIsParsedAsThePythonDoes(t *testing.T) {
	want, listed, _ := expected(t)
	paths, err := foreign.ListClaude("testdata/claude")
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, p := range paths {
		names = append(names, filepath.Base(p))
	}
	if len(names) != len(listed) || names[0] != listed[0] || names[1] != listed[1] {
		t.Fatalf("listed %v, want %v (one level deep only)", names, listed)
	}
	for name, w := range want["claude"] {
		s, err := foreign.ParseClaude(filepath.Join("testdata", "claude", "proj-a", name))
		if err != nil {
			t.Fatal(err)
		}
		same(t, name, s, w)
	}
}

func TestCodexIsParsedAsThePythonDoes(t *testing.T) {
	want, _, _ := expected(t)
	paths, err := foreign.ListCodex("testdata/codex")
	if err != nil || len(paths) != 1 {
		t.Fatalf("ListCodex = %v, %v", paths, err)
	}
	s, err := foreign.ParseCodex(paths[0])
	if err != nil {
		t.Fatal(err)
	}
	same(t, filepath.Base(paths[0]), s, want["codex"][filepath.Base(paths[0])])
}

func TestOpencodeIsParsedAsThePythonDoes(t *testing.T) {
	want, _, listed := expected(t)
	db := filepath.Join("testdata", "opencode.db")
	before, _ := os.ReadFile(db)
	ids, err := foreign.ListOpencode(db)
	if err != nil || len(ids) != len(listed) || ids[0] != listed[0] || ids[1] != listed[1] {
		t.Fatalf("ListOpencode = %v, %v; want %v (roots, newest first)", ids, err, listed)
	}
	for id, w := range want["opencode"] {
		s, err := foreign.ParseOpencode(db, id)
		if err != nil {
			t.Fatal(err)
		}
		same(t, id, s, w)
	}
	if after, _ := os.ReadFile(db); string(after) != string(before) {
		t.Fatal("the opencode database was written")
	}
}
