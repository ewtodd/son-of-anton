package server_test

// After a compaction the session announces, deterministically and with no
// model involved, that the conversation has probably outgrown its name — the
// owner answers with the TUI's /retitle <new title> (a session.rename). A
// title the user set themselves is never tipped over (owner, 2026-11-06).
// The tip rides the compaction (auto ones included, mid-turn in the feed);
// this test drives it through the compact command, where it lands between
// turns and is observable event by event.

import (
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

func TestACompactionTipsTheRename(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: strings.Repeat("A", 4000)},
		fakellm.Turn{Content: "B"},
		fakellm.Turn{Content: "C"},
		fakellm.Turn{Content: "D"},
	)
	aux := fakellm.New(t, fakellm.Turn{Content: "SUMMARY-BODY"}, fakellm.Turn{Content: "SUMMARY-AGAIN"})
	h := newHarness(t, fake, auxCompaction(aux))
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, big)
	c.turn()
	c.send(id, "b")
	c.turn()

	// Two turns are four rows; the compact keeps the last two verbatim and
	// summarizes the head — and the tip rides the compaction.
	c.ok("command.run", map[string]any{"session": id, "name": "compact", "args": ""})
	tip := c.awaitWhere("notice", func(e map[string]any) bool {
		return strings.Contains(str(e, "text"), "/retitle")
	})
	if body := str(tip, "text"); !strings.Contains(body, "outgrew its title") || !strings.Contains(body, "/retitle") {
		t.Fatalf("the tip = %v", tip)
	}

	// The user's own title silences the tip from then on: a rename sets the
	// title's source, so the next compaction announces nothing. Two more
	// turns give that compaction a head to summarize.
	c.ok("session.rename", map[string]any{"session": id, "title": "Mine"})
	c.send(id, "c")
	c.turn()
	c.send(id, "d")
	c.turn()
	c.ok("command.run", map[string]any{"session": id, "name": "compact", "args": ""})
	c.none("notice", 1500*time.Millisecond)
}
