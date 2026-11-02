package server_test

import (
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

// The session reports the model that served it (maitred's x-maitred-model),
// and /commit as the bot asks for a Model: line from $SOA_MODEL.
func TestTheServedModelAndTheCommitModelLine(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "hi", ServedModel: "radiance/qwen3.8-27b"}, fakellm.Turn{Content: "committed"})
	h := newHarness(t, fake, "[git]\nauthor_name = \"Bot\"\nauthor_email = \"bot@example.com\"\n")
	c := h.dial()
	id := c.create(t.TempDir())
	if s := obj(c.attach(id, "driver"), "session"); str(s, "served_model") != "test-model" {
		t.Fatalf("before any answer the served model is the session's: %v", s)
	}
	c.send(id, "hello")
	c.turn()
	upd := c.awaitWhere("session.updated", func(e map[string]any) bool { return str(obj(e, "state"), "status") == "idle" })
	if str(obj(upd, "state"), "served_model") != "radiance/qwen3.8-27b" {
		t.Fatalf("state = %v", obj(upd, "state"))
	}
	c.ok("command.run", map[string]any{"session": id, "name": "commit"})
	text := str(of(c.turn(), "turn.start")[0], "text")
	if !strings.Contains(text, "Model: <id>") || !strings.Contains(text, "$SOA_MODEL") {
		t.Fatalf("a commit authored as the bot names its model: %q", text)
	}
}
