package server_test

// Phase 4a through the daemon: the task tool runs child sessions
// (daemon/phases/04a-subagents.md). Child requests are told apart in the
// fake by their type's prompt, which no parent request contains.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/subagent"
)

const explorePrompt = "You are a file search specialist."

func task(id, args string) fakellm.ToolCall { return call(id, "task", args) }

func toolNames(t *testing.T, req map[string]any) []string {
	t.Helper()
	var names []string
	ts, _ := req["tools"].([]any)
	for _, raw := range ts {
		fn, _ := raw.(map[string]any)["function"].(map[string]any)
		names = append(names, str(fn, "name"))
	}
	return names
}

func toolRows(evs []map[string]any) []string {
	var out []string
	for _, m := range committed(evs) {
		if str(m, "role") == "tool" {
			out = append(out, str(m, "content"))
		}
	}
	return out
}

func TestATaskRunsAChildAndReturnsItsAnswer(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{task("t1", `{"description":"Find the thing","prompt":"look for the thing","subagent_type":"explore"}`)}},
		fakellm.Turn{When: explorePrompt, Content: "found it at /a/b"},
		fakellm.Turn{Content: "done"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	parent := c.create(t.TempDir())
	c.attach(parent, "driver")
	c.send(parent, "find the thing")
	evs := c.turn()

	created := of(evs, "child.created")
	if len(created) != 1 || str(created[0], "call_id") != "t1" {
		t.Fatalf("child.created = %v", created)
	}
	child := obj(created[0], "child")
	id := str(child, "id")
	if str(child, "parent_id") != parent || str(child, "kind") != "child" || str(child, "agent") != "explore" ||
		str(child, "title") != "Find the thing (@explore subagent)" {
		t.Fatalf("child = %v", child)
	}
	if done := of(evs, "child.done"); len(done) != 1 || str(done[0], "child_id") != id || str(done[0], "status") != "done" {
		t.Fatalf("child.done = %v", done)
	}
	if rows := toolRows(evs); len(rows) != 1 || rows[0] != subagent.Render(id, "completed", "", "found it at /a/b") {
		t.Fatalf("the task's result = %v", rows)
	}

	childReq := wire(t, fake, 1)
	sys := str(childReq[0], "content")
	if !strings.HasPrefix(sys, explorePrompt) || str(childReq[1], "content") != "look for the thing" {
		t.Fatalf("the child's request: system %.80q, user %q", sys, str(childReq[1], "content"))
	}
	if names := strings.Join(toolNames(t, fake.Request(1)), ","); names != "terminal,read_file,search_files" {
		t.Fatalf("explore's tools = %s", names)
	}
	if parentTools := strings.Join(toolNames(t, fake.Request(0)), ","); !strings.Contains(parentTools, "task") {
		t.Fatalf("the parent has task: %s", parentTools)
	}

	kids := objs(c.ok("session.children", map[string]any{"session": parent}), "children")
	if len(kids) != 1 || str(kids[0], "id") != id {
		t.Fatalf("session.children = %v", kids)
	}
	for _, s := range objs(c.ok("session.list", map[string]any{}), "sessions") {
		if str(s, "id") == id {
			t.Fatal("session lists never show a child")
		}
	}
	att := h.dial().attach(id, "viewer")
	if ms := objs(att, "messages"); roleList(ms) != "user,assistant" || str(ms[1], "content") != "found it at /a/b" {
		t.Fatalf("a viewer reads the child: %v", ms)
	}
}

func TestTasksInOneMessageRunConcurrently(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{
			task("t1", `{"description":"A","prompt":"do job alpha","subagent_type":"general"}`),
			task("t2", `{"description":"B","prompt":"do job beta","subagent_type":"general"}`),
		}},
		fakellm.Turn{When: "do job alpha", ToolCalls: []fakellm.ToolCall{terminal("a1", "sleep 0.8; echo alpha")}},
		fakellm.Turn{When: "do job beta", ToolCalls: []fakellm.ToolCall{terminal("b1", "sleep 0.8; echo beta")}},
		fakellm.Turn{When: "do job alpha", Content: "alpha done"},
		fakellm.Turn{When: "do job beta", Content: "beta done"},
		fakellm.Turn{Content: "both done"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	parent := c.create(t.TempDir())
	c.attach(parent, "driver")
	start := time.Now()
	c.send(parent, "do both")
	evs := c.turn()
	if took := time.Since(start); took > 1400*time.Millisecond {
		t.Fatalf("two children with 0.8s of work each took %v; they run together", took)
	}
	rows := toolRows(evs)
	if len(rows) != 2 || !strings.Contains(rows[0], "alpha done") || !strings.Contains(rows[1], "beta done") {
		t.Fatalf("results in call order: %v", rows)
	}
	if names := strings.Join(toolNames(t, fake.Request(1)), ","); strings.Contains(names, "task") || strings.Contains(names, "memory") ||
		strings.Contains(names, "clarify") || strings.Contains(names, "todo") || !strings.Contains(names, "terminal") {
		t.Fatalf("general's tools = %s; the parent's, without task, memory, clarify and todo", names)
	}
}

func TestABackgroundTaskReportsBackAsANotice(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{task("t1", `{"description":"Look around","prompt":"look around","subagent_type":"explore","background":true}`)}},
		fakellm.Turn{When: explorePrompt, Content: "bg result"},
		fakellm.Turn{Content: "launched"},
		fakellm.Turn{Content: "got it"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	parent := c.create(t.TempDir())
	c.attach(parent, "driver")
	c.send(parent, "start it")
	evs := c.turns(2)
	rows := toolRows(evs)
	if len(rows) == 0 || !strings.Contains(rows[0], `state="running"`) || !strings.Contains(rows[0], "<summary>Background task started</summary>") ||
		!strings.Contains(rows[0], "The task is working in the background.") {
		t.Fatalf("a background task returns at once: %v", rows)
	}
	starts := of(evs, "turn.start")
	if len(starts) != 2 {
		t.Fatalf("turns = %v", kinds(evs))
	}
	notice := str(starts[1], "text")
	if !strings.Contains(notice, `state="completed"`) || !strings.Contains(notice, "<summary>Background task completed: Look around</summary>") ||
		!strings.Contains(notice, "bg result") {
		t.Fatalf("the result arrives as a notice turn: %q", notice)
	}
}

func TestAChildsApprovalAsksTheParentsDriver(t *testing.T) {
	cwd := t.TempDir()
	f := filepath.Join(cwd, "f.txt")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{task("t1", `{"description":"Fix perms","prompt":"open up f.txt","subagent_type":"explore"}`)}},
		fakellm.Turn{When: explorePrompt, ToolCalls: []fakellm.ToolCall{terminal("c1", "chmod 777 f.txt")}},
		fakellm.Turn{When: explorePrompt, Content: "opened"},
		fakellm.Turn{Content: "ok"},
	)
	h := newHarness(t, fake, "")
	d := h.dial()
	parent := d.create(cwd)
	d.attach(parent, "driver")
	d.send(parent, "fix it")
	created := d.await("child.created")
	req := d.await("approval.request")
	if str(req, "session") != parent || str(req, "child_session") != str(obj(created, "child"), "id") || !strings.Contains(str(req, "subject"), "chmod 777") {
		t.Fatalf("approval.request = %v; a child's request goes to the parent's driver", req)
	}
	d.ok("approval.answer", map[string]any{"session": parent, "approval_id": str(req, "approval_id"), "choice": "once"})
	d.turn()
	if fi, _ := os.Stat(f); fi.Mode().Perm() != 0o777 {
		t.Fatal("the approved command ran in the child, in the parent's directory")
	}
}

func TestInterruptingTheParentCancelsItsChildren(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{task("t1", `{"description":"Wait","prompt":"wait forever","subagent_type":"explore"}`)}},
		fakellm.Turn{When: explorePrompt, Hang: true},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	parent := c.create(t.TempDir())
	c.attach(parent, "driver")
	c.send(parent, "go")
	c.await("child.created")
	waitUntil(t, "the child's request", func() bool { return fake.Count() == 2 })
	start := time.Now()
	c.ok("turn.interrupt", map[string]any{"session": parent})
	evs := c.turn()
	if time.Since(start) > 5*time.Second || str(evs[len(evs)-1], "status") != "interrupted" {
		t.Fatalf("turn.end = %v", evs[len(evs)-1])
	}
	if done := of(evs, "child.done"); len(done) != 1 || str(done[0], "status") != "interrupted" {
		t.Fatalf("child.done = %v", done)
	}
	if rows := toolRows(evs); len(rows) != 1 || !strings.Contains(rows[0], "Task cancelled") {
		t.Fatalf("the task's result = %v", rows)
	}
}

func TestTaskIDContinuesTheSameChild(t *testing.T) {
	var childID string
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{task("t1", `{"description":"Remember","prompt":"remember the number seven","subagent_type":"explore"}`)}},
		fakellm.Turn{When: explorePrompt, Content: "remembered"},
		fakellm.Turn{Content: "ok"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{{ID: "t2", Name: "task", ArgumentsFunc: func() string {
			b, _ := json.Marshal(map[string]any{"description": "Recall", "prompt": "which number was it", "subagent_type": "explore", "task_id": childID})
			return string(b)
		}}}},
		fakellm.Turn{When: "which number was it", Content: "seven"},
		fakellm.Turn{Content: "it was seven"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{task("t3", `{"description":"Bogus","prompt":"x","subagent_type":"explore","task_id":"20260101_000000_000000"}`)}},
		fakellm.Turn{Content: "no such task"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	parent := c.create(t.TempDir())
	c.attach(parent, "driver")
	c.send(parent, "remember")
	childID = str(obj(c.await("child.created"), "child"), "id")
	c.turn()

	c.send(parent, "recall")
	evs := c.turn()
	if rows := toolRows(evs); len(rows) != 1 || rows[0] != subagent.Render(childID, "completed", "", "seven") {
		t.Fatalf("resumed result = %v", rows)
	}
	resumed := wire(t, fake, 4)
	if roleList(resumed) != "system,user,assistant,user" || str(resumed[1], "content") != "remember the number seven" {
		t.Fatalf("the child continues with its history: %s", roleList(resumed))
	}
	if kids := objs(c.ok("session.children", map[string]any{"session": parent}), "children"); len(kids) != 1 {
		t.Fatalf("still one child: %v", kids)
	}

	c.send(parent, "bogus")
	if rows := toolRows(c.turn()); len(rows) != 1 || !strings.Contains(rows[0], "no task 20260101_000000_000000 in this session") {
		t.Fatalf("an unknown task_id: %v", rows)
	}
}
