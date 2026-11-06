package server_test

// A session that never got a turn does not count as a session (owner,
// 2026-11-06): /resume and session.list leave out the one the TUI just
// opened. A session with its first turn in flight still lists: nothing is
// stored until the first step ends.

import (
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

func TestAZeroTurnSessionDoesNotList(t *testing.T) {
	rel := make(chan struct{})
	fake := fakellm.New(t, fakellm.Turn{Content: "slow", ChunkRunes: 1, Release: rel})
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	if n := len(objs(c.ok("session.list", map[string]any{}), "sessions")); n != 0 {
		t.Fatalf("an attached session with no turns listed %d sessions", n)
	}
	c.send(id, "go")
	c.await("turn.start")
	if n := len(objs(c.ok("session.list", map[string]any{}), "sessions")); n != 1 {
		t.Fatalf("a session with its first turn in flight lists %d sessions", n)
	}
	close(rel)
	c.turn()
	if n := len(objs(c.ok("session.list", map[string]any{}), "sessions")); n != 1 {
		t.Fatalf("after the turn: %d sessions", n)
	}
}
