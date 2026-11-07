package server_test

// Phase 10a: session.rename and session.delete for `son-of-anton sessions`
// (daemon/phases/10a-son-of-anton-command.md).

import (
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

// /retitle: the session's own model names the conversation where it stands,
// not the little titles model naming the opening message. An explicit user
// rename still wins afterwards, and a later retitle beats that too.
func TestARetitleLetsTheSessionModelNameIt(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "the answer"},
		fakellm.Turn{Content: `{"title": "Receipts and rerolling"}`},
		fakellm.Turn{Content: `{"title": "Session named again"}`},
	)
	h := newHarness(t, fake, "") // no auxiliary title model: the derived one stands
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "retitle proves hard to spell")
	c.turn()

	c.ok("session.retitle", map[string]any{"session": id})
	c.awaitWhere("session.updated", func(e map[string]any) bool {
		return str(obj(e, "state"), "title") == "Receipts and rerolling"
	})

	// The ask went to the session's endpoint under the session's model, with
	// both sides of the conversation in the digest.
	if req := fake.Request(1); req["model"] != "test-model" {
		t.Fatalf("retitle request = %v", req)
	}
	digest := string(fake.Messages(1)[1])
	if !strings.Contains(digest, "user: retitle proves hard to spell") || !strings.Contains(digest, "assistant: the answer") {
		t.Fatalf("the retitle digest = %s", digest)
	}

	c.ok("session.rename", map[string]any{"session": id, "title": "Login bugs"})
	c.awaitWhere("session.updated", func(e map[string]any) bool {
		return str(obj(e, "state"), "title") == "Login bugs"
	})
	c.ok("session.retitle", map[string]any{"session": id})
	c.awaitWhere("session.updated", func(e map[string]any) bool {
		return str(obj(e, "state"), "title") == "Session named again"
	})
	time.Sleep(150 * time.Millisecond) // the second retitle's event order settles
}

func TestRenameAndDeleteASession(t *testing.T) {
	rel := make(chan struct{})
	fake := fakellm.New(t, fakellm.Turn{Content: "slow", ChunkRunes: 2, Release: rel})
	h := newHarness(t, fake, "")
	tui, cli := h.dial(), h.dial()
	id := tui.create(t.TempDir())
	tui.attach(id, "driver")

	// Renaming takes no role: the TUI stays the driver and hears of it.
	cli.ok("session.rename", map[string]any{"session": id, "title": "the parser fix"})
	if st := obj(tui.await("session.updated"), "state"); st["title"] != "the parser fix" {
		t.Fatalf("session.updated after a rename = %v", st)
	}
	cli.fails("session.rename", map[string]any{"session": id, "title": "  "}, -32602)
	cli.fails("session.rename", map[string]any{"session": "nope", "title": "x"}, -32011)

	// In use: attached, then running.
	cli.fails("session.delete", map[string]any{"session": id}, -32013)
	tui.send(id, "go")
	tui.await("turn.start")
	tui.close()
	cli.fails("session.delete", map[string]any{"session": id}, -32013)
	close(rel)

	other := cli.create(t.TempDir())
	cli.ok("session.delete", map[string]any{"session": other})
	cli.fails("session.attach", map[string]any{"session": other, "as": "viewer"}, -32011)
	cli.fails("session.delete", map[string]any{"session": other}, -32011)
	for _, s := range objs(cli.ok("session.list", map[string]any{}), "sessions") {
		if str(s, "id") == other {
			t.Fatal("a deleted session is still listed")
		}
	}
}
