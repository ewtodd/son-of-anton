package agent_test

// Phase 1b session behavior: a session owns its background processes, its
// todo list survives a resume, notifications drain through the session, and
// clarify reaches the runner's questioner.

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
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "terminal", `{"command":"sleep 30","background":true}`)}},
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

func TestResumeRestoresTheTodoList(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "todo", `{"todos":[{"id":"1","content":"remember me","status":"in_progress"}]}`)}},
		fakellm.Turn{Content: "planned"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c2", "todo", `{}`)}},
		fakellm.Turn{Content: "still planned"},
	)
	s, _ := toolRunner(t, st, fake, tools.Builtin()...).NewSession(ctx, "interactive", t.TempDir())
	send(t, s, "plan")
	_ = s.Close()

	resumed, err := toolRunner(t, st, fake, tools.Builtin()...).Resume(ctx, s.ID())
	if err != nil {
		t.Fatal(err)
	}
	send(t, resumed, "what is the plan")
	ms := full(t, fake.Messages(3))
	if last := ms[len(ms)-1]; last.Role != "tool" || !strings.Contains(last.Content, "remember me") {
		t.Fatalf("after a resume the todo list comes back from the transcript: %+v", last)
	}
}

func TestNotificationsDrainThroughTheSession(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "terminal", `{"command":"echo bg-done","background":true,"notify_on_complete":true}`)}},
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

func TestWithoutAsyncDeliveryNotifyIsRefused(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "terminal", `{"command":"true","background":true,"notify_on_complete":true}`)}},
		fakellm.Turn{Content: "ok"},
	)
	s, _ := toolRunner(t, st, fake, tools.Builtin()...).NewSession(ctx, "interactive", t.TempDir())
	defer s.Close()
	send(t, s, "go")
	stored, _ := st.Messages(ctx, s.ID())
	if storedResult(t, stored[2].Content)["notify_unsupported"] != true {
		t.Fatalf("result = %s", stored[2].Content)
	}
}

func TestClarifyReachesTheRunnersQuestioner(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "clarify", `{"question":"Tabs or spaces?","choices":["tabs","spaces"]}`)}},
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
	if asked.Text != "Tabs or spaces?" {
		t.Fatalf("asked %+v", asked)
	}
	stored, _ := st.Messages(ctx, s.ID())
	if storedResult(t, stored[2].Content)["user_response"] != "tabs" {
		t.Fatalf("clarify result = %s", stored[2].Content)
	}
}
