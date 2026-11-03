package server_test

import (
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

// Phase 3c through the daemon: session_search finds another session's
// conversation, and with recall on, each turn's conversation rows are
// indexed for meaning-based search once the turn ends.
func TestSessionSearchThroughTheDaemon(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "noted the protocol"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "session_search", `{"query":"quokka"}`)}},
		fakellm.Turn{Content: "found it"},
	)
	h := newHarness(t, fake, ragConfig(fake))
	c := h.dial()
	s1 := c.create(t.TempDir())
	c.attach(s1, "driver")
	c.send(s1, "the quokka protocol is green")
	c.turn()
	waitUntil(t, "the turn's rows to be indexed", func() bool {
		var user, reply bool
		for _, call := range fake.EmbedCalls() {
			for _, in := range call {
				user = user || strings.Contains(in, "the quokka protocol is green")
				reply = reply || strings.Contains(in, "noted the protocol")
			}
		}
		return user && reply
	})

	s2 := c.create(t.TempDir())
	c.attach(s2, "driver")
	c.send(s2, "what did we say about it")
	var result string
	for _, m := range committed(c.turn()) {
		if str(m, "role") == "tool" {
			result = str(m, "content")
		}
	}
	if !strings.Contains(result, s1) || !strings.Contains(result, "quokka") || strings.Contains(result, s2) {
		t.Fatalf("session_search result = %s; the other session, never the caller's own", result)
	}
}
