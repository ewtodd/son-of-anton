package agent_test

import (
	"context"
	"encoding/json"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// concurrentTool is a fakeTool that says it may run beside its siblings.
type concurrentTool struct{ *fakeTool }

func (concurrentTool) Concurrent() bool { return true }

// Phase 4a: consecutive calls to tools that implement Concurrent() true run
// at the same time (opencode runs several task calls in one message
// concurrently); results keep the calls' order, and other tools still run
// one at a time.
func TestConcurrentToolsRunTogether(t *testing.T) {
	st := newStore(t)
	var inFlight, most atomic.Int32
	slow := concurrentTool{&fakeTool{name: "slow_tool", run: func(ctx context.Context, args json.RawMessage) string {
		n := inFlight.Add(1)
		for {
			m := most.Load()
			if n <= m || most.CompareAndSwap(m, n) {
				break
			}
		}
		time.Sleep(300 * time.Millisecond)
		inFlight.Add(-1)
		return `{"echo":` + string(args) + `}`
	}}}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{
			call("c1", "slow_tool", `{"x":"1"}`), call("c2", "slow_tool", `{"x":"2"}`), call("c3", "slow_tool", `{"x":"3"}`),
			call("c4", "echo_tool", `{"x":"4"}`),
		}},
		fakellm.Turn{Content: "done"},
	)
	s, _ := toolRunner(t, st, fake, slow, echoTool()).NewSession(ctx, "interactive", t.TempDir())
	start := time.Now()
	send(t, s, "go")
	if took := time.Since(start); took > 800*time.Millisecond {
		t.Fatalf("three 300ms calls took %v; they run together", took)
	}
	if most.Load() != 3 {
		t.Fatalf("at most %d ran at once, want 3", most.Load())
	}
	ms := full(t, fake.Messages(1))
	if roles(ms) != "system,user,assistant,tool,tool,tool,tool" {
		t.Fatalf("roles = %s", roles(ms))
	}
	for i, want := range []string{"c1", "c2", "c3", "c4"} {
		if ms[3+i].ToolCallID != want || (i < 3 && !strings.Contains(ms[3+i].Content, `"x":"`+want[1:]+`"`)) {
			t.Fatalf("result %d = %+v; results keep the calls' order", i, ms[3+i])
		}
	}
	checkPairing(t, ms)
}

func TestAnInterruptStopsConcurrentCalls(t *testing.T) {
	st := newStore(t)
	slow := concurrentTool{&fakeTool{name: "slow_tool", delay: 30 * time.Second}}
	fake := fakellm.New(t, fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "slow_tool", `{}`), call("c2", "slow_tool", `{}`)}})
	s, _ := toolRunner(t, st, fake, slow).NewSession(ctx, "interactive", t.TempDir())
	onEvent, started := signalOn(func(e agent.Event) bool { return e.Kind == "tool.start" })
	start := time.Now()
	done := sendAsync(s, "go", onEvent)
	<-started
	s.Stop(false)
	if err := wait(t, done); err == nil {
		t.Fatal("the turn was stopped")
	}
	if time.Since(start) > 5*time.Second {
		t.Fatal("Stop(false) cancels every running concurrent call")
	}
	stored, _ := st.Messages(ctx, s.ID())
	if len(stored) != 5 || stored[2].ToolCallID != "c1" || stored[3].ToolCallID != "c2" || stored[4].Content != agent.InterruptedText {
		t.Fatalf("stored %+v", stored)
	}
}

var _ tools.Tool = concurrentTool{}
