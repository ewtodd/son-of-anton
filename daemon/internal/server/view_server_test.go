package server_test

// Phase 4b, the daemon side of the TUI's subagent view
// (daemon/phases/04b-subagent-view.md): stored usage, task row summaries,
// child.progress, and the children's run stats.

import (
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

const generalPrompt = "Another agent has handed you one job."

func num(m map[string]any, k string) float64 { v, _ := m[k].(float64); return v }

func TestUsageAndContextSurviveARestart(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "hi", PromptTokens: 300, CompletionTokens: 20, CachedTokens: 100})
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "hello")
	c.turn()
	c.close()
	h.restart()

	c = h.dial()
	sess := obj(c.attach(id, "viewer"), "session")
	u := obj(sess, "usage")
	if num(sess, "context_tokens") != 320 || num(u, "input_tokens") != 300 || num(u, "output_tokens") != 20 ||
		num(u, "cached_tokens") != 100 || num(u, "api_calls") != 1 {
		t.Fatalf("after a restart: context %v, usage %v; both are stored", sess["context_tokens"], u)
	}
	for _, s := range objs(c.ok("session.list", map[string]any{}), "sessions") {
		if str(s, "id") == id && num(s, "context_tokens") != 320 {
			t.Fatalf("session.list = %v", s)
		}
	}
}

func TestATaskRowFollowsItsChild(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{task("t1", `{"description":"Find the thing","prompt":"look for the thing","subagent_type":"explore"}`)}},
		fakellm.Turn{When: explorePrompt, ToolCalls: []fakellm.ToolCall{terminal("a1", "sleep 0.2; echo one")}},
		fakellm.Turn{When: explorePrompt, ToolCalls: []fakellm.ToolCall{terminal("a2", "echo two")}},
		fakellm.Turn{When: explorePrompt, Content: "found it", PromptTokens: 500, CompletionTokens: 7},
		fakellm.Turn{Content: "done"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	parent := c.create(t.TempDir())
	c.attach(parent, "driver")
	c.send(parent, "find the thing")
	evs := c.turn()

	if s := str(of(evs, "tool.start")[0], "summary"); s != "Explore Task — Find the thing" {
		t.Fatalf("the task row's summary = %q", s)
	}
	id := str(obj(of(evs, "child.created")[0], "child"), "id")
	prog := of(evs, "child.progress")
	if len(prog) != 2 || str(prog[0], "child_id") != id || str(prog[0], "name") != "terminal" ||
		str(prog[0], "summary") != "sleep 0.2; echo one" || num(prog[0], "tool_calls") != 1 ||
		str(prog[1], "summary") != "echo two" || num(prog[1], "tool_calls") != 2 {
		t.Fatalf("child.progress = %v", prog)
	}
	if i, j := indexOf(evs, "child.progress"), indexOf(evs, "child.done"); i > j {
		t.Fatal("progress comes before the end")
	}
	done := of(evs, "child.done")[0]
	if num(done, "tool_calls") != 2 || num(done, "duration_ms") < 200 {
		t.Fatalf("child.done = %v", done)
	}
	kids := objs(c.ok("session.children", map[string]any{"session": parent}), "children")
	if len(kids) != 1 || num(kids[0], "tool_calls") != 2 || num(kids[0], "duration_ms") < 200 ||
		num(kids[0], "context_tokens") != 507 || num(obj(kids[0], "usage"), "api_calls") != 3 {
		t.Fatalf("session.children = %v", kids)
	}
}

func TestBackgroundAndFailedTaskRows(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{
			task("t1", `{"description":"Look around","prompt":"look around","subagent_type":"general","background":true}`),
			task("t2", `{"description":"Carry on","prompt":"more","subagent_type":"explore","task_id":"20260101_000000_000000"}`),
		}},
		fakellm.Turn{When: generalPrompt, Content: "bg done"},
		fakellm.Turn{Content: "launched"},
		fakellm.Turn{Content: "noted"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	parent := c.create(t.TempDir())
	c.attach(parent, "driver")
	c.send(parent, "start it")
	evs := c.turns(2)

	summaries := map[string]string{}
	for _, e := range of(evs, "tool.start") {
		summaries[str(e, "call_id")] = str(e, "summary")
	}
	if summaries["t1"] != "General Task (background) — Look around" || summaries["t2"] != "Explore Task — Carry on" {
		t.Fatalf("summaries = %v", summaries)
	}
	failed := map[string]bool{}
	for _, e := range of(evs, "tool.end") {
		failed[str(e, "call_id")] = e["is_error"] == true
	}
	if failed["t1"] || !failed["t2"] {
		t.Fatalf("is_error = %v; a task result in state error is an error", failed)
	}
	if done := of(evs, "child.done"); len(done) != 1 || num(done[0], "tool_calls") != 0 {
		t.Fatalf("child.done = %v", done)
	}
	if !strings.Contains(text(evs, "turn.start"), "Finished in the background: Look around") {
		t.Fatal("the notice turn")
	}
}
