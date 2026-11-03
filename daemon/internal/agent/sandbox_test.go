package agent_test

// Phase 11b: a session in the physics sandbox. Commands run inside bubblewrap
// with the run directory writable and the data read-only; the file tools reach
// only those roots; yolo mode with nobody to ask, hardline rules still
// refusing; and nothing runs when the sandbox is unavailable. The tests that
// need bwrap skip, saying why, where it cannot create its namespaces.

import (
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/sandbox"
	"github.com/ewtodd/son-of-anton/daemon/internal/sandbox/sandboxtest"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// world is a run directory, a data directory and a directory outside both
// (standing in for a home), with a secret in the last.
type world struct {
	run, data, outside, runtime string
}

func newWorld(t *testing.T, runtime string) world {
	t.Helper()
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	w := world{run: filepath.Join(root, "run"), data: filepath.Join(root, "data"), outside: filepath.Join(root, "home"), runtime: runtime}
	for _, d := range []string{w.run, w.data, w.outside} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(w.outside, "secret"), []byte("TOPSECRET"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(w.data, "events.txt"), []byte("DATAFILE"), 0o644); err != nil {
		t.Fatal(err)
	}
	return w
}

func (w world) policy() *sandbox.Policy {
	return &sandbox.Policy{RunDir: w.run, Data: []string{w.data}, Runtime: w.runtime, Home: filepath.Join(w.run, ".home")}
}

// sandboxRunner is a runner whose sessions are sandboxed, given every builtin
// tool (the session must offer only its own set).
func sandboxRunner(t *testing.T, st *store.Store, fake *fakellm.Server, w world) *agent.Runner {
	t.Helper()
	r := runner(st, fake, "sys", nil)
	r.Tools = tools.NewRegistry(tools.Builtin()...)
	r.Sandbox = w.policy()
	return r
}

func tc(id, name string, args map[string]any) fakellm.ToolCall {
	b, _ := json.Marshal(args)
	return call(id, name, string(b))
}

// runCalls runs the calls, one after the other, in one sandboxed session and
// returns each call's result text. The session is closed afterwards.
func runCalls(t *testing.T, w world, calls ...fakellm.ToolCall) []string {
	t.Helper()
	turns := make([]fakellm.Turn, 0, len(calls)+1)
	for _, c := range calls {
		turns = append(turns, fakellm.Turn{ToolCalls: []fakellm.ToolCall{c}})
	}
	turns = append(turns, fakellm.Turn{Content: "done"})
	fake := fakellm.New(t, turns...)
	s, err := sandboxRunner(t, newStore(t), fake, w).NewSession(ctx, "interactive", w.run)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if res := send(t, s, "go"); res.Content != "done" {
		t.Fatalf("result %+v", res)
	}
	var out []string
	for _, m := range full(t, fake.Messages(len(calls))) {
		if m.Role == "tool" {
			out = append(out, m.Content)
		}
	}
	if len(out) != len(calls) {
		t.Fatalf("%d results for %d calls: %q", len(out), len(calls), out)
	}
	return out
}

func term(id, command string) fakellm.ToolCall {
	return tc(id, "terminal", map[string]any{"command": command})
}

// shellOut is the output of a terminal result.
func shellOut(t *testing.T, result string) string {
	t.Helper()
	var r struct {
		Output string `json:"output"`
		Error  string `json:"error"`
	}
	if err := json.Unmarshal([]byte(result), &r); err != nil {
		t.Fatalf("result %q: %v", result, err)
	}
	return r.Output + r.Error
}

func TestSandboxedTerminalConfinesTheCommand(t *testing.T) {
	rt := sandboxtest.Runtime(t)
	w := newWorld(t, rt)

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	var accepted atomic.Int32
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			accepted.Add(1)
			c.Close()
		}
	}()
	port := ln.Addr().(*net.TCPAddr).Port

	res := runCalls(t, w,
		term("c1", "echo hello > made.txt && cat made.txt"),
		term("c2", "echo x > "+w.data+"/new.txt; echo rc=$?"),
		term("c3", "cat "+w.outside+"/secret; echo rc=$?"),
		term("c4", fmt.Sprintf("exec 3<>/dev/tcp/127.0.0.1/%d; echo rc=$?", port)),
		term("c5", "echo $PATH"),
		term("c6", "mkdir sub && cd sub && export KEPT=yes"),
		term("c7", "pwd; echo $KEPT"),
		term("c8", "cat "+w.data+"/events.txt"),
	)
	if got := shellOut(t, res[0]); got != "hello" {
		t.Errorf("write in the run dir: %q", got)
	}
	if b, err := os.ReadFile(filepath.Join(w.run, "made.txt")); err != nil || string(b) != "hello\n" {
		t.Errorf("made.txt on the host: %q, %v", b, err)
	}
	if got := shellOut(t, res[1]); strings.Contains(got, "rc=0") {
		t.Errorf("writing in the data dir succeeded: %q", got)
	}
	if _, err := os.Stat(filepath.Join(w.data, "new.txt")); err == nil {
		t.Error("a file appeared in the data dir")
	}
	if got := shellOut(t, res[2]); strings.Contains(got, "TOPSECRET") || strings.Contains(got, "rc=0") {
		t.Errorf("read outside the binds: %q", got)
	}
	if got := shellOut(t, res[3]); strings.Contains(got, "rc=0") {
		t.Errorf("connected to the host's loopback: %q", got)
	}
	time.Sleep(100 * time.Millisecond)
	if accepted.Load() != 0 {
		t.Error("the listener on the host saw a connection from the sandbox")
	}
	if got := shellOut(t, res[4]); got != rt {
		t.Errorf("PATH = %q, want the runtime %q", got, rt)
	}
	if got := shellOut(t, res[6]); got != filepath.Join(w.run, "sub")+"\nyes" {
		t.Errorf("cd and export across calls: %q", got)
	}
	if got := shellOut(t, res[7]); got != "DATAFILE" {
		t.Errorf("reading the data dir: %q", got)
	}
}

func TestSandboxedBackgroundProcessDiesWithTheSession(t *testing.T) {
	rt := sandboxtest.Runtime(t)
	w := newWorld(t, rt)
	const marker = "987654.25"
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{tc("c1", "terminal", map[string]any{"command": "echo up > bg.txt; sleep " + marker, "background": true})}},
		fakellm.Turn{Content: "started"},
	)
	s, err := sandboxRunner(t, newStore(t), fake, w).NewSession(ctx, "interactive", w.run)
	if err != nil {
		t.Fatal(err)
	}
	send(t, s, "go")
	deadline := time.Now().Add(5 * time.Second)
	for !hostHas(marker) || !exists(filepath.Join(w.run, "bg.txt")) {
		if time.Now().After(deadline) {
			t.Fatal("the background process did not run in the sandbox")
		}
		time.Sleep(20 * time.Millisecond)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	deadline = time.Now().Add(5 * time.Second)
	for hostHas(marker) {
		if time.Now().After(deadline) {
			t.Fatal("the background process outlived the session")
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func exists(p string) bool { _, err := os.Stat(p); return err == nil }

// hostHas reports whether a process on the host has marker in its command line.
func hostHas(marker string) bool {
	ents, _ := filepath.Glob("/proc/[0-9]*/cmdline")
	for _, e := range ents {
		if b, err := os.ReadFile(e); err == nil && strings.Contains(string(b), "sleep\x00"+marker) {
			return true
		}
	}
	return false
}

func TestSandboxedFileToolsReachOnlyTheRoots(t *testing.T) {
	rt := sandboxtest.Runtime(t)
	w := newWorld(t, rt)
	if err := os.Symlink(w.outside, filepath.Join(w.run, "out")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(w.outside, "secret"), filepath.Join(w.run, "secret-link")); err != nil {
		t.Fatal(err)
	}
	res := runCalls(t, w,
		tc("c1", "read_file", map[string]any{"path": filepath.Join(w.outside, "secret")}),
		tc("c2", "write_file", map[string]any{"path": filepath.Join(w.data, "new.txt"), "content": "x"}),
		tc("c3", "read_file", map[string]any{"path": filepath.Join(w.run, "secret-link")}),
		tc("c4", "read_file", map[string]any{"path": filepath.Join(w.run, "out", "secret")}),
		tc("c5", "write_file", map[string]any{"path": filepath.Join(w.run, "out", "planted.txt"), "content": "x"}),
		tc("c6", "patch", map[string]any{"path": filepath.Join(w.data, "events.txt"), "old_string": "DATAFILE", "new_string": "changed"}),
		tc("c7", "search_files", map[string]any{"pattern": "TOPSECRET", "path": w.outside}),
		tc("c8", "read_file", map[string]any{"path": filepath.Join(w.data, "events.txt")}),
		tc("c9", "write_file", map[string]any{"path": "notes/a.txt", "content": "inside"}),
		tc("c10", "search_files", map[string]any{"pattern": "TOPSECRET", "path": filepath.Join(w.run, "out")}),
	)
	for i := 0; i < 7; i++ {
		if !strings.Contains(res[i], `"error"`) {
			t.Errorf("call c%d was not refused: %s", i+1, res[i])
		}
		if strings.Contains(res[i], "TOPSECRET") {
			t.Errorf("call c%d leaked the secret: %s", i+1, res[i])
		}
	}
	// A refusal names the allowed roots.
	for _, root := range []string{w.run, w.data, "/nix/store"} {
		if !strings.Contains(res[0], root) {
			t.Errorf("the refusal does not name %s: %s", root, res[0])
		}
	}
	if exists(filepath.Join(w.data, "new.txt")) || exists(filepath.Join(w.outside, "planted.txt")) {
		t.Error("a write got through")
	}
	if b, _ := os.ReadFile(filepath.Join(w.data, "events.txt")); string(b) != "DATAFILE" {
		t.Errorf("the data file changed: %q", b)
	}
	if !strings.Contains(res[7], "DATAFILE") {
		t.Errorf("reading the data dir: %s", res[7])
	}
	if b, err := os.ReadFile(filepath.Join(w.run, "notes", "a.txt")); err != nil || string(b) != "inside" {
		t.Errorf("writing in the run dir: %q, %v (%s)", b, err, res[8])
	}
	if strings.Contains(res[9], "TOPSECRET") {
		t.Errorf("search followed a symlink out: %s", res[9])
	}
}

func TestSandboxedSessionOffersTheSandboxToolsOnly(t *testing.T) {
	// No bwrap needed: the tool list is the session's, not the sandbox's.
	w := newWorld(t, t.TempDir())
	fake := fakellm.New(t, fakellm.Turn{Content: "hi"})
	s, err := sandboxRunner(t, newStore(t), fake, w).NewSession(ctx, "interactive", w.run)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	send(t, s, "go")
	var names []string
	for _, tl := range fake.Request(0)["tools"].([]any) {
		names = append(names, tl.(map[string]any)["function"].(map[string]any)["name"].(string))
	}
	if got, want := strings.Join(names, ","), "terminal,process,read_file,write_file,patch,search_files,todo"; got != want {
		t.Errorf("tools = %s, want %s", got, want)
	}
}

func TestSandboxUnavailableRunsNothing(t *testing.T) {
	w := newWorld(t, t.TempDir())
	t.Setenv("PATH", t.TempDir()) // no bwrap
	res := runCalls(t, w,
		term("c1", "touch "+filepath.Join(w.run, "ran.txt")),
		tc("c2", "terminal", map[string]any{"command": "touch " + filepath.Join(w.run, "bg.txt"), "background": true}),
	)
	for i, r := range res {
		if !strings.Contains(r, "sandbox is unavailable") {
			t.Errorf("call c%d: %s", i+1, r)
		}
	}
	time.Sleep(100 * time.Millisecond)
	if exists(filepath.Join(w.run, "ran.txt")) || exists(filepath.Join(w.run, "bg.txt")) {
		t.Error("a command ran although the sandbox is unavailable")
	}
}

func TestSandboxedSessionIsYoloButHardlineStillRefuses(t *testing.T) {
	rt := sandboxtest.Runtime(t)
	w := newWorld(t, rt)
	const dangerous = "rm -rf scratch"
	if len(approval.Classify(dangerous)) == 0 {
		t.Fatal("the test needs a command the gate calls dangerous")
	}
	if err := os.MkdirAll(filepath.Join(w.run, "scratch", "deep"), 0o755); err != nil {
		t.Fatal(err)
	}
	res := runCalls(t, w,
		term("c1", dangerous+"; echo gone"),
		term("c2", "rm -rf / ; echo ran"),
	)
	if got := shellOut(t, res[0]); got != "gone" || exists(filepath.Join(w.run, "scratch")) {
		t.Errorf("the dangerous command was not run without asking: %s", res[0])
	}
	if !strings.Contains(res[1], "BLOCKED (hardline)") || strings.Contains(res[1], "ran") {
		t.Errorf("the hardline command was not refused: %s", res[1])
	}
}

// search_files runs ripgrep inside the sandbox: a file outside the roots,
// reached through a symlink in the run directory, is never reported, and a
// match in the run directory is.
func TestSandboxedSearchRunsInsideTheSandbox(t *testing.T) {
	rt := sandboxtest.Runtime(t)
	w := newWorld(t, rt)
	if rg, err := exec.LookPath("rg"); err != nil || !strings.HasPrefix(rg, "/nix/store/") {
		t.Skipf("ripgrep (%q, %v) is not in the Nix store, so it cannot run in the sandbox here", rg, err)
	}
	if err := os.Symlink(w.outside, filepath.Join(w.run, "out")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(w.outside, "secret"), filepath.Join(w.run, "secret-link")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(w.run, "mine.txt"), []byte("TOPSECRET-not\nneedle here\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	res := runCalls(t, w,
		tc("c1", "search_files", map[string]any{"pattern": "TOPSECRET", "path": w.run}),
		tc("c2", "search_files", map[string]any{"pattern": "needle", "path": w.run}),
		tc("c3", "search_files", map[string]any{"pattern": "*", "target": "files", "path": w.run}),
		tc("c4", "search_files", map[string]any{"pattern": "DATAFILE", "path": w.data}),
	)
	if strings.Contains(res[0], "secret") || strings.Contains(res[0], "out/") || strings.Count(res[0], "TOPSECRET") != 1 {
		t.Errorf("search reached outside the roots (only mine.txt may match): %s", res[0])
	}
	if !strings.Contains(res[1], "mine.txt") || strings.Contains(res[1], `"error"`) {
		t.Errorf("search inside the run dir: %s", res[1])
	}
	if strings.Contains(res[2], "victim") || strings.Contains(res[2], "/home/") {
		t.Errorf("file search reached outside: %s", res[2])
	}
	if !strings.Contains(res[3], "events.txt") {
		t.Errorf("search in the data dir: %s", res[3])
	}
}
