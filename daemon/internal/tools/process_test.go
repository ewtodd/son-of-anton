package tools_test

// Background processes (phase 1b): terminal(background) and the process tool.

import (
	"context"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

func startBG(t *testing.T, f *fixture, command string, extra map[string]any) string {
	t.Helper()
	args := map[string]any{"command": command, "background": true}
	for k, v := range extra {
		args[k] = v
	}
	r := f.call(t, "terminal", args)
	id := str(r, "session_id")
	if !strings.HasPrefix(id, "proc_") || num(r, "pid") <= 0 {
		t.Fatalf("background start = %v", r)
	}
	return id
}

func alive(pid int) bool { return syscall.Kill(pid, 0) == nil }

func waitExit(t *testing.T, f *fixture, id string) map[string]any {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if r := f.call(t, "process", map[string]any{"action": "poll", "session_id": id}); str(r, "status") == "exited" {
			return r
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("process %s never exited", id)
	return nil
}

func TestBackgroundRunsInTheSessionsShell(t *testing.T) {
	f := setup(t, nil)
	f.call(t, "terminal", map[string]any{"command": "export SOA_BG=inherited"})
	id := startBG(t, f, `echo "$SOA_BG"; pwd`, nil)
	r := f.call(t, "process", map[string]any{"action": "wait", "session_id": id, "timeout": 10})
	if str(r, "status") != "exited" || num(r, "exit_code") != 0 {
		t.Fatalf("wait = %v", r)
	}
	if out := str(r, "output"); !strings.Contains(out, "inherited") || !strings.Contains(out, f.cwd) {
		t.Fatalf("a background command gets the session's exports and cwd: %q", out)
	}
}

func TestPollLogAndExitCode(t *testing.T) {
	f := setup(t, nil)
	id := startBG(t, f, "printf 'one\\ntwo\\nthree\\n'; exit 4", nil)
	r := waitExit(t, f, id)
	if num(r, "exit_code") != 4 {
		t.Fatalf("poll after exit = %v", r)
	}
	log := f.call(t, "process", map[string]any{"action": "log", "session_id": id})
	if !strings.Contains(str(log, "output"), "two") || num(log, "total_lines") != 3 {
		t.Fatalf("log = %v", log)
	}
	page := f.call(t, "process", map[string]any{"action": "log", "session_id": id, "offset": 1, "limit": 1})
	if strings.TrimSpace(str(page, "output")) != "two" {
		t.Fatalf("log offset 1 limit 1 = %v", page)
	}
}

func TestWaitTimesOutWithoutKilling(t *testing.T) {
	f := setup(t, nil)
	r := f.call(t, "terminal", map[string]any{"command": "sleep 30", "background": true})
	id, pid := str(r, "session_id"), num(r, "pid")
	w := f.call(t, "process", map[string]any{"action": "wait", "session_id": id, "timeout": 1})
	if str(w, "status") != "timeout" || w["process_running"] != true || !alive(pid) {
		t.Fatalf("wait timeout = %v (alive %v)", w, alive(pid))
	}
	k := f.call(t, "process", map[string]any{"action": "kill", "session_id": id})
	if str(k, "status") != "killed" {
		t.Fatalf("kill = %v", k)
	}
	time.Sleep(300 * time.Millisecond)
	if alive(pid) {
		t.Fatal("kill must end the process")
	}
	if k := f.call(t, "process", map[string]any{"action": "kill", "session_id": id}); str(k, "status") != "already_exited" {
		t.Fatalf("second kill = %v", k)
	}
}

func TestKillTakesTheWholeProcessGroup(t *testing.T) {
	f := setup(t, nil)
	id := startBG(t, f, "sleep 30 & sleep 30 & wait", nil)
	time.Sleep(200 * time.Millisecond)
	r := f.call(t, "process", map[string]any{"action": "poll", "session_id": id})
	pid := num(r, "pid")
	f.call(t, "process", map[string]any{"action": "kill", "session_id": id})
	time.Sleep(300 * time.Millisecond)
	if syscall.Kill(-pid, 0) == nil {
		t.Fatal("children of a killed background process must die with it")
	}
}

func TestAnInterruptedWaitLeavesTheProcessRunning(t *testing.T) {
	f := setup(t, nil)
	r := f.call(t, "terminal", map[string]any{"command": "sleep 30", "background": true})
	id, pid := str(r, "session_id"), num(r, "pid")
	c, cancel := context.WithCancel(ctx)
	time.AfterFunc(200*time.Millisecond, cancel)
	w := f.callCtx(t, c, "process", map[string]any{"action": "wait", "session_id": id, "timeout": 30})
	if str(w, "status") != "interrupted" || !alive(pid) {
		t.Fatalf("interrupted wait = %v (alive %v)", w, alive(pid))
	}
	f.call(t, "process", map[string]any{"action": "kill", "session_id": id})
}

func TestListShowsOnlyThisSessionsProcesses(t *testing.T) {
	f := setup(t, nil)
	other := setup(t, nil)
	mine := startBG(t, f, "sleep 5", nil)
	theirs := startBG(t, other, "sleep 5", nil)
	r := f.call(t, "process", map[string]any{"action": "list"})
	var ids []string
	procs, _ := r["processes"].([]any)
	for _, p := range procs {
		ids = append(ids, str(p.(map[string]any), "session_id"))
	}
	joined := strings.Join(ids, ",")
	if !strings.Contains(joined, mine) || strings.Contains(joined, theirs) {
		t.Fatalf("list = %v", ids)
	}
	if r := f.call(t, "process", map[string]any{"action": "poll", "session_id": theirs}); !isErr(r) {
		t.Fatalf("another session's process must not be reachable: %v", r)
	}
}

func TestUniquePrefixesResolve(t *testing.T) {
	f := setup(t, nil)
	id := startBG(t, f, "true", nil)
	short := strings.TrimPrefix(id, "proc_")[:6]
	if r := f.call(t, "process", map[string]any{"action": "poll", "session_id": short}); str(r, "session_id") != id {
		t.Fatalf("prefix %q: %v", short, r)
	}
	if r := f.call(t, "process", map[string]any{"action": "poll", "session_id": "proc_nope"}); !isErr(r) {
		t.Fatalf("unknown id: %v", r)
	}
	if r := f.call(t, "process", map[string]any{"action": "submit", "session_id": id, "data": "x"}); !isErr(r) {
		t.Fatalf("stdin actions are not supported: %v", r)
	}
}

func TestOutputKeepsTheMostRecent200K(t *testing.T) {
	f := setup(t, nil)
	id := startBG(t, f, `head -c 300000 /dev/zero | tr '\0' a; printf 'END'`, nil)
	waitExit(t, f, id)
	log := f.call(t, "process", map[string]any{"action": "log", "session_id": id, "limit": 100000})
	out := str(log, "output")
	if len(out) > 200_100 || !strings.HasSuffix(strings.TrimSpace(out), "END") {
		t.Fatalf("buffered output: %d chars", len(out))
	}
}

func TestBackgroundCommandsGoThroughTheGate(t *testing.T) {
	f := setup(t, nil) // default mode, nobody to ask
	r := f.call(t, "terminal", map[string]any{"command": "rm -r somewhere", "background": true})
	if !strings.Contains(str(r, "error"), "BLOCKED") || str(r, "session_id") != "" {
		t.Fatalf("a dangerous background command with nobody to ask: %v", r)
	}
}

func TestNotifyOnCompleteNeedsAsyncDelivery(t *testing.T) {
	f := setup(t, approval.NewGate(approval.Yolo, nil, nil))
	r := f.call(t, "terminal", map[string]any{"command": "true", "background": true, "notify_on_complete": true})
	if r["notify_unsupported"] != true || str(r, "session_id") == "" {
		t.Fatalf("no async delivery: the process runs, the notification is refused: %v", r)
	}
	waitExit(t, f, str(r, "session_id"))
	if n := f.env.State.TakeNotifications(); len(n) != 0 {
		t.Fatalf("no notifications without async delivery: %v", n)
	}
}

func TestNotificationsArriveUnlessTheResultWasConsumed(t *testing.T) {
	f := setup(t, nil)
	f.env.AsyncDelivery = true
	waited := startBG(t, f, "echo consumed", map[string]any{"notify_on_complete": true})
	f.call(t, "process", map[string]any{"action": "wait", "session_id": waited, "timeout": 10})
	unseen := startBG(t, f, "echo finished-alone; exit 2", map[string]any{"notify_on_complete": true})
	waitExit(t, f, unseen) // poll observes the exit but does not consume it
	n := f.env.State.TakeNotifications()
	if len(n) != 1 || !strings.Contains(n[0], unseen) || !strings.Contains(n[0], "finished-alone") || !strings.Contains(n[0], "2") {
		t.Fatalf("notifications = %q", n)
	}
	if strings.Contains(strings.Join(n, ""), waited) {
		t.Fatal("a result read with wait must not notify again")
	}
	if again := f.env.State.TakeNotifications(); len(again) != 0 {
		t.Fatalf("TakeNotifications drains: %v", again)
	}
}

func TestClosingTheStateKillsItsProcesses(t *testing.T) {
	f := setup(t, nil)
	r := f.call(t, "terminal", map[string]any{"command": "sleep 30", "background": true})
	pid := num(r, "pid")
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
	for _, want := range []string{"terminal", "process", "read_file", "write_file", "patch", "search_files", "todo", "clarify"} {
		i := strings.Index(joined, ","+want+",")
		if i <= last {
			t.Fatalf("builtin tools = %v; missing or out of order: %s", names, want)
		}
		last = i
	}
}
