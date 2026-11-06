package server_test

// Desktop nudges: an approval or a clarify question that waits longer than
// agent.prompt_notify_after gets a notify-send (the Python's nudge, owner
// 2026-11-06); answering it cancels the pending one, and 0 turns the nudge
// off.

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

func TestAWaitingApprovalNudges(t *testing.T) {
	cwd := t.TempDir()
	f := filepath.Join(cwd, "f.txt")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("a1", "chmod 777 f.txt")}},
		fakellm.Turn{Content: "moving on"},
	)
	h := newHarness(t, fake, "[agent]\nprompt_notify_after = 1\n")
	c := h.dial()
	id := c.create(cwd)
	c.attach(id, "driver")
	c.send(id, "make it writable")
	req := c.await("approval.request")
	waitUntil(t, "the nudge", func() bool { return len(h.nudged()) > 0 })
	if body := h.nudged()[0]; !strings.Contains(body, "waiting for your answer") || !strings.Contains(body, "chmod 777") {
		t.Fatalf("the nudge = %q", body)
	}
	c.ok("approval.answer", map[string]any{"session": id, "approval_id": str(req, "approval_id"), "choice": "deny"})
	c.turn()
	// A resolved request does not nudge again.
	time.Sleep(2 * time.Second)
	if n := len(h.nudged()); n != 1 {
		t.Fatalf("nudges = %d, want the one", n)
	}
}

func TestAWaitingClarifyQuestionNudges(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("q1", "clarify", `{"question":"Which figure is wrong?","choices":["a","b"]}`)}},
		fakellm.Turn{Content: "thanks"},
	)
	h := newHarness(t, fake, "[agent]\nprompt_notify_after = 1\n")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "ask me")
	req := c.await("question.request")
	waitUntil(t, "the nudge", func() bool { return len(h.nudged()) > 0 })
	if body := h.nudged()[0]; !strings.Contains(body, "Which figure is wrong?") {
		t.Fatalf("the nudge = %q", body)
	}
	c.ok("question.answer", map[string]any{"session": id, "question_id": str(req, "question_id"), "answers": []string{"a"}})
	c.turn()
	time.Sleep(2 * time.Second)
	if n := len(h.nudged()); n != 1 {
		t.Fatalf("nudges = %d, want the one", n)
	}
}

func TestPromptNotifyAfterZeroDisablesTheNudge(t *testing.T) {
	cwd := t.TempDir()
	f := filepath.Join(cwd, "f.txt")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("a1", "chmod 777 f.txt")}},
		fakellm.Turn{Content: "moving on"},
	)
	h := newHarness(t, fake, "[agent]\nprompt_notify_after = 0\n")
	c := h.dial()
	id := c.create(cwd)
	c.attach(id, "driver")
	c.send(id, "make it writable")
	req := c.await("approval.request")
	time.Sleep(2 * time.Second)
	if n := len(h.nudged()); n != 0 {
		t.Fatalf("prompt_notify_after = 0 nudged %d times", n)
	}
	c.ok("approval.answer", map[string]any{"session": id, "approval_id": str(req, "approval_id"), "choice": "deny"})
	c.turn()
}
