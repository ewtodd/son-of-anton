package agent_test

// Phase 1b session behavior: a session owns its background processes,
// receipts drain through the session, AskUserQuestion reaches the runner's
// questioner, and an old tool name is answered with its replacement.

import (
	"context"
	"encoding/json"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

func storedResult(t *testing.T, content string) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal([]byte(content), &m); err != nil {
		t.Fatalf("tool row %q is not JSON: %v", content, err)
	}
	return m
}

func TestClosingASessionKillsItsBackgroundProcesses(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "Bash", `{"command":"sleep 30","run_in_background":true}`)}},
		fakellm.Turn{Content: "started"},
	)
	s, _ := toolRunner(t, st, fake, tools.Builtin()...).NewSession(ctx, "interactive", t.TempDir())
	send(t, s, "start something")
	stored, _ := st.Messages(ctx, s.ID())
	pid := int(storedResult(t, stored[2].Content)["pid"].(float64))
	if syscall.Kill(pid, 0) != nil {
		t.Fatal("the background process should be running")
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(300 * time.Millisecond)
	if syscall.Kill(pid, 0) == nil {
		t.Fatal("Close must kill the session's background processes")
	}
}

// A resumed session's history names tools by their old names; a model that
// calls one is told what replaced it, and a call to an old tool the session
// never had is just unknown.
func TestAnOldToolNameIsAnsweredWithItsReplacement(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{
			call("c1", "read_file", `{"path":"x.txt"}`),
			call("c2", "terminal", `{"command":"true"}`),
		}},
		fakellm.Turn{Content: "ok"},
	)
	r := toolRunner(t, st, fake, tools.Builtin()...)
	r.Tools = r.Tools.Only("Read", "Write")
	s, _ := r.NewSession(ctx, "interactive", t.TempDir())
	defer s.Close()
	send(t, s, "go")
	stored, _ := st.Messages(ctx, s.ID())
	if e, _ := storedResult(t, stored[2].Content)["error"].(string); !strings.Contains(e, "read_file is now Read (file_path, offset, limit)") {
		t.Fatalf("an old name = %s", stored[2].Content)
	}
	if e, _ := storedResult(t, stored[3].Content)["error"].(string); e != "unknown tool: terminal" {
		t.Fatalf("an old name whose replacement the session lacks = %s", stored[3].Content)
	}
}

func TestReceiptsDrainThroughTheSession(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "Bash", `{"command":"echo bg-done","run_in_background":true}`)}},
		fakellm.Turn{Content: "ok"},
	)
	r := toolRunner(t, st, fake, tools.Builtin()...)
	r.AsyncDelivery = true
	s, _ := r.NewSession(ctx, "interactive", t.TempDir())
	defer s.Close()
	send(t, s, "go")
	deadline := time.Now().Add(10 * time.Second)
	var got []string
	for time.Now().Before(deadline) && len(got) == 0 {
		got = s.TakeNotifications()
		time.Sleep(50 * time.Millisecond)
	}
	if len(got) != 1 || !strings.Contains(got[0], "bg-done") {
		t.Fatalf("notifications = %q", got)
	}
}

func TestWithoutAsyncDeliveryTheResultSaysToCheck(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "Bash", `{"command":"true","run_in_background":true}`)}},
		fakellm.Turn{Content: "ok"},
	)
	s, _ := toolRunner(t, st, fake, tools.Builtin()...).NewSession(ctx, "interactive", t.TempDir())
	defer s.Close()
	send(t, s, "go")
	stored, _ := st.Messages(ctx, s.ID())
	if note, _ := storedResult(t, stored[2].Content)["note"].(string); !strings.Contains(note, "check on it with BashOutput") {
		t.Fatalf("result = %s", stored[2].Content)
	}
}

func TestAskUserQuestionReachesTheRunnersQuestioner(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "AskUserQuestion",
			`{"questions":[{"question":"Tabs or spaces?","header":"Indent","multiSelect":false,"options":[{"label":"tabs","description":"one per level"},{"label":"spaces","description":"four per level"}]}]}`)}},
		fakellm.Turn{Content: "tabs it is"},
	)
	r := toolRunner(t, st, fake, tools.Builtin()...)
	var asked tools.Question
	r.Ask = func(_ context.Context, q tools.Question) ([]string, error) {
		asked = q
		return []string{"tabs"}, nil
	}
	s, _ := r.NewSession(ctx, "interactive", t.TempDir())
	defer s.Close()
	send(t, s, "format it")
	if asked.Text != "Tabs or spaces?" || asked.Header != "Indent" {
		t.Fatalf("asked %+v", asked)
	}
	stored, _ := st.Messages(ctx, s.ID())
	answers, _ := storedResult(t, stored[2].Content)["answers"].([]any)
	if len(answers) != 1 || answers[0].(map[string]any)["answer"] != "tabs" {
		t.Fatalf("AskUserQuestion result = %s", stored[2].Content)
	}
}
