package agent_test

// OnStep: a turn's hook between tool steps, where the daemon takes in what was
// sent for the next step.

import (
	"errors"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

func TestOnStepRunsAfterEachToolStepOnceItIsStored(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "echo_tool", `{}`)}},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c2", "echo_tool", `{}`)}},
		fakellm.Turn{Content: "done"},
	)
	s, _ := toolRunner(t, st, fake, echoTool()).NewSession(ctx, "interactive", t.TempDir())
	var rows []int
	s.OnStep(func() {
		stored, _ := st.Messages(ctx, s.ID())
		rows = append(rows, len(stored))
	})
	if res := send(t, s, "go"); res.Content != "done" {
		t.Fatalf("result %+v", res)
	}
	if len(rows) != 2 || rows[0] != 3 || rows[1] != 5 {
		t.Fatalf("OnStep saw %v rows, want 3 (user, assistant, tool) then 5, and nothing after the answer", rows)
	}
}

func TestAStopFromOnStepEndsTheTurnThere(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "echo_tool", `{}`)}},
		fakellm.Turn{Content: "never asked for"},
	)
	s, _ := toolRunner(t, st, fake, echoTool()).NewSession(ctx, "interactive", t.TempDir())
	s.OnStep(func() { s.Stop(true) })
	if _, err := s.Send(ctx, "go", nil); !errors.Is(err, agent.ErrStopped) {
		t.Fatalf("err = %v, want agent.ErrStopped", err)
	}
	if fake.Count() != 1 {
		t.Fatalf("requests = %d; the turn ends before the next one", fake.Count())
	}
	stored, _ := st.Messages(ctx, s.ID())
	if len(stored) != 4 || stored[2].Role != "tool" || stored[3].Content != agent.InterruptedText {
		t.Fatalf("stored %+v, want the step kept and a closing %q", stored, agent.InterruptedText)
	}
}
