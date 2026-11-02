package server_test

// Phase 3b through the socket: an interactive turn carries recalled facts,
// and the memory tool's rule changes go to the driver for approval.

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

func ragConfig(fake *fakellm.Server) string {
	return "[memory.rag]\nenabled = true\nbase_url = \"" + fake.URL + "\"\nmodel = \"embed\"\nmin_score = 0.4\n\n"
}

func putFile(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestRecallAndRuleApprovalThroughTheDaemon(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "it is at 10.0.0.3"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "memory", `{"kind":"rule","action":"add","text":"Answer in plain English"}`)}},
		fakellm.Turn{Content: "saved"},
		fakellm.Turn{Content: "fresh session"},
	)
	h := newHarness(t, fake, ragConfig(fake))
	putFile(t, filepath.Join(h.home, "memories", "facts.toml"),
		"[[fact]]\nid = \"nas-address\"\ntext = \"NAS address 10.0.0.3 home network\"\nsource = \"agent\"\n")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")

	c.send(id, "NAS network address")
	c.turn()
	first := wire(t, fake, 0)
	if u := str(first[len(first)-1], "content"); !strings.Contains(u, "<recalled-notes>") || !strings.Contains(u, "[fact nas-address]") {
		t.Fatalf("an interactive turn carries recalled facts: %q", u)
	}

	c.send(id, "remember to use plain English")
	req := c.await("approval.request")
	if str(req, "approval_kind") != "memory" || !strings.Contains(str(req, "subject"), "Answer in plain English") ||
		strings.Join(strs(req, "choices"), ",") != "once,deny" {
		t.Fatalf("approval.request = %v", req)
	}
	c.ok("approval.answer", map[string]any{"session": id, "approval_id": str(req, "approval_id"), "choice": "once"})
	c.turn()
	rules, _ := os.ReadFile(filepath.Join(h.home, "memories", "rules.toml"))
	if !strings.Contains(string(rules), "Answer in plain English") {
		t.Fatalf("rules.toml = %s", rules)
	}
	if string(fake.Messages(2)[0]) != string(fake.Messages(0)[0]) {
		t.Fatal("a new rule never changes a running session's prompt")
	}
	other := c.create(t.TempDir())
	c.attach(other, "driver")
	c.send(other, "hi")
	c.turn()
	if !strings.Contains(str(wire(t, fake, 3)[0], "content"), "[answer-in-plain-english]") {
		t.Fatal("a new session's prompt carries the new rule")
	}
}
