package server_test

// Phase 10a: session.rename and session.delete for `son-of-anton sessions`
// (daemon/phases/10a-son-of-anton-command.md).

import (
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

// The retitle-to-a-model ask is gone (owner, 2026-11-06): the compaction
// rename tip and the TUI's /retitle replaced it, in retitle_tip_server_test.

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
