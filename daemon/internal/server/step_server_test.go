package server_test

// turn.send with mode step, what a terminal's enter sends during a turn: the
// message goes in once the running tools end, in the very next request, and
// never cuts a reply off. It closes the turn to do so, as a steer does: a user
// message never follows a tool result directly (phase 2a).

import (
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

func TestAStepMessageGoesInOnceTheRunningToolEnds(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{bashCall("c1", "sleep 0.5; echo finished")}},
		fakellm.Turn{Content: "took it in"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "go")
	c.await("tool.start")
	r := c.ok("turn.send", map[string]any{"session": id, "text": "change of plan", "mode": "step"})
	if r["started"] != false || str(r, "queued") == "" || r["steered"] == true {
		t.Fatalf("turn.send step while running = %v, want queued with an item id", r)
	}
	evs := c.turns(2)
	ends := of(evs, "turn.end")
	if str(ends[0], "status") != "steered" || str(ends[1], "status") != "done" {
		t.Fatalf("turn.end statuses = %v, %v", ends[0], ends[1])
	}
	if starts := of(evs, "turn.start"); len(starts) != 1 || str(starts[0], "text") != "change of plan" {
		t.Fatalf("the message starts the next turn at once: %v", starts)
	}
	for _, m := range committed(evs) {
		if str(m, "role") == "tool" && !strings.Contains(str(m, "content"), "finished") {
			t.Fatalf("the running command finishes; its result = %q", str(m, "content"))
		}
	}
	queues := of(evs, "queue.updated")
	if len(queues) == 0 || len(objs(queues[len(queues)-1], "items")) != 0 {
		t.Fatalf("the queue must end empty: %v", queues)
	}
	if fake.Count() != 2 {
		t.Fatalf("requests = %d; the tool's result and the message go in one request", fake.Count())
	}
	req := wire(t, fake, 1)
	if roleList(req) != "system,user,assistant,tool,assistant,user" || str(req[4], "content") != interrupted || str(req[5], "content") != "change of plan" {
		t.Fatalf("the request after the tool = %s %v", roleList(req), req)
	}
}

func TestAStepMessageNeverCutsAReplyOff(t *testing.T) {
	rel := make(chan struct{})
	fake := fakellm.New(t,
		fakellm.Turn{Content: "a whole answer", ChunkRunes: 4, Release: rel},
		fakellm.Turn{Content: "second answer"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "go")
	c.await("assistant.delta")
	c.ok("turn.send", map[string]any{"session": id, "text": "and also", "mode": "step"})
	close(rel)
	evs := c.turns(2)
	ends := of(evs, "turn.end")
	if str(ends[0], "status") != "done" || str(ends[1], "status") != "done" {
		t.Fatalf("a reply with no tool calls ends its turn, then the message runs: %v", ends)
	}
	req := wire(t, fake, 1)
	if roleList(req) != "system,user,assistant,user" || str(req[2], "content") != "a whole answer" || str(req[3], "content") != "and also" {
		t.Fatalf("the request after the reply = %s %v", roleList(req), req)
	}
}

func TestAStepMessageWaitsForTheToolsOfTheReplyItArrivedDuring(t *testing.T) {
	rel := make(chan struct{})
	fake := fakellm.New(t,
		fakellm.Turn{Content: "let me look", ChunkRunes: 4, Release: rel, ToolCalls: []fakellm.ToolCall{bashCall("c1", "echo finished")}},
		fakellm.Turn{Content: "took it in"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "go")
	c.await("assistant.delta")
	c.ok("turn.send", map[string]any{"session": id, "text": "and also", "mode": "step"})
	close(rel)
	c.turns(2)
	if fake.Count() != 2 {
		t.Fatalf("requests = %d", fake.Count())
	}
	req := wire(t, fake, 1)
	if roleList(req) != "system,user,assistant,tool,assistant,user" || str(req[2], "content") != "let me look" ||
		!strings.Contains(str(req[3], "content"), "finished") || str(req[5], "content") != "and also" {
		t.Fatalf("the reply and its tool stand whole, then the message: %s %v", roleList(req), req)
	}
}

func TestStepMessagesGoInTogether(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{bashCall("c1", "sleep 0.5")}},
		fakellm.Turn{Content: "both"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "go")
	c.await("tool.start")
	for _, q := range []string{"b", "c"} {
		c.ok("turn.send", map[string]any{"session": id, "text": q, "mode": "step"})
	}
	c.turns(2)
	if fake.Count() != 2 {
		t.Fatalf("requests = %d", fake.Count())
	}
	if req := wire(t, fake, 1); str(req[len(req)-1], "content") != "b\n\nc" {
		t.Fatalf("the last message = %q", str(req[len(req)-1], "content"))
	}
}

// What queues on purpose (/commit, a chat, a relayed message) still waits for
// the turn to end, behind what was sent for the next step.
func TestAStepMessageGoesAheadOfTheQueue(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{bashCall("c1", "sleep 0.5")}},
		fakellm.Turn{Content: "step done"},
		fakellm.Turn{Content: "queued done"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "go")
	c.await("tool.start")
	c.ok("turn.send", map[string]any{"session": id, "text": "queued", "mode": "queue"})
	c.ok("turn.send", map[string]any{"session": id, "text": "step", "mode": "step"})
	evs := c.turns(3)
	var starts []string
	for _, e := range of(evs, "turn.start") {
		starts = append(starts, str(e, "text"))
	}
	if strings.Join(starts, ",") != "step,queued" {
		t.Fatalf("turns started %v, want step then queued", starts)
	}
}

func TestAMessageQueuedOnPurposeOutlastsAToolStep(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{bashCall("c1", "sleep 0.5")}},
		fakellm.Turn{Content: "first turn done"},
		fakellm.Turn{Content: "queued done"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "go")
	c.await("tool.start")
	c.ok("turn.send", map[string]any{"session": id, "text": "queued", "mode": "queue"})
	evs := c.turns(2)
	if ends := of(evs, "turn.end"); str(ends[0], "status") != "done" {
		t.Fatalf("the turn runs to its answer: %v", ends)
	}
	if fake.Count() != 3 {
		t.Fatalf("requests = %d", fake.Count())
	}
}

func TestAnInterruptLeavesAStepMessageQueued(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{bashCall("c1", "sleep 30")}},
		fakellm.Turn{Content: "ran it"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "go")
	c.await("tool.start")
	c.ok("turn.send", map[string]any{"session": id, "text": "later", "mode": "step"})
	c.ok("turn.interrupt", map[string]any{"session": id})
	if end := c.await("turn.end"); str(end, "status") != "interrupted" {
		t.Fatalf("turn.end = %v", end)
	}
	c.none("turn.start", 300*time.Millisecond)
	if r := c.ok("queue.steer", map[string]any{"session": id}); r["started"] != true {
		t.Fatalf("queue.steer = %v, want the message still there to start", r)
	}
	c.turn()
	if req := wire(t, fake, 1); str(req[len(req)-1], "content") != "later" {
		t.Fatalf("the last message = %q", str(req[len(req)-1], "content"))
	}
}

func TestAnUnknownSendModeIsRefused(t *testing.T) {
	h := newHarness(t, fakellm.New(t), "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.fails("turn.send", map[string]any{"session": id, "text": "x", "mode": "later"}, codeBadParams)
}
