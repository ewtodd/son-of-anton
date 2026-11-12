package tools_test

// Background commands: Bash with run_in_background, BashOutput and KillShell.

import (
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

func startBG(t *testing.T, f *fixture, command string) (string, int) {
	t.Helper()
	r := f.call(t, "Bash", map[string]any{"command": command, "run_in_background": true})
	id := str(r, "bash_id")
	if !strings.HasPrefix(id, "bash_") || num(r, "pid") <= 0 || str(r, "status") != "running" {
		t.Fatalf("background start = %v", r)
	}
	return id, num(r, "pid")
}

func alive(pid int) bool { return syscall.Kill(pid, 0) == nil }

// exits signals each background exit in f's session, so a test can wait for
// one without reading its output (BashOutput would use it up).
func exits(f *fixture) <-chan struct{} {
	ch := make(chan struct{}, 16)
	f.env.State.SetOnExit(func() { ch <- struct{}{} })
	return ch
}

func waitFor(t *testing.T, ch <-chan struct{}) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(10 * time.Second):
		t.Fatal("the background command never exited")
	}
}

// drain reads a background command with BashOutput until it has ended, and
// returns the last result and everything it printed.
func drain(t *testing.T, f *fixture, id string) (map[string]any, string) {
	t.Helper()
	var all []string
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		r := f.call(t, "BashOutput", map[string]any{"bash_id": id})
		if out := str(r, "output"); out != "" {
			all = append(all, out)
		}
		if str(r, "status") != "running" {
			return r, strings.Join(all, "\n")
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("%s never ended", id)
	return nil, ""
}

func TestBackgroundRunsInTheSessionsShell(t *testing.T) {
	f := setup(t, nil)
	f.call(t, "Bash", map[string]any{"command": "export SOA_BG=inherited"})
	id, _ := startBG(t, f, `echo "$SOA_BG"; pwd`)
	r, out := drain(t, f, id)
	if str(r, "status") != "completed" || num(r, "exit_code") != 0 {
		t.Fatalf("end = %v", r)
	}
	if !strings.Contains(out, "inherited") || !strings.Contains(out, f.cwd) {
		t.Fatalf("a background command gets the session's exports and cwd: %q", out)
	}
}

func TestBashOutputShowsOnlyWhatIsNew(t *testing.T) {
	f := setup(t, nil)
	id, _ := startBG(t, f, "echo one; while [ ! -e go ]; do sleep 0.05; done; echo two; exit 4")
	deadline := time.Now().Add(10 * time.Second)
	var r map[string]any
	for time.Now().Before(deadline) {
		if r = f.call(t, "BashOutput", map[string]any{"bash_id": id}); str(r, "output") != "" {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if str(r, "output") != "one" || str(r, "status") != "running" || r["exit_code"] != nil {
		t.Fatalf("first look = %v", r)
	}
	if r := f.call(t, "BashOutput", map[string]any{"bash_id": id}); str(r, "output") != "" || str(r, "status") != "running" {
		t.Fatalf("a second look with nothing new = %v", r)
	}
	f.write(t, "go", "")
	r, out := drain(t, f, id)
	if out != "two" || str(r, "status") != "failed" || num(r, "exit_code") != 4 {
		t.Fatalf("after the exit: %v, output %q", r, out)
	}
}

func TestBashOutputFilterKeepsMatchingLinesAndPassesTheRestOver(t *testing.T) {
	f := setup(t, nil)
	ended := exits(f)
	id, _ := startBG(t, f, `printf 'error: a\ninfo: b\nerror: c\n'`)
	waitFor(t, ended)
	r := f.call(t, "BashOutput", map[string]any{"bash_id": id, "filter": "^error"})
	if str(r, "output") != "error: a\nerror: c" || str(r, "status") != "completed" {
		t.Fatalf("filtered = %v", r)
	}
	if r := f.call(t, "BashOutput", map[string]any{"bash_id": id}); str(r, "output") != "" {
		t.Fatalf("the lines the filter passed over came back: %v", r)
	}
	if r := f.call(t, "BashOutput", map[string]any{"bash_id": id, "filter": "("}); !strings.Contains(str(r, "error"), "regular expression") {
		t.Fatalf("a bad filter = %v", r)
	}
}

func TestKillShellTakesTheWholeProcessGroup(t *testing.T) {
	f := setup(t, nil)
	id, pid := startBG(t, f, "sleep 30 & sleep 30 & wait")
	time.Sleep(200 * time.Millisecond)
	if k := f.call(t, "KillShell", map[string]any{"shell_id": id}); str(k, "status") != "killed" {
		t.Fatalf("kill = %v", k)
	}
	time.Sleep(300 * time.Millisecond)
	if syscall.Kill(-pid, 0) == nil {
		t.Fatal("children of a killed background command must die with it")
	}
	if r := f.call(t, "BashOutput", map[string]any{"bash_id": id}); str(r, "status") != "killed" {
		t.Fatalf("a killed command reads as killed: %v", r)
	}
	if k := f.call(t, "KillShell", map[string]any{"shell_id": id}); str(k, "status") != "already_exited" {
		t.Fatalf("second kill = %v", k)
	}
}

// process list had no counterpart; a BashOutput or KillShell for an id the
// session does not have names the ones it does.
func TestShellsBelongToTheirSessionAndAnUnknownIdListsThem(t *testing.T) {
	f := setup(t, nil)
	other := setup(t, nil)
	mine, _ := startBG(t, f, "sleep 5")
	theirs, _ := startBG(t, other, "sleep 5")
	r := f.call(t, "BashOutput", map[string]any{"bash_id": theirs})
	if e := str(r, "error"); !strings.Contains(e, "no background shell") || !strings.Contains(e, mine) || !strings.Contains(e, "sleep 5") || strings.Contains(e, theirs+" (") {
		t.Fatalf("another session's command must not be reachable, and the error lists this one's: %v", r)
	}
	if r := f.call(t, "KillShell", map[string]any{"shell_id": theirs}); !isErr(r) {
		t.Fatalf("KillShell of another session's command: %v", r)
	}
	if r := other.call(t, "BashOutput", map[string]any{"bash_id": theirs}); str(r, "status") != "running" {
		t.Fatalf("the other session's command was touched: %v", r)
	}
	if r := setup(t, nil).call(t, "BashOutput", map[string]any{"bash_id": "bash_nope"}); !strings.Contains(str(r, "error"), "started none") {
		t.Fatalf("a session with no shells: %v", r)
	}
}

func TestUniquePrefixesResolve(t *testing.T) {
	f := setup(t, nil)
	id, _ := startBG(t, f, "true")
	short := strings.TrimPrefix(id, "bash_")[:6]
	if r := f.call(t, "BashOutput", map[string]any{"bash_id": short}); str(r, "bash_id") != id {
		t.Fatalf("prefix %q: %v", short, r)
	}
	if r := f.call(t, "BashOutput", map[string]any{"bash_id": ""}); !isErr(r) {
		t.Fatalf("no id: %v", r)
	}
}

func TestOutputKeepsTheMostRecent200K(t *testing.T) {
	f := setup(t, nil)
	ended := exits(f)
	id, _ := startBG(t, f, `head -c 500000 /dev/zero | tr '\0' a; printf 'END'`)
	waitFor(t, ended)
	r := f.call(t, "BashOutput", map[string]any{"bash_id": id})
	out := str(r, "output")
	if len(out) > 52_000 || !strings.Contains(out, "output cut") || !strings.HasSuffix(out, "END") {
		t.Fatalf("buffered output: %d chars, ends %q", len(out), out[max(0, len(out)-10):])
	}
	if note := str(r, "note"); !strings.Contains(note, "300003 bytes") || !strings.Contains(note, "dropped") {
		t.Fatalf("the bytes dropped unread must be counted: %v", r["note"])
	}
}

func TestBackgroundCommandsGoThroughTheGate(t *testing.T) {
	f := setup(t, nil) // default mode, nobody to ask
	r := f.call(t, "Bash", map[string]any{"command": "rm -r somewhere", "run_in_background": true})
	if !strings.Contains(str(r, "error"), "BLOCKED") || str(r, "bash_id") != "" {
		t.Fatalf("a dangerous background command with nobody to ask: %v", r)
	}
}

func TestNoReceiptWithoutAsyncDelivery(t *testing.T) {
	f := setup(t, approval.NewGate(approval.Yolo, nil, nil))
	ended := exits(f)
	r := f.call(t, "Bash", map[string]any{"command": "true", "run_in_background": true})
	if !strings.Contains(str(r, "note"), "Nothing can tell you when it ends") || str(r, "bash_id") == "" {
		t.Fatalf("no async delivery: the command runs and the result says to check on it: %v", r)
	}
	waitFor(t, ended)
	if n := f.env.State.TakeNotifications(); len(n) != 0 {
		t.Fatalf("no receipts without async delivery: %v", n)
	}
}

// Every background command ends in a receipt where one can be delivered,
// unless its end was read with BashOutput or it was killed.
func TestReceiptsArriveUnlessTheEndWasRead(t *testing.T) {
	f := setup(t, nil)
	f.env.AsyncDelivery = true
	ended := exits(f)
	read, _ := startBG(t, f, "echo consumed")
	waitFor(t, ended)
	f.call(t, "BashOutput", map[string]any{"bash_id": read})
	killed, _ := startBG(t, f, "sleep 30")
	f.call(t, "KillShell", map[string]any{"shell_id": killed})
	waitFor(t, ended)
	unseen, _ := startBG(t, f, "echo finished-alone; exit 2")
	waitFor(t, ended)
	n := f.env.State.TakeNotifications()
	if len(n) != 1 || !strings.Contains(n[0], "[background receipt] process "+unseen+" exited with code 2") || !strings.Contains(n[0], "finished-alone") {
		t.Fatalf("receipts = %q", n)
	}
	if again := f.env.State.TakeNotifications(); len(again) != 0 {
		t.Fatalf("TakeNotifications drains: %v", again)
	}
}

func TestClosingTheStateKillsItsProcesses(t *testing.T) {
	f := setup(t, nil)
	_, pid := startBG(t, f, "sleep 30")
	if err := f.env.State.Close(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(300 * time.Millisecond)
	if alive(pid) {
		t.Fatal("closing a session's state must kill its background processes")
	}
}

func TestTheNewToolsAreBuiltin(t *testing.T) {
	var names []string
	for _, s := range tools.NewRegistry(tools.Builtin()...).Schemas() {
		names = append(names, s.Function.Name)
	}
	// Relative order only: later phases add tools.
	joined := "," + strings.Join(names, ",") + ","
	last := -1
	for _, want := range []string{"Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "Glob", "Grep", "TodoWrite", "AskUserQuestion"} {
		i := strings.Index(joined, ","+want+",")
		if i <= last {
			t.Fatalf("builtin tools = %v; missing or out of order: %s", names, want)
		}
		last = i
	}
}
