package server_test

import (
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

// A queue nobody will run says so: a terminal left it, and no chat owns the
// session, so "within a minute" would be a promise nothing keeps.
func TestAQueueLeftByATerminalWaitsForOne(t *testing.T) {
	dir := t.TempDir()
	rel := make(chan struct{})
	t.Cleanup(func() { close(rel) })
	fake := fakellm.New(t,
		fakellm.Turn{When: asWorker, Content: "slow", ChunkRunes: 2, Release: rel},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "list"})}},
		fakellm.Turn{When: asFiona, Content: "One is queued."},
	)
	r := newChatRig(t, fake, singleUser)
	c := r.h.dial()
	desk := c.create(dir)
	c.attach(desk, "driver")
	c.send(desk, "first")
	c.await("turn.start")
	c.send(desk, "second")
	c.ok("turn.interrupt", map[string]any{"session": desk})
	c.await("turn.end")
	c.close()

	r.say("what is there?")
	r.reply(sigGroup, "One is queued.")
	ws, _ := toolResult(t, fake, "r1")["workers"].([]any)
	if len(ws) != 1 || str(ws[0].(map[string]any), "state") != "queued (waiting for a terminal)" {
		t.Fatalf("relay list = %v", ws)
	}
}
