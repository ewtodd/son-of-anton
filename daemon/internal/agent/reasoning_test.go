package agent_test

// A model's reasoning goes back to it. Dropped, every step of a tool loop
// starts without the plan the step before made, and the model spends its turn
// working the same thing out again: a DeepSeek-V4 session read for two hours,
// tens of thousands of reasoning tokens a step, and edited nothing.

import (
	"bytes"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

func TestReasoningGoesBackWithEveryStepByteForByte(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{Reasoning: "plan: \"read\" it\nthen answer", ToolCalls: []fakellm.ToolCall{call("c1", "echo_tool", `{"x":"1"}`)}},
		fakellm.Turn{Reasoning: "it says 1, so answer", Content: "done"},
		fakellm.Turn{Content: "next turn"},
	)
	s, _ := toolRunner(t, st, fake, echoTool()).NewSession(ctx, "interactive", t.TempDir())
	send(t, s, "go")

	step := fake.Messages(1)
	if len(step) != 4 || !bytes.Contains(step[2], []byte(`"reasoning_content":"plan: \"read\" it\nthen answer"`)) {
		t.Fatalf("the next step lacks the reasoning behind its tool call: %s", step[2])
	}
	send(t, s, "again")
	later := fake.Messages(2)
	if !bytes.Contains(later[4], []byte(`"reasoning_content":"it says 1, so answer"`)) {
		t.Fatalf("a later turn lacks the last step's reasoning: %s", later[4])
	}
	// The cache needs the prefix unchanged, whatever a server keeps of it.
	for i := range step {
		if !bytes.Equal(step[i], later[i]) {
			t.Fatalf("message %d changed:\n%s\n%s", i, step[i], later[i])
		}
	}
	if bytes.Contains(later[1], []byte("reasoning_content")) || bytes.Contains(later[3], []byte("reasoning_content")) {
		t.Fatalf("only an assistant message carries reasoning: %s %s", later[1], later[3])
	}
}

// Compaction's estimates have to count what the requests carry, or it keeps a
// tail that does not fit and frees less than it thinks.
func TestCompactionCountsReasoning(t *testing.T) {
	st := newStore(t)
	const size = 40_000
	main := fakellm.New(t,
		fakellm.Turn{Content: "A", Reasoning: strings.Repeat("r", size)},
		fakellm.Turn{Content: "B"},
	)
	s, _ := withCompaction(runner(st, main, "sys", nil), fakellm.New(t)).NewSession(ctx, "interactive", "/w")
	send(t, s, "a")
	send(t, s, "b")
	plan, err := s.PlanCompaction()
	if err != nil || plan.Tokens < size/4 {
		t.Fatalf("PlanCompaction = %+v, %v; want at least %d tokens", plan, err, size/4)
	}
}
