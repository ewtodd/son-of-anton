package main

// Phase 11c: `son-of-anton problem run|list|watch|stop|resume` against an
// in-process daemon and internal/fakellm.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

const (
	workerMark  = "on a physics analysis problem on your own"
	managerMark = "You review a physics analysis"
)

func tool(id, name string, args map[string]any) fakellm.ToolCall {
	b, _ := json.Marshal(args)
	return fakellm.ToolCall{ID: id, Name: name, Arguments: string(b)}
}

func results(id, content string) fakellm.ToolCall {
	return tool(id, "write_file", map[string]any{"path": "RESULTS.txt", "content": content})
}

// physHome is a home whose daemon is serving, with a physics config and a
// spec file; it returns the home, the spec path and the workspace root.
func physHome(t *testing.T, turns ...fakellm.Turn) (h, spec, root string) {
	t.Helper()
	fake := fakellm.New(t, turns...)
	root = t.TempDir()
	data := t.TempDir()
	if err := os.WriteFile(filepath.Join(data, "d.root"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	h = home(t, fmt.Sprintf("[physics]\npython = %q\ndata_dirs = [%q]\nworkspace_root = %q\n", filepath.Join(t.TempDir(), "python3"), data, root))
	cfg, err := os.ReadFile(filepath.Join(h, "config.toml"))
	if err != nil {
		t.Fatal(err)
	}
	cfg = []byte(strings.Replace(string(cfg), "http://127.0.0.1:9/v1", fake.URL, 1))
	if err := os.WriteFile(filepath.Join(h, "config.toml"), cfg, 0o600); err != nil {
		t.Fatal(err)
	}
	spec = filepath.Join(t.TempDir(), "p.toml")
	body := fmt.Sprintf(`name = "peak"
problem = "Report centroid and width."
data = [%q]
[[checks]]
id = "c"
key = "centroid"
expected = 10.0
tolerance = 0.5
[[checks]]
id = "w"
key = "width"
expected = 2.0
tolerance = 0.25
`, filepath.Join(data, "d.root"))
	if err := os.WriteFile(spec, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	serve(t, h)
	return h, spec, root
}

func accept() []fakellm.Turn {
	return []fakellm.Turn{
		{When: managerMark, ToolCalls: []fakellm.ToolCall{tool("r", "review", map[string]any{"verdict": "accept", "feedback": "ok"})}},
		{When: managerMark, Content: "Done."},
	}
}

func TestProblemRunWaitExitCodes(t *testing.T) {
	for _, c := range []struct {
		name, content string
		code          int
		state         string
	}{
		{"passes", "centroid = 10.2\nwidth = 2.1\n", 0, "passed"},
		{"fails", "centroid = 10.2\nwidth = 9\n", 1, "failed"},
	} {
		t.Run(c.name, func(t *testing.T) {
			h, spec, _ := physHome(t, append([]fakellm.Turn{
				{When: workerMark, ToolCalls: []fakellm.ToolCall{results("w", c.content)}},
				{When: workerMark, Content: "finished"},
			}, accept()...)...)
			r := cli(t, t.TempDir(), "", nil, "--home", h, "problem", "run", spec, "--wait")
			if r.code != c.code {
				t.Fatalf("exit %d, want %d:\n%s%s", r.code, c.code, r.out, r.errOut)
			}
			for _, want := range []string{"run: ", "directory: ", "work session 1 started", "work session 1 ended: ", " tokens", "review 1: accept", "scored: " + c.state, "_peak_test-model: " + c.state} {
				if !strings.Contains(r.out, want) {
					t.Errorf("output lacks %q:\n%s", want, r.out)
				}
			}
			// list and watch of the finished run.
			l := cli(t, t.TempDir(), "", nil, "--home", h, "problem", "list")
			if l.code != 0 || !strings.Contains(l.out, "  "+c.state+"  1 sessions") {
				t.Errorf("list: %d %q", l.code, l.out)
			}
			id := strings.Fields(l.out)[0]
			w := cli(t, t.TempDir(), "", nil, "--home", h, "problem", "watch", id)
			if w.code != c.code || !strings.Contains(w.out, "review 1: accept") || !strings.HasSuffix(w.out, "run "+id+": "+c.state+"\n") {
				t.Errorf("watch: %d %q", w.code, w.out)
			}
		})
	}
}

func TestProblemRunFlags(t *testing.T) {
	h, spec, root := physHome(t,
		append([]fakellm.Turn{
			{When: workerMark, ToolCalls: []fakellm.ToolCall{results("w", "centroid = 10\nwidth = 2\n")}},
			{When: workerMark, Content: "finished"},
		}, accept()...)...)
	r := cli(t, t.TempDir(), "", nil, "--home", h, "problem", "run", spec, "--model", "m-one", "--manager-model", "m-two",
		"--max-sessions", "3", "--session-tokens", "5000", "--total-tokens", "9000", "--hours", "1.5", "--wait")
	if r.code != 0 {
		t.Fatalf("%d %s%s", r.code, r.out, r.errOut)
	}
	ents, _ := os.ReadDir(root)
	if len(ents) != 1 || !strings.HasSuffix(ents[0].Name(), "_peak_m-one") {
		t.Fatalf("workspace root: %v", ents)
	}
	b, err := os.ReadFile(filepath.Join(root, ents[0].Name(), "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	var cfg struct {
		WorkerModel  string `json:"worker_model"`
		ManagerModel string `json:"manager_model"`
		Limits       struct {
			MaxSessions   int     `json:"max_sessions"`
			SessionTokens int     `json:"session_tokens"`
			TotalTokens   int     `json:"total_tokens"`
			Hours         float64 `json:"hours"`
		} `json:"limits"`
	}
	_ = json.Unmarshal(b, &cfg)
	if cfg.WorkerModel != "m-one" || cfg.ManagerModel != "m-two" || cfg.Limits.MaxSessions != 3 || cfg.Limits.SessionTokens != 5000 || cfg.Limits.TotalTokens != 9000 || cfg.Limits.Hours != 1.5 {
		t.Errorf("config.json: %s", b)
	}
}

func TestProblemStopAndExitCode(t *testing.T) {
	h, spec, _ := physHome(t, fakellm.Turn{When: workerMark, Hang: true})
	r := cli(t, t.TempDir(), "", nil, "--home", h, "problem", "run", spec)
	if r.code != 0 || !strings.HasPrefix(r.out, "run: ") {
		t.Fatalf("run: %d %q %q", r.code, r.out, r.errOut)
	}
	id := strings.TrimSpace(strings.TrimPrefix(strings.SplitN(r.out, "\n", 2)[0], "run: "))
	// Another run is refused while this one goes.
	if r2 := cli(t, t.TempDir(), "", nil, "--home", h, "problem", "run", spec); r2.code != 1 || !strings.Contains(r2.errOut, id) {
		t.Errorf("second run: %d %q", r2.code, r2.errOut)
	}
	// A follower sees the stop and exits 3.
	done := make(chan result, 1)
	go func() { done <- cli(t, t.TempDir(), "", nil, "--home", h, "problem", "watch", id) }()
	time.Sleep(300 * time.Millisecond)
	if s := cli(t, t.TempDir(), "", nil, "--home", h, "problem", "stop", id); s.code != 0 || s.out != "run "+id+": stopped\n" {
		t.Fatalf("stop: %d %q %q", s.code, s.out, s.errOut)
	}
	select {
	case w := <-done:
		if w.code != 3 || !strings.HasSuffix(w.out, "run "+id+": stopped\n") {
			t.Errorf("watch: %d %q", w.code, w.out)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("the follower did not end")
	}
	if w := cli(t, t.TempDir(), "", nil, "--home", h, "problem", "watch", id); w.code != 3 {
		t.Errorf("watch of a stopped run: %d", w.code)
	}
}

func TestProblemUsageErrors(t *testing.T) {
	h := home(t, "")
	for _, args := range [][]string{
		{"problem"},
		{"problem", "frob"},
		{"problem", "run"},
		{"problem", "run", "a.toml", "b.toml"},
		{"problem", "run", "a.toml", "--max-sessions", "0"},
		{"problem", "run", "a.toml", "--session-tokens", "x"},
		{"problem", "run", "a.toml", "--total-tokens"},
		{"problem", "run", "a.toml", "--hours", "-1"},
		{"problem", "run", "a.toml", "--bogus"},
		{"problem", "run", "a.toml", "--model"},
		{"problem", "list", "extra"},
		{"problem", "watch"},
		{"problem", "stop"},
		{"problem", "resume", "a", "b"},
		{"problem", "stop", "a", "--wait"},
	} {
		r := cli(t, t.TempDir(), "", nil, append([]string{"--home", h}, args...)...)
		if r.code != 2 || r.errOut == "" {
			t.Errorf("%v: exit %d, stderr %q", args, r.code, r.errOut)
		}
		if _, err := os.Stat(filepath.Join(h, "run", "soa-d.sock")); err == nil {
			t.Fatalf("%v started a daemon", args)
		}
	}
	if r := cli(t, t.TempDir(), "", nil, "--home", h, "problem", "run", "/no/such/spec.toml"); r.code != 1 || !strings.Contains(r.errOut, "cannot read the spec") {
		t.Errorf("missing spec file: %d %q", r.code, r.errOut)
	}
	if r := cli(t, t.TempDir(), "", nil, "--help"); !strings.Contains(r.out, "problem run SPEC") {
		t.Errorf("usage does not list problem: %q", r.out)
	}
}
