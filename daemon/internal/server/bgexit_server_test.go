package server_test

// The session object carries the count of running background processes, so
// a process ending, killed or on its own, sends session.updated.

import (
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

func TestABackgroundProcessEndingUpdatesTheSession(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("b1", "Bash", `{"command":"sleep 0.3","run_in_background":true}`)}},
		fakellm.Turn{Content: "started"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.ok("session.set", map[string]any{"session": id, "mode": "yolo"})
	c.send(id, "start it")
	c.turn()
	c.awaitWhere("session.updated", func(e map[string]any) bool {
		return num(obj(e, "state"), "background_processes") == 1
	})
	c.awaitWhere("session.updated", func(e map[string]any) bool {
		return num(obj(e, "state"), "background_processes") == 0
	})
}
