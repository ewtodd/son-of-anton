package server_test

// Phase 11c: physics runs. A worker session (sandboxed, in the run directory)
// and a manager session (read-only) are driven by the run's controller, both
// against internal/fakellm: the worker's turns are told apart from the
// manager's by their system prompts (fakellm's When). The tests that run
// commands in the sandbox skip, saying why, where bwrap cannot run.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/sandbox/sandboxtest"
)

// The spec's expected values and tolerances: strings that must never reach a
// request, and that the models' own (passing) values do not contain.
const (
	expA, tolA = "4217.5", "0.4813"
	expB, tolB = "88.25", "0.0617"

	goodResults = "centroid_a = 4217.8\nwidth_b = 88.27\n"
	badResults  = "centroid_a = 4217.8\nwidth_b = 99.1\n"

	formatOK = "RESULTS.txt: every key present, each a plain number."

	workerMark  = "on a physics analysis problem on your own"
	managerMark = "You review a physics analysis"
)

func wt(t fakellm.Turn) fakellm.Turn { t.When = workerMark; return t }
func mt(t fakellm.Turn) fakellm.Turn { t.When = managerMark; return t }

func tcall(id, name string, args map[string]any) fakellm.ToolCall {
	b, _ := json.Marshal(args)
	return call(id, name, string(b))
}

func writeResults(id, content string) fakellm.ToolCall {
	return tcall(id, "Write", map[string]any{"file_path": "RESULTS.txt", "content": content})
}

func reviewCall(id, verdict, feedback string) fakellm.ToolCall {
	return tcall(id, "review", map[string]any{"verdict": verdict, "feedback": feedback})
}

// accepting is a manager that reviews once and accepts.
func accepting() []fakellm.Turn {
	return []fakellm.Turn{
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{reviewCall("r1", "accept", "fine")}}),
		mt(fakellm.Turn{Content: "Done."}),
	}
}

type physEnv struct {
	t       *testing.T
	h       *harness
	fake    *fakellm.Server
	c       *client
	root    string // physics.workspace_root
	dataDir string // physics.data_dirs
	runtime string
}

// newPhys starts a daemon with the physics config. sandbox: the runtime is a
// stand-in the sandbox can run (the test skips where it cannot).
func newPhys(t *testing.T, sandbox bool, extra string, turns ...fakellm.Turn) *physEnv {
	t.Helper()
	var rt string
	if sandbox {
		rt = sandboxtest.Runtime(t)
	} else {
		rt = filepath.Join(t.TempDir(), "bin")
		if err := os.Mkdir(rt, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	data, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(data, "events.root"), []byte("DATA"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t, turns...)
	cfg := fmt.Sprintf("[physics]\npython = %q\ndata_dirs = [%q]\nworkspace_root = %q\n%s\n", filepath.Join(rt, "python3"), data, root, extra)
	h := newHarness(t, fake, cfg)
	return &physEnv{t: t, h: h, fake: fake, c: h.dial(), root: root, dataDir: data, runtime: rt}
}

func (e *physEnv) spec() string { return e.specWith("") }

// specWith is the spec with extra lines inserted before the checks.
func (e *physEnv) specWith(extra string) string {
	return fmt.Sprintf(`name = "yap_A_peak"
problem = """Fit the peak in the data and report centroid_a (keV) and width_b (keV)."""
data = [%q]
%s
[[checks]]
id = "c1"
key = "centroid_a"
expected = %s
tolerance = %s

[[checks]]
id = "c2"
key = "width_b"
expected = %s
tolerance = %s
`, filepath.Join(e.dataDir, "events.root"), extra, expA, tolA, expB, tolB)
}

func (e *physEnv) start(spec string, extra map[string]any) map[string]any {
	e.t.Helper()
	p := map[string]any{"spec": spec}
	for k, v := range extra {
		p[k] = v
	}
	return e.c.ok("physics.run", p)
}

func (e *physEnv) runs() []map[string]any {
	return objs(e.c.ok("physics.list", nil), "runs")
}

func (e *physEnv) run(id string) map[string]any {
	e.t.Helper()
	for _, r := range e.runs() {
		if str(r, "id") == id {
			return r
		}
	}
	e.t.Fatalf("no run %s", id)
	return nil
}

func isFinal(state string) bool {
	return state == "passed" || state == "failed" || state == "stopped" || state == "interrupted"
}

// final waits for the run to leave working and reviewing and returns it.
func (e *physEnv) final(id string) map[string]any {
	e.t.Helper()
	var r map[string]any
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		if r = e.run(id); isFinal(str(r, "state")) {
			return r
		}
		time.Sleep(25 * time.Millisecond)
	}
	e.t.Fatalf("run %s did not end: %v", id, r)
	return nil
}

// requests of one session kind: the indexes of the fake's requests whose
// system prompt carries the mark.
func (e *physEnv) requests(mark string) []int {
	var out []int
	for i := range e.fake.Bodies() {
		msgs := e.fake.Messages(i)
		if len(msgs) > 0 && bytes.Contains(msgs[0], []byte(mark)) {
			out = append(out, i)
		}
	}
	return out
}

// lastUser is the content of the last message of request i.
func (e *physEnv) lastContent(i int) string {
	msgs := wire(e.t, e.fake, i)
	c, _ := msgs[len(msgs)-1]["content"].(string)
	return c
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func gitLog(t *testing.T, dir string) []string {
	t.Helper()
	cmd := exec.Command("git", "log", "--reverse", "--format=%s")
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_NOSYSTEM=1")
	for _, kv := range cmd.Env {
		if strings.HasPrefix(kv, "GIT_DIR=") {
			t.Fatal("GIT_DIR is set in the test environment")
		}
	}
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git log in %s: %v: %s", dir, err, out)
	}
	return strings.Split(strings.TrimSpace(string(out)), "\n")
}

// noExpectedValues fails if any request either session made carries an
// expected value or a tolerance.
func (e *physEnv) noExpectedValues() {
	e.t.Helper()
	bodies := e.fake.Bodies()
	if len(bodies) == 0 {
		e.t.Fatal("no request was made")
	}
	for i, b := range bodies {
		for _, secret := range []string{expA, tolA, expB, tolB} {
			if bytes.Contains(b, []byte(secret)) {
				e.t.Errorf("request %d contains %q", i, secret)
			}
		}
	}
}

func TestPhysicsRunPasses(t *testing.T) {
	e := newPhys(t, false, "max_sessions = 3\n",
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{writeResults("w1", goodResults)}, PromptTokens: 100, CompletionTokens: 10}),
		wt(fakellm.Turn{Content: "Found both values.\nSee RESULTS.txt.", PromptTokens: 200, CachedTokens: 100, CompletionTokens: 20, ServedModel: "served-x"}),
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{reviewCall("r1", "accept", "fine")}}),
		mt(fakellm.Turn{Content: "Done."}),
	)
	w := e.c.ok("physics.run", map[string]any{"spec": e.spec()})
	id := str(w, "id")
	dir := str(w, "dir")
	if !strings.HasPrefix(filepath.Base(dir), "20") || !strings.HasSuffix(dir, "_yap_A_peak_test-model") || filepath.Base(dir) != id || filepath.Dir(dir) != e.root {
		t.Fatalf("run id %q in %q", id, dir)
	}
	r := e.final(id)
	if str(r, "state") != "passed" || r["sessions_done"] != float64(1) {
		t.Fatalf("run: %v", r)
	}

	eval := readFile(t, filepath.Join(dir, "FORMAL_EVAL.md"))
	for _, want := range []string{"Status: passed", "Checks passed: 2/2", "c1 (centroid_a)", "c2 (width_b)"} {
		if !strings.Contains(eval, want) {
			t.Errorf("FORMAL_EVAL.md lacks %q:\n%s", want, eval)
		}
	}
	got := gitLog(t, dir)
	want := []string{"start: yap_A_peak", "session 1: Found both values.", "review 1: accept", "formal evaluation: passed"}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Errorf("commits %q, want %q", got, want)
	}
	for _, f := range []string{".gitignore", "PROBLEM.md", "config.json", "EVENT_LOG.jsonl", "REVIEWS.md", "logs/session-1.md", "RESULTS.txt"} {
		if _, err := os.Stat(filepath.Join(dir, f)); err != nil {
			t.Errorf("run directory lacks %s", f)
		}
	}
	if ig := readFile(t, filepath.Join(dir, ".gitignore")); ig != ".home/\n__pycache__/\n*.root\n*.npy\n*.npz\n*.h5\n*.bin\n*.pkl\n" {
		t.Errorf(".gitignore = %q", ig)
	}
	if pm := readFile(t, filepath.Join(dir, "PROBLEM.md")); !strings.Contains(pm, "report centroid_a (keV) and width_b") || !strings.Contains(pm, "- centroid_a\n- width_b\n") {
		t.Errorf("PROBLEM.md = %q", pm)
	}
	if rv := readFile(t, filepath.Join(dir, "REVIEWS.md")); !strings.Contains(rv, "Review 1: accept") || !strings.Contains(rv, "fine") {
		t.Errorf("REVIEWS.md = %q", rv)
	}

	// The worker's first message is PROBLEM.md and "Begin."; the manager's is
	// the review message in its exact form.
	wr, mr := e.requests(workerMark), e.requests(managerMark)
	if len(wr) != 2 || len(mr) != 2 {
		t.Fatalf("requests: worker %v manager %v", wr, mr)
	}
	first := wire(t, e.fake, wr[0])
	if first[1]["role"] != "user" || first[1]["content"] != readFile(t, filepath.Join(dir, "PROBLEM.md"))+"\nBegin." {
		t.Errorf("the worker's first message = %q", first[1]["content"])
	}
	wantReview := "Review of work session 1 of at most 3.\n\nThe agent's closing message:\nFound both values.\nSee RESULTS.txt.\n\n" + formatOK + "\n\nThe session's commands are in logs/session-1.md."
	if got := e.lastContent(mr[0]); got != wantReview {
		t.Errorf("the review message:\n%q\nwant\n%q", got, wantReview)
	}

	// The worker's tools are the sandbox set; the manager's read and review.
	toolNames := func(i int) string {
		var req struct {
			Tools []struct {
				Function struct{ Name string } `json:"function"`
			} `json:"tools"`
		}
		_ = json.Unmarshal(e.fake.Bodies()[i], &req)
		var names []string
		for _, tl := range req.Tools {
			names = append(names, tl.Function.Name)
		}
		return strings.Join(names, ",")
	}
	if got := toolNames(wr[0]); got != "Bash,BashOutput,KillShell,Read,Write,Edit,Glob,Grep,TodoWrite" {
		t.Errorf("the worker's tools: %s", got)
	}
	if got := toolNames(mr[0]); got != "Read,Glob,Grep,review" {
		t.Errorf("the manager's tools: %s", got)
	}

	// The record: llm_call and tool_call lines.
	var llm, tool int
	for _, line := range strings.Split(strings.TrimSpace(readFile(t, filepath.Join(dir, "EVENT_LOG.jsonl"))), "\n") {
		var ev map[string]any
		if err := json.Unmarshal([]byte(line), &ev); err != nil {
			t.Fatalf("EVENT_LOG line %q: %v", line, err)
		}
		switch ev["kind"] {
		case "llm_call":
			llm++
			for _, k := range []string{"ts", "agent", "session", "model", "served_model", "input_tokens", "cached_tokens", "output_tokens", "duration_s", "stop_reason"} {
				if _, ok := ev[k]; !ok {
					t.Errorf("llm_call lacks %s: %s", k, line)
				}
			}
			if ev["input_tokens"] == float64(200) && (ev["cached_tokens"] != float64(100) || ev["served_model"] != "served-x" || ev["stop_reason"] != "stop" || ev["agent"] != "worker" || ev["model"] != "test-model") {
				t.Errorf("llm_call: %s", line)
			}
		case "tool_call":
			tool++
			for _, k := range []string{"ts", "agent", "session", "tool", "duration_s"} {
				if _, ok := ev[k]; !ok {
					t.Errorf("tool_call lacks %s: %s", k, line)
				}
			}
			if _, has := ev["exit_code"]; has && ev["tool"] != "Bash" {
				t.Errorf("a %v call has an exit code: %s", ev["tool"], line)
			}
		default:
			t.Errorf("EVENT_LOG line %s", line)
		}
	}
	if llm != 4 || tool != 2 {
		t.Errorf("EVENT_LOG has %d llm_call and %d tool_call lines, want 4 and 2", llm, tool)
	}
	var cfg struct {
		Name         string   `json:"name"`
		Data         []string `json:"data"`
		WorkerModel  string   `json:"worker_model"`
		ManagerModel string   `json:"manager_model"`
		Limits       struct {
			MaxSessions   int     `json:"max_sessions"`
			SessionTokens int     `json:"session_tokens"`
			TotalTokens   int     `json:"total_tokens"`
			Hours         float64 `json:"hours"`
		} `json:"limits"`
	}
	if err := json.Unmarshal([]byte(readFile(t, filepath.Join(dir, "config.json"))), &cfg); err != nil {
		t.Fatal(err)
	}
	if cfg.Name != "yap_A_peak" || len(cfg.Data) != 1 || cfg.WorkerModel != "test-model" || cfg.ManagerModel != "test-model" ||
		cfg.Limits.MaxSessions != 3 || cfg.Limits.SessionTokens != 400000 || cfg.Limits.TotalTokens != 3000000 || cfg.Limits.Hours != 8 {
		t.Errorf("config.json: %+v", cfg)
	}

	// The tokens: prompt tokens not from the cache, plus completion tokens.
	// 100+10, (200-100)+20, and the manager's two (zero) requests.
	if r["tokens"] != float64(230) {
		t.Errorf("tokens = %v, want 230", r["tokens"])
	}

	// The progress lines a watcher sees.
	res := e.c.ok("physics.watch", map[string]any{"run": id})
	lines := strings.Join(strs(res, "lines"), "\n")
	for _, want := range []string{"work session 1 started", "work session 1 ended: 230 tokens", "review 1: accept", "scored: passed (2/2)"} {
		if !strings.Contains(lines, want) {
			t.Errorf("progress lacks %q:\n%s", want, lines)
		}
	}
	if str(res, "state") != "passed" {
		t.Errorf("watch state = %v", res["state"])
	}
	e.noExpectedValues()
}

func TestPhysicsRunFailsOnAWrongValue(t *testing.T) {
	e := newPhys(t, false, "",
		append([]fakellm.Turn{
			wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{writeResults("w1", badResults)}}),
			wt(fakellm.Turn{Content: "done"}),
		}, accepting()...)...)
	w := e.start(e.spec(), nil)
	r := e.final(str(w, "id"))
	if str(r, "state") != "failed" {
		t.Fatalf("run: %v", r)
	}
	eval := readFile(t, filepath.Join(str(w, "dir"), "FORMAL_EVAL.md"))
	if !strings.Contains(eval, "Status: failed") || !strings.Contains(eval, "Checks passed: 1/2") || !strings.Contains(eval, "value 99.1: failed") {
		t.Errorf("FORMAL_EVAL.md:\n%s", eval)
	}
	log := gitLog(t, str(w, "dir"))
	if log[len(log)-1] != "formal evaluation: failed" {
		t.Errorf("commits %q", log)
	}
	e.noExpectedValues()
}

func TestPhysicsContinueSendsTheFeedbackToTheSameSession(t *testing.T) {
	e := newPhys(t, false, "max_sessions = 3\n",
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{writeResults("w1", "centroid_a = 4217.8\nwidth_b = 88.27 +- 0.1\n")}}),
		wt(fakellm.Turn{Content: "first pass"}),
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{reviewCall("r1", "continue", "Fix the width line.")}}),
		mt(fakellm.Turn{Content: "Sent back."}),
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{writeResults("w2", goodResults)}}),
		wt(fakellm.Turn{Content: "second pass"}),
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{reviewCall("r2", "accept", "good")}}),
		mt(fakellm.Turn{Content: "Done."}),
	)
	w := e.start(e.spec(), nil)
	before := e.run(str(w, "id"))
	r := e.final(str(w, "id"))
	if str(r, "state") != "passed" || r["sessions_done"] != float64(2) {
		t.Fatalf("run: %v", r)
	}
	if r["worker_session"] != before["worker_session"] || r["manager_session"] != before["manager_session"] {
		t.Errorf("the session ids changed: %v then %v", before, r)
	}

	wr, mr := e.requests(workerMark), e.requests(managerMark)
	if len(wr) != 4 || len(mr) != 4 {
		t.Fatalf("requests: worker %v manager %v", wr, mr)
	}
	// The manager's first review sees the bad value's format.
	wantCheck := `RESULTS.txt: not plain numbers: width_b ("88.27 +- 0.1").`
	if got := e.lastContent(mr[0]); !strings.Contains(got, wantCheck) || !strings.Contains(got, "Review of work session 1 of at most 3.") {
		t.Errorf("the first review message: %q", got)
	}
	wantFeedback := "Reviewer's feedback on work session 1:\n\nFix the width line.\n\n" + wantCheck + "\n\nThis is work session 2 of at most 3."
	if got := e.lastContent(wr[2]); got != wantFeedback {
		t.Errorf("the feedback message:\n%q\nwant\n%q", got, wantFeedback)
	}
	// Same session: the second session's request replays the first's messages.
	prev, next := e.fake.Messages(wr[1]), e.fake.Messages(wr[2])
	for i := range prev {
		if !bytes.Equal(prev[i], next[i]) {
			t.Fatalf("message %d of the worker's request changed:\n%s\n%s", i, prev[i], next[i])
		}
	}
	if len(next) != len(prev)+2 { // its closing message, then the feedback
		t.Errorf("%d messages after %d", len(next), len(prev))
	}
	// The manager's second review replays its first.
	mprev, mnext := e.fake.Messages(mr[1]), e.fake.Messages(mr[2])
	for i := range mprev {
		if !bytes.Equal(mprev[i], mnext[i]) {
			t.Fatalf("message %d of the manager's request changed", i)
		}
	}
	if got := gitLog(t, str(w, "dir")); strings.Join(got, "|") != "start: yap_A_peak|session 1: first pass|review 1: continue|session 2: second pass|review 2: accept|formal evaluation: passed" {
		t.Errorf("commits %q", got)
	}
	e.noExpectedValues()
}

func TestPhysicsLastSessionAndMissingReview(t *testing.T) {
	// The manager answers without calling review: it gets one reminder, then
	// counts as continue, its last message the feedback. The second session is
	// the last of two and is told so; the run is scored after its review
	// whatever the verdict.
	e := newPhys(t, false, "max_sessions = 2\n",
		wt(fakellm.Turn{Content: "nothing written"}),
		mt(fakellm.Turn{Content: "I think it needs work."}),
		mt(fakellm.Turn{Content: "Still undecided: write the file."}),
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{writeResults("w2", goodResults)}}),
		wt(fakellm.Turn{Content: "written"}),
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{reviewCall("r2", "continue", "keep going")}}),
		mt(fakellm.Turn{Content: "ok"}),
	)
	w := e.start(e.spec(), nil)
	r := e.final(str(w, "id"))
	if str(r, "state") != "passed" || r["sessions_done"] != float64(2) {
		t.Fatalf("run: %v", r)
	}
	wr, mr := e.requests(workerMark), e.requests(managerMark)
	if len(wr) != 3 || len(mr) != 4 {
		t.Fatalf("requests: worker %v manager %v (no third work session after the last review)", wr, mr)
	}
	if got := e.lastContent(mr[0]); !strings.Contains(got, "The agent's closing message:\nnothing written\n\nRESULTS.txt: missing.\n") {
		t.Errorf("the review message: %q", got)
	}
	if got := e.lastContent(mr[1]); got != "Call review with your verdict." {
		t.Errorf("the reminder: %q", got)
	}
	want := "Reviewer's feedback on work session 1:\n\nStill undecided: write the file.\n\nRESULTS.txt: missing.\n\nThis is work session 2 of at most 2. It is your last: leave RESULTS.txt complete with your best values before you stop."
	if got := e.lastContent(wr[1]); got != want {
		t.Errorf("the feedback:\n%q\nwant\n%q", got, want)
	}
	if rv := readFile(t, filepath.Join(str(w, "dir"), "REVIEWS.md")); !strings.Contains(rv, "Review 1: continue") || !strings.Contains(rv, "Review 2: continue") {
		t.Errorf("REVIEWS.md = %q", rv)
	}
}

// toolResults are the tool messages of request i, by tool call id.
func (e *physEnv) toolResults(i int) map[string]string {
	out := map[string]string{}
	for _, m := range wire(e.t, e.fake, i) {
		if m["role"] == "tool" {
			id, _ := m["tool_call_id"].(string)
			c, _ := m["content"].(string)
			out[id] = c
		}
	}
	return out
}

func TestPhysicsNoRequestCarriesTheSpec(t *testing.T) {
	// Both sessions try to read the spec's directory; neither can. The run
	// directory never holds the spec (FORMAL_EVAL.md, which the daemon writes
	// after the last request, is the one file that names the expected values).
	var e *physEnv
	spec := func() string { return filepath.Join(e.h.home, "physics", "spec.toml") }
	specDir := func() string { return filepath.Join(e.h.home, "physics") }
	argsFor := func(f func() map[string]any) func() string {
		return func() string { b, _ := json.Marshal(f()); return string(b) }
	}
	read := func(p func() string) func() map[string]any {
		return func() map[string]any { return map[string]any{"file_path": p()} }
	}
	e = newPhys(t, false, "",
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{
			{ID: "w1", Name: "Read", ArgumentsFunc: argsFor(read(spec))},
			{ID: "w2", Name: "Grep", ArgumentsFunc: argsFor(func() map[string]any {
				return map[string]any{"pattern": "expected", "path": specDir()}
			})},
			writeResults("w3", goodResults),
		}}),
		wt(fakellm.Turn{Content: "done"}),
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{
			{ID: "m1", Name: "Read", ArgumentsFunc: argsFor(read(spec))},
			tcall("m2", "Read", map[string]any{"file_path": "RESULTS.txt"}),
			reviewCall("m3", "accept", "fine"),
		}}),
		mt(fakellm.Turn{Content: "Done."}),
	)
	w := e.start(e.spec(), nil)
	dir := str(w, "dir")
	if r := e.final(str(w, "id")); str(r, "state") != "passed" {
		t.Fatalf("run: %v", r)
	}
	// The spec is kept at <home>/physics/<id>.toml, with its values.
	kept := readFile(t, filepath.Join(e.h.home, "physics", str(w, "id")+".toml"))
	if !strings.Contains(kept, expA) || !strings.Contains(kept, tolB) {
		t.Errorf("the kept spec lacks its values:\n%s", kept)
	}
	wr, mr := e.requests(workerMark), e.requests(managerMark)
	for _, c := range []struct {
		who string
		res map[string]string
		ids []string
	}{{"worker", e.toolResults(wr[1]), []string{"w1", "w2"}}, {"manager", e.toolResults(mr[1]), []string{"m1"}}} {
		for _, id := range c.ids {
			if got := c.res[id]; !strings.Contains(got, "outside the paths this session may use") || !strings.Contains(got, dir) {
				t.Errorf("the %s's %s: %q", c.who, id, got)
			}
		}
	}
	if got := e.toolResults(mr[1])["m2"]; !strings.Contains(got, "centroid_a = 4217.8") {
		t.Errorf("the manager could not read RESULTS.txt: %q", got)
	}
	// The run directory holds no spec and no expected value, the files the
	// daemon writes after the last request aside.
	err := filepath.WalkDir(dir, func(p string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		if strings.HasSuffix(p, ".toml") {
			t.Errorf("the run directory holds %s", p)
		}
		if filepath.Base(p) == "FORMAL_EVAL.md" {
			return nil
		}
		b, _ := os.ReadFile(p)
		for _, secret := range []string{expA, tolA, expB, tolB} {
			if bytes.Contains(b, []byte(secret)) {
				t.Errorf("%s contains %q", p, secret)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	// Nor do the commits: only FORMAL_EVAL.md, in the last one, names them.
	cmd := exec.Command("git", "log", "--format=", "-p", "-S"+expA)
	cmd.Dir = dir
	out, _ := cmd.CombinedOutput()
	if n := strings.Count(string(out), "diff --git"); n != 1 || !strings.Contains(string(out), "FORMAL_EVAL.md") {
		t.Errorf("the commits that name an expected value:\n%s", out)
	}
	e.noExpectedValues()
}

func TestPhysicsFormatCheckReachesTheManager(t *testing.T) {
	e := newPhys(t, false, "",
		append([]fakellm.Turn{
			wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{writeResults("w1", "centroid_a = 4217.8\n")}}),
			wt(fakellm.Turn{Content: "partial"}),
		}, accepting()...)...)
	w := e.start(e.spec(), nil)
	e.final(str(w, "id"))
	if got := e.lastContent(e.requests(managerMark)[0]); !strings.Contains(got, "\n\nRESULTS.txt: missing keys: width_b.\n\n") {
		t.Errorf("the review message: %q", got)
	}
}

func TestPhysicsSpecRefusals(t *testing.T) {
	e := newPhys(t, false, "")
	good := e.spec()
	other := t.TempDir()
	if err := os.WriteFile(filepath.Join(other, "events.root"), []byte("X"), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct{ name, spec, want string }{
		{"name not a slug", strings.Replace(good, `name = "yap_A_peak"`, `name = "has space"`, 1), "not a slug"},
		{"no name", strings.Replace(good, `name = "yap_A_peak"`, ``, 1), "not a slug"},
		{"empty problem", strings.Replace(good, "Fit the peak in the data and report centroid_a (keV) and width_b (keV).", "  ", 1), "problem is empty"},
		{"data missing", strings.Replace(good, "events.root", "nope.root", 1), "does not exist"},
		{"data outside data_dirs", strings.Replace(good, e.dataDir, other, 1), "not under physics.data_dirs"},
		{"no checks", good[:strings.Index(good, "[[checks]]")], "no [[checks]]"},
		{"expected missing", strings.Replace(good, "expected = "+expA+"\n", "", 1), "expected must be a number"},
		{"expected not a number", strings.Replace(good, "expected = "+expA, `expected = "4217.5"`, 1), "expected must be a number"},
		{"tolerance zero", strings.Replace(good, "tolerance = "+tolA, "tolerance = 0", 1), "tolerance must be a positive number"},
		{"tolerance negative", strings.Replace(good, "tolerance = "+tolB, "tolerance = -1.5", 1), "tolerance must be a positive number"},
		{"tolerance missing", strings.Replace(good, "tolerance = "+tolB+"\n", "", 1), "tolerance must be a positive number"},
		{"key not in the problem", strings.Replace(good, `key = "width_b"`, `key = "sigma_c"`, 1), `"sigma_c" does not appear in the problem`},
		{"answer_template", good[:strings.Index(good, "[[checks]]")] + "answer_template = \"x\"\n" + good[strings.Index(good, "[[checks]]"):], "answer_template is not supported"},
		{"checker", strings.Replace(good, "data = [", "checker = \"c.py\"\ndata = [", 1), "checker is not supported"},
		{"not TOML", "name = = =", "not valid TOML"},
	} {
		_, err := e.c.call("physics.run", map[string]any{"spec": c.spec})
		if err == nil || err.Code != -32602 || !strings.Contains(err.Message, c.want) {
			t.Errorf("%s: %+v, want a refusal containing %q", c.name, err, c.want)
		}
	}
	// A refusal leaves nothing behind.
	if runs := e.runs(); len(runs) != 0 {
		t.Errorf("runs after refusals: %v", runs)
	}
	if ents, _ := os.ReadDir(e.root); len(ents) != 0 {
		t.Errorf("the workspace root holds %d entries", len(ents))
	}
	if e.fake.Count() != 0 {
		t.Errorf("a refused spec made %d requests", e.fake.Count())
	}
	// The model must be one the instance knows? Any name resolves; a missing
	// python is refused.
}

func TestPhysicsNeedsARuntime(t *testing.T) {
	fake := fakellm.New(t)
	h := newHarness(t, fake, "[physics]\nworkspace_root = \"/tmp\"\n")
	c := h.dial()
	_, err := c.call("physics.run", map[string]any{"spec": "name = \"x\""})
	if err == nil || !strings.Contains(err.Message, "physics.python is not set") {
		t.Fatalf("got %+v", err)
	}
}

func TestPhysicsSessionLimit(t *testing.T) {
	// 90% of a work session's tokens: the budget message is steered in and the
	// next turn, still the same work session, starts with it. Uncached prompt
	// tokens plus completion tokens count: 1200-400+150 = 950 of 1000.
	e := newPhys(t, false, "session_tokens = 1000\nmax_sessions = 3\n",
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{writeResults("w1", goodResults)}, PromptTokens: 1200, CachedTokens: 400, CompletionTokens: 150}),
		wt(fakellm.Turn{Content: "wrapped up", PromptTokens: 10}),
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{reviewCall("r1", "accept", "ok")}}),
		mt(fakellm.Turn{Content: "Done."}),
	)
	w := e.start(e.spec(), nil)
	r := e.final(str(w, "id"))
	if str(r, "state") != "passed" || r["sessions_done"] != float64(1) {
		t.Fatalf("run: %v", r)
	}
	wr := e.requests(workerMark)
	if len(wr) != 2 {
		t.Fatalf("worker requests %v", wr)
	}
	msgs := wire(t, e.fake, wr[1])
	if got := e.lastContent(wr[1]); got != "This work session's budget is nearly spent. Update NOTES.md and RESULTS.txt with where things stand, then stop." {
		t.Errorf("the steered message: %q", got)
	}
	// The steer ends the turn after its tools: the write ran.
	if res := e.toolResults(wr[1])["w1"]; !strings.Contains(res, "RESULTS.txt") {
		t.Errorf("the tool call did not run: %q (%v)", res, msgs)
	}
	lines := strings.Join(strs(e.c.ok("physics.watch", map[string]any{"run": str(w, "id")}), "lines"), "\n")
	if !strings.Contains(lines, "work session 1 ended: 960 tokens") {
		t.Errorf("progress:\n%s", lines)
	}
}

func TestPhysicsSessionLimitInterrupt(t *testing.T) {
	// 125%: the turn is stopped (no budget message first) and the work
	// session ends; it is reviewed like any other.
	e := newPhys(t, false, "session_tokens = 1000\n",
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{writeResults("w1", goodResults)}, PromptTokens: 1300, CompletionTokens: 0}),
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{reviewCall("r1", "accept", "ok")}}),
		mt(fakellm.Turn{Content: "Done."}),
	)
	w := e.start(e.spec(), nil)
	r := e.final(str(w, "id"))
	if str(r, "state") != "failed" || r["sessions_done"] != float64(1) {
		t.Fatalf("run: %v", r)
	}
	if wr := e.requests(workerMark); len(wr) != 1 {
		t.Errorf("the worker made %d requests, want 1 (stopped at 125%%)", len(wr))
	}
	if mr := e.requests(managerMark); len(mr) != 2 {
		t.Errorf("the manager made %d requests, want a review", len(mr))
	}
	lines := strings.Join(strs(e.c.ok("physics.watch", map[string]any{"run": str(w, "id")}), "lines"), "\n")
	if !strings.Contains(lines, "work session 1 ended: 1300 tokens") || !strings.Contains(lines, "review 1: accept") {
		t.Errorf("progress:\n%s", lines)
	}
	log := readFile(t, filepath.Join(str(w, "dir"), "logs", "session-1.md"))
	if !strings.Contains(log, "Write") {
		t.Errorf("session-1.md:\n%s", log)
	}
}

func TestPhysicsTotalTokensScoresWithoutAReview(t *testing.T) {
	// total_tokens spent: the worker gets the budget message, and the run is
	// scored when it stops, with no review (the fake has no manager turns, so
	// a manager request would fail the test).
	e := newPhys(t, false, "total_tokens = 1000\n",
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{writeResults("w1", goodResults)}, PromptTokens: 1000}),
		wt(fakellm.Turn{Content: "stopping"}),
	)
	w := e.start(e.spec(), nil)
	r := e.final(str(w, "id"))
	if str(r, "state") != "passed" {
		t.Fatalf("run: %v", r)
	}
	wr := e.requests(workerMark)
	if len(wr) != 2 || !strings.HasPrefix(e.lastContent(wr[1]), "This work session's budget is nearly spent.") {
		t.Fatalf("worker requests %v", wr)
	}
	if mr := e.requests(managerMark); len(mr) != 0 {
		t.Errorf("a review was asked for: %v", mr)
	}
}

func TestPhysicsHoursScoreWithoutAReview(t *testing.T) {
	// The hours run out while a request is in flight: it is cancelled, the
	// budget message follows, and the run is scored with no review.
	e := newPhys(t, false, "hours = 0.0004\n",
		wt(fakellm.Turn{Hang: true}),
		wt(fakellm.Turn{Content: "stopping"}),
	)
	w := e.start(e.spec(), nil)
	r := e.final(str(w, "id"))
	if str(r, "state") != "failed" {
		t.Fatalf("run: %v", r)
	}
	wr := e.requests(workerMark)
	if len(wr) != 2 || !strings.HasPrefix(e.lastContent(wr[1]), "This work session's budget is nearly spent.") {
		t.Fatalf("worker requests %v", wr)
	}
	if mr := e.requests(managerMark); len(mr) != 0 {
		t.Errorf("a review was asked for: %v", mr)
	}
}

func TestPhysicsOneRunAtATimeAndInterrupted(t *testing.T) {
	e := newPhys(t, false, "",
		wt(fakellm.Turn{Hang: true}),
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{writeResults("w1", goodResults)}}),
		wt(fakellm.Turn{Content: "resumed and done"}),
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{reviewCall("r1", "accept", "ok")}}),
		mt(fakellm.Turn{Content: "Done."}),
	)
	w := e.start(e.spec(), nil)
	id := str(w, "id")
	waitUntil(t, "the worker's request", func() bool { return len(e.requests(workerMark)) == 1 })

	_, err := e.c.call("physics.run", map[string]any{"spec": e.spec()})
	if err == nil || err.Code != -32013 || !strings.Contains(err.Message, id) {
		t.Fatalf("a second run: %+v", err)
	}
	if _, err := e.c.call("physics.resume", map[string]any{"run": id}); err == nil || err.Code != -32602 {
		t.Errorf("resuming a going run: %+v", err)
	}

	e.h.restart()
	e.c = e.h.dial()
	r := e.run(id)
	if str(r, "state") != "interrupted" {
		t.Fatalf("after the daemon stopped: %v", r)
	}
	e.c.ok("physics.resume", map[string]any{"run": id})
	r = e.final(id)
	if str(r, "state") != "passed" || r["worker_session"] != w["worker_session"] {
		t.Fatalf("after resume: %v", r)
	}
	wr := e.requests(workerMark)
	if len(wr) != 3 {
		t.Fatalf("worker requests %v", wr)
	}
	if got := e.lastContent(wr[1]); got != "The run was interrupted; carry on where you left off." {
		t.Errorf("the resume message: %q", got)
	}
	// The same session: the begin message is still first.
	msgs := wire(t, e.fake, wr[1])
	if !strings.HasSuffix(msgs[1]["content"].(string), "\nBegin.") {
		t.Errorf("the transcript no longer starts with the problem: %v", msgs[1])
	}
	if lines := strings.Join(strs(e.c.ok("physics.watch", map[string]any{"run": id}), "lines"), "\n"); !strings.Contains(lines, "interrupted") {
		t.Errorf("progress:\n%s", lines)
	}
	// Only an interrupted run resumes.
	if _, err := e.c.call("physics.resume", map[string]any{"run": id}); err == nil {
		t.Error("resumed a passed run")
	}
}

func TestPhysicsInterruptedDuringAReviewRepeatsIt(t *testing.T) {
	e := newPhys(t, false, "",
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{writeResults("w1", goodResults)}}),
		wt(fakellm.Turn{Content: "done"}),
		mt(fakellm.Turn{Hang: true}),
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{reviewCall("r1", "accept", "ok")}}),
		mt(fakellm.Turn{Content: "Done."}),
	)
	w := e.start(e.spec(), nil)
	id := str(w, "id")
	waitUntil(t, "the manager's request", func() bool { return len(e.requests(managerMark)) == 1 })
	e.h.restart()
	e.c = e.h.dial()
	if r := e.run(id); str(r, "state") != "interrupted" || r["sessions_done"] != float64(1) {
		t.Fatalf("after the daemon stopped: %v", r)
	}
	e.c.ok("physics.resume", map[string]any{"run": id})
	if r := e.final(id); str(r, "state") != "passed" {
		t.Fatalf("after resume: %v", r)
	}
	mr := e.requests(managerMark)
	if len(mr) != 3 {
		t.Fatalf("manager requests %v", mr)
	}
	first, again := e.lastContent(mr[0]), e.lastContent(mr[1])
	if first != again || !strings.HasPrefix(again, "Review of work session 1 of at most 8.") {
		t.Errorf("the review was not repeated:\n%q\n%q", first, again)
	}
	if wr := e.requests(workerMark); len(wr) != 2 {
		t.Errorf("worker requests %v: the work session was not repeated", wr)
	}
}

func TestPhysicsStop(t *testing.T) {
	e := newPhys(t, false, "",
		wt(fakellm.Turn{Hang: true}),
	)
	w := e.start(e.spec(), nil)
	id := str(w, "id")
	waitUntil(t, "the worker's request", func() bool { return len(e.requests(workerMark)) == 1 })
	r := e.c.ok("physics.stop", map[string]any{"run": id})
	if str(r, "state") != "stopped" {
		t.Fatalf("stop: %v", r)
	}
	if eval := readFile(t, filepath.Join(str(w, "dir"), "FORMAL_EVAL.md")); !strings.Contains(eval, "Status: failed") {
		t.Errorf("FORMAL_EVAL.md:\n%s", eval)
	}
	if got := e.run(id); str(got, "state") != "stopped" {
		t.Errorf("run: %v", got)
	}
	// Free again: a new run may start.
	if _, err := e.c.call("physics.stop", map[string]any{"run": id}); err == nil {
		t.Error("stopped a stopped run")
	}
}

func TestPhysicsStopOfAnInterruptedRun(t *testing.T) {
	e := newPhys(t, false, "",
		wt(fakellm.Turn{Hang: true}),
	)
	w := e.start(e.spec(), nil)
	id := str(w, "id")
	waitUntil(t, "the worker's request", func() bool { return len(e.requests(workerMark)) == 1 })
	e.h.restart()
	e.c = e.h.dial()
	if str(e.run(id), "state") != "interrupted" {
		t.Fatal("not interrupted")
	}
	r := e.c.ok("physics.stop", map[string]any{"run": id})
	if str(r, "state") != "stopped" {
		t.Fatalf("stop: %v", r)
	}
	if _, err := os.Stat(filepath.Join(str(w, "dir"), "FORMAL_EVAL.md")); err != nil {
		t.Error("no FORMAL_EVAL.md")
	}
}

func TestPhysicsSessionsAreViewedNotDriven(t *testing.T) {
	e := newPhys(t, false, "",
		wt(fakellm.Turn{Hang: true}),
	)
	w := e.start(e.spec(), nil)
	waitUntil(t, "the worker's request", func() bool { return len(e.requests(workerMark)) == 1 })
	for _, sid := range []string{str(w, "worker_session"), str(w, "manager_session")} {
		e.c.fails("session.attach", map[string]any{"session": sid, "as": "driver"}, -32010)
		res := e.c.ok("session.attach", map[string]any{"session": sid, "as": "viewer"})
		if str(res, "role") != "viewer" || str(obj(res, "session"), "kind") != "physics" {
			t.Errorf("viewer attach: %v", res)
		}
		e.c.fails("turn.send", map[string]any{"session": sid, "text": "hello"}, -32010)
		e.c.fails("turn.interrupt", map[string]any{"session": sid}, -32010)
	}
	// Not in the session lists the terminal shows.
	for _, kinds := range [][]string{nil, {"interactive", "worker"}, {"interactive", "worker", "oneshot", "cron", "chat", "child"}} {
		params := map[string]any{"limit": 100}
		if kinds != nil {
			params["kinds"] = kinds
		}
		for _, s := range objs(e.c.ok("session.list", params), "sessions") {
			if s["kind"] == "physics" {
				t.Errorf("kinds %v list a physics session: %v", kinds, s)
			}
		}
	}
	// The viewer sees the worker's turn live.
	e.c.ok("physics.stop", map[string]any{"run": str(w, "id")})
}

func TestPhysicsLogsAndEventLogWithTheSandbox(t *testing.T) {
	// The worker runs a command in the sandbox: logs/session-N.md lists it
	// with its exit code and the start and end of its output, and EVENT_LOG
	// records the call with the exit code.
	cmd := `i=1; while [ $i -le 100 ]; do echo line$i; i=$((i+1)); done; pwd; (exit 3)`
	e := newPhys(t, true, "",
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{bashCall("w1", cmd), writeResults("w2", goodResults)}}),
		wt(fakellm.Turn{Content: "done"}),
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{reviewCall("r1", "accept", "ok")}}),
		mt(fakellm.Turn{Content: "Done."}),
	)
	w := e.start(e.spec(), nil)
	dir := str(w, "dir")
	if r := e.final(str(w, "id")); str(r, "state") != "passed" {
		t.Fatalf("run: %v", r)
	}
	log := readFile(t, filepath.Join(dir, "logs", "session-1.md"))
	for _, want := range []string{"# Work session 1", "2 tool calls", "1. Bash", cmd, "exit code 3", "line1\n", "line30\n", "[56 lines omitted]", "line87\n", "line100\n" + dir, "2. Write", "RESULTS.txt"} {
		if !strings.Contains(log, want) {
			t.Errorf("session-1.md lacks %q:\n%s", want, log)
		}
	}
	if strings.Contains(log, "line31\n") || strings.Contains(log, "line86\n") {
		t.Errorf("session-1.md holds output past the first 30 and last 15 lines:\n%s", log)
	}
	var sawTerminal bool
	for _, line := range strings.Split(strings.TrimSpace(readFile(t, filepath.Join(dir, "EVENT_LOG.jsonl"))), "\n") {
		var ev map[string]any
		_ = json.Unmarshal([]byte(line), &ev)
		if ev["kind"] == "tool_call" && ev["tool"] == "Bash" {
			sawTerminal = true
			if ev["exit_code"] != float64(3) || ev["agent"] != "worker" || ev["session"] != float64(1) {
				t.Errorf("Bash tool_call: %s", line)
			}
		}
	}
	if !sawTerminal {
		t.Error("no Bash tool_call in EVENT_LOG.jsonl")
	}
}

func TestPhysicsLogClipsLongOutput(t *testing.T) {
	// At most 4,000 characters per call.
	cmd := `i=1; while [ $i -le 20 ]; do echo "` + strings.Repeat("x", 300) + `$i"; i=$((i+1)); done`
	e := newPhys(t, true, "",
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{bashCall("w1", cmd), writeResults("w2", goodResults)}}),
		wt(fakellm.Turn{Content: "done"}),
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{reviewCall("r1", "accept", "ok")}}),
		mt(fakellm.Turn{Content: "Done."}),
	)
	w := e.start(e.spec(), nil)
	e.final(str(w, "id"))
	log := readFile(t, filepath.Join(str(w, "dir"), "logs", "session-1.md"))
	if !strings.Contains(log, "[clipped]") {
		t.Errorf("long output was not clipped:\n%s", log)
	}
	if n := len([]rune(log)); n > 4000+len(cmd)+1500 {
		t.Errorf("session-1.md is %d characters", n)
	}
}

// plant runs f on the run directory (found under the workspace root) at the
// moment the fake builds a worker's tool call, i.e. while the worker works.
func (e *physEnv) plant(f func(dir string)) func() string {
	return func() string {
		ents, _ := os.ReadDir(e.root)
		if len(ents) == 1 {
			f(filepath.Join(e.root, ents[0].Name()))
		}
		return `{"file_path":"RESULTS.txt","content":` + fmt.Sprintf("%q", goodResults) + `}`
	}
}

func outsideDir(t *testing.T) (dir, file string) {
	t.Helper()
	d, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	file = filepath.Join(d, "victim")
	if err := os.WriteFile(file, []byte("OUTSIDE"), 0o644); err != nil {
		t.Fatal(err)
	}
	return d, file
}

func TestPhysicsDaemonWritesStayBeneathTheRunDirectory(t *testing.T) {
	// While the worker works it replaces EVENT_LOG.jsonl, REVIEWS.md,
	// FORMAL_EVAL.md and logs/ with symlinks to outside the run directory.
	// The daemon's writes are refused (they land nowhere outside), the run
	// still finishes and is scored.
	out, victim := outsideDir(t)
	var e *physEnv
	plant := func(dir string) {
		for _, name := range []string{"EVENT_LOG.jsonl", "REVIEWS.md", "FORMAL_EVAL.md"} {
			_ = os.Remove(filepath.Join(dir, name))
			_ = os.Symlink(victim, filepath.Join(dir, name))
		}
		_ = os.RemoveAll(filepath.Join(dir, "logs"))
		_ = os.Symlink(out, filepath.Join(dir, "logs"))
	}
	e = newPhys(t, false, "",
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{{ID: "w1", Name: "Write", ArgumentsFunc: func() string { return e.plant(plant)() }}}}),
		wt(fakellm.Turn{Content: "done"}),
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{reviewCall("r1", "accept", "ok")}}),
		mt(fakellm.Turn{Content: "Done."}),
	)
	w := e.start(e.spec(), nil)
	r := e.final(str(w, "id"))
	if str(r, "state") != "passed" {
		t.Fatalf("run: %v", r)
	}
	if b, _ := os.ReadFile(victim); string(b) != "OUTSIDE" {
		t.Errorf("the outside file was changed: %q", b)
	}
	if ents, _ := os.ReadDir(out); len(ents) != 1 {
		t.Errorf("the outside directory gained entries: %v", ents)
	}
	lines := strings.Join(strs(e.c.ok("physics.watch", map[string]any{"run": str(w, "id")}), "lines"), "\n")
	for _, want := range []string{"logs/session-1.md was not written", "REVIEWS.md was not written", "FORMAL_EVAL.md was not written"} {
		if !strings.Contains(lines, want) {
			t.Errorf("progress lacks %q:\n%s", want, lines)
		}
	}
}

func TestPhysicsResultsSymlinkIsUnreadable(t *testing.T) {
	// RESULTS.txt is a symlink to a file outside that would pass the checks:
	// it is treated as missing, its content reaches no request, and the run
	// does not pass.
	_, _ = outsideDir(t)
	out, _ := outsideDir(t)
	secret := filepath.Join(out, "results")
	if err := os.WriteFile(secret, []byte("centroid_a = 4217.8\nwidth_b = 88.27\n# SECRETMARK\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var e *physEnv
	plant := func(dir string) { _ = os.Symlink(secret, filepath.Join(dir, "RESULTS.txt")) }
	e = newPhys(t, false, "",
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{{ID: "w1", Name: "Read", ArgumentsFunc: func() string { e.plant(plant)(); return `{"file_path":"PROBLEM.md"}` }}}}),
		wt(fakellm.Turn{Content: "done"}),
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{reviewCall("r1", "accept", "ok")}}),
		mt(fakellm.Turn{Content: "Done."}),
	)
	w := e.start(e.spec(), nil)
	r := e.final(str(w, "id"))
	if str(r, "state") != "failed" {
		t.Fatalf("run: %v", r)
	}
	if got := e.lastContent(e.requests(managerMark)[0]); !strings.Contains(got, "RESULTS.txt: missing.") {
		t.Errorf("the review message: %q", got)
	}
	for i, b := range e.fake.Bodies() {
		if bytes.Contains(b, []byte("SECRETMARK")) {
			t.Errorf("request %d carries the outside file", i)
		}
	}
}

func TestPhysicsManagerCannotReadOrSearchThroughASymlink(t *testing.T) {
	out, victim := outsideDir(t)
	if err := os.WriteFile(victim, []byte("OUTSIDE-SECRET"), 0o644); err != nil {
		t.Fatal(err)
	}
	var e *physEnv
	plant := func(dir string) {
		_ = os.Symlink(victim, filepath.Join(dir, "filelink"))
		_ = os.Symlink(out, filepath.Join(dir, "dirlink"))
	}
	e = newPhys(t, true, "",
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{{ID: "w1", Name: "Write", ArgumentsFunc: func() string { return e.plant(plant)() }}}}),
		wt(fakellm.Turn{Content: "done"}),
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{
			tcall("m1", "Read", map[string]any{"file_path": "filelink"}),
			tcall("m2", "Read", map[string]any{"file_path": "dirlink/victim"}),
			tcall("m3", "Grep", map[string]any{"output_mode": "content", "pattern": "OUTSIDE", "path": "dirlink"}),
			tcall("m4", "Grep", map[string]any{"output_mode": "content", "pattern": "OUTSIDE-SECRET"}),
			tcall("m5", "Read", map[string]any{"file_path": "RESULTS.txt"}),
			reviewCall("m6", "accept", "ok"),
		}}),
		mt(fakellm.Turn{Content: "Done."}),
	)
	w := e.start(e.spec(), nil)
	if r := e.final(str(w, "id")); str(r, "state") != "passed" {
		t.Fatalf("run: %v", r)
	}
	res := e.toolResults(e.requests(managerMark)[1])
	for _, id := range []string{"m1", "m2", "m3", "m4"} {
		if strings.Contains(res[id], "OUTSIDE-SECRET") || strings.Contains(res[id], "OUTSIDE\"") {
			t.Errorf("%s returned the outside file: %q", id, res[id])
		}
	}
	for _, id := range []string{"m1", "m2", "m3"} {
		if !strings.Contains(res[id], "outside") && !strings.Contains(res[id], "leaves") && !strings.Contains(res[id], "error") {
			t.Errorf("%s was not refused: %q", id, res[id])
		}
	}
	if !strings.Contains(res["m5"], "centroid_a") {
		t.Errorf("the manager cannot read inside the run directory: %q", res["m5"])
	}
	for i, b := range e.fake.Bodies() {
		if bytes.Contains(b, []byte("OUTSIDE-SECRET")) && i > 0 {
			// Only the manager's own search pattern may carry the word.
			if !bytes.Contains(b, []byte(`"pattern":"OUTSIDE-SECRET"`)) && !strings.Contains(string(b), `OUTSIDE-SECRET\"`) {
				t.Errorf("request %d carries the outside file", i)
			}
		}
	}
}

func TestPhysicsDataIsReadableAtTheNamedAndTheResolvedPath(t *testing.T) {
	var e *physEnv
	link := filepath.Join(t.TempDir(), "named", "YAP-Final")
	if err := os.MkdirAll(filepath.Dir(link), 0o755); err != nil {
		t.Fatal(err)
	}
	cmd := "cat %s/events.root; echo; echo x > %s/w; echo rc=$?"
	e = newPhys(t, true, "",
		wt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{
			{ID: "w1", Name: "Bash", ArgumentsFunc: func() string {
				b, _ := json.Marshal(map[string]any{"command": fmt.Sprintf(cmd, link, link) + "; " + fmt.Sprintf(cmd, e.dataDir, e.dataDir)})
				return string(b)
			}},
			{ID: "w2", Name: "Read", ArgumentsFunc: func() string { return `{"file_path":` + fmt.Sprintf("%q", link+"/events.root") + `}` }},
			{ID: "w3", Name: "Read", ArgumentsFunc: func() string {
				return `{"file_path":` + fmt.Sprintf("%q", e.dataDir+"/events.root") + `}`
			}},
			{ID: "w4", Name: "Write", ArgumentsFunc: func() string {
				return `{"file_path":` + fmt.Sprintf("%q", link+"/new.txt") + `,"content":"x"}`
			}},
			{ID: "w5", Name: "Write", ArgumentsFunc: func() string {
				return `{"file_path":` + fmt.Sprintf("%q", e.dataDir+"/new.txt") + `,"content":"x"}`
			}},
		}}),
		wt(fakellm.Turn{Content: "done"}),
		mt(fakellm.Turn{ToolCalls: []fakellm.ToolCall{reviewCall("r1", "accept", "ok")}}),
		mt(fakellm.Turn{Content: "Done."}),
	)
	if err := os.Symlink(e.dataDir, link); err != nil {
		t.Fatal(err)
	}
	spec := strings.Replace(e.spec(), filepath.Join(e.dataDir, "events.root"), link, 1)
	w := e.start(spec, nil)
	e.final(str(w, "id"))
	wr := e.requests(workerMark)
	res := e.toolResults(wr[1])
	var term struct {
		Output string `json:"output"`
	}
	_ = json.Unmarshal([]byte(res["w1"]), &term)
	if strings.Count(term.Output, "DATA") != 2 || strings.Count(term.Output, "rc=1") != 2 {
		t.Errorf("Bash at both paths: %q", res["w1"])
	}
	for _, id := range []string{"w2", "w3"} {
		if !strings.Contains(res[id], "DATA") {
			t.Errorf("Read %s: %q", id, res[id])
		}
	}
	for _, id := range []string{"w4", "w5"} {
		if !strings.Contains(res[id], "error") {
			t.Errorf("Write %s was not refused: %q", id, res[id])
		}
	}
	if _, err := os.Stat(filepath.Join(e.dataDir, "new.txt")); err == nil {
		t.Error("wrote into the data directory")
	}
	// The prompt names the data as the spec does.
	if sys := string(e.fake.Messages(wr[0])[0]); !strings.Contains(sys, "mounted read-only at "+link+",") {
		t.Errorf("the worker prompt does not name %s", link)
	}
}

func TestPhysicsSymlinkedDataOutsideDataDirsIsRefused(t *testing.T) {
	e := newPhys(t, false, "")
	other := t.TempDir()
	if err := os.WriteFile(filepath.Join(other, "events.root"), []byte("X"), 0o644); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(e.dataDir, "sneaky.root") // named inside data_dirs, points outside
	if err := os.Symlink(filepath.Join(other, "events.root"), link); err != nil {
		t.Fatal(err)
	}
	spec := strings.Replace(e.spec(), filepath.Join(e.dataDir, "events.root"), link, 1)
	_, err := e.c.call("physics.run", map[string]any{"spec": spec})
	if err == nil || !strings.Contains(err.Message, "not under physics.data_dirs") {
		t.Fatalf("got %+v", err)
	}
}
