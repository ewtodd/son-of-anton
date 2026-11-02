package agent_test

// Tool-loop contract (phase 1a): tool calls run, results pair with their
// calls, every step is persisted so a resumed session replays the same bytes,
// and no path ever injects a synthetic user message.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/paths"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// fakeTool answers with a fixed JSON result built from its arguments.
type fakeTool struct {
	name  string
	runs  atomic.Int32
	run   func(ctx context.Context, args json.RawMessage) string
	delay time.Duration
}

func (f *fakeTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: f.name, Description: "test tool " + f.name,
		Parameters: json.RawMessage(`{"type":"object","properties":{"x":{"type":"string"}}}`),
	}}
}

func (f *fakeTool) Run(ctx context.Context, _ *tools.Env, args json.RawMessage) (string, error) {
	f.runs.Add(1)
	if f.delay > 0 {
		select {
		case <-time.After(f.delay):
		case <-ctx.Done():
			return `{"error":"interrupted"}`, nil
		}
	}
	if f.run != nil {
		return f.run(ctx, args), nil
	}
	return `{"echo":` + string(args) + `}`, nil
}

func echoTool() *fakeTool { return &fakeTool{name: "echo_tool"} }

func toolRunner(t *testing.T, st *store.Store, fake *fakellm.Server, ts ...tools.Tool) *agent.Runner {
	t.Helper()
	r := runner(st, fake, "sys", nil)
	home := t.TempDir()
	r.Tools = tools.NewRegistry(ts...)
	r.Policy = paths.Policy{Home: home, SoaHome: filepath.Join(home, ".son-of-anton")}
	r.Mode = approval.Yolo
	return r
}

func call(id, name, args string) fakellm.ToolCall {
	return fakellm.ToolCall{ID: id, Name: name, Arguments: args}
}

type fullMsg struct {
	Role       string `json:"role"`
	Content    string `json:"content"`
	ToolCallID string `json:"tool_call_id"`
	ToolCalls  []struct {
		ID string `json:"id"`
	} `json:"tool_calls"`
}

func full(t *testing.T, raw []json.RawMessage) []fullMsg {
	t.Helper()
	out := make([]fullMsg, len(raw))
	for i, r := range raw {
		if err := json.Unmarshal(r, &out[i]); err != nil {
			t.Fatal(err)
		}
	}
	return out
}

func roles(ms []fullMsg) string {
	var rs []string
	for _, m := range ms {
		rs = append(rs, m.Role)
	}
	return strings.Join(rs, ",")
}

// checkPairing asserts the alternation rule with tools: user and assistant
// never repeat, a user message never follows a tool result directly (strict
// providers misread tool -> user; phase 2a), and each tool message answers a
// call of the assistant message before it, exactly once per call.
func checkPairing(t *testing.T, ms []fullMsg) {
	t.Helper()
	pending := map[string]bool{}
	for i, m := range ms {
		switch m.Role {
		case "tool":
			if !pending[m.ToolCallID] {
				t.Fatalf("message %d: tool result for %q, which no open call made (roles %s)", i, m.ToolCallID, roles(ms))
			}
			delete(pending, m.ToolCallID)
			continue
		case "user", "assistant":
			if len(pending) > 0 {
				t.Fatalf("message %d (%s) arrives before calls %v got results", i, m.Role, pending)
			}
			if i > 0 && ms[i-1].Role == m.Role {
				t.Fatalf("two %s messages in a row at %d (roles %s)", m.Role, i, roles(ms))
			}
			if i > 0 && m.Role == "user" && ms[i-1].Role == "tool" {
				t.Fatalf("a user message follows a tool result at %d (roles %s)", i, roles(ms))
			}
		}
		for _, c := range m.ToolCalls {
			pending[c.ID] = true
		}
	}
}

func rawTools(t *testing.T, fake *fakellm.Server, i int) string {
	t.Helper()
	var req struct {
		Tools json.RawMessage `json:"tools"`
	}
	if err := json.Unmarshal(fake.Bodies()[i], &req); err != nil {
		t.Fatal(err)
	}
	return string(req.Tools)
}

func TestToolCallsRunAndTheLoopContinues(t *testing.T) {
	st := newStore(t)
	echo := echoTool()
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "echo_tool", `{"x":"one"}`)}},
		fakellm.Turn{Content: "done"},
	)
	s, err := toolRunner(t, st, fake, echo).NewSession(ctx, "interactive", t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	res := send(t, s, "go")
	if res.Content != "done" || echo.runs.Load() != 1 {
		t.Fatalf("result %+v, runs %d", res, echo.runs.Load())
	}
	ms := full(t, fake.Messages(1))
	if roles(ms) != "system,user,assistant,tool" || ms[3].ToolCallID != "c1" || !strings.Contains(ms[3].Content, `"one"`) {
		t.Fatalf("second request = %+v", ms)
	}
	if rawTools(t, fake, 0) == "" || rawTools(t, fake, 0) != rawTools(t, fake, 1) {
		t.Fatal("the tool list must be sent, byte-identical, on every request of the session")
	}
}

func TestSeveralCallsInOneMessageAnswerInOrder(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "echo_tool", `{"x":"a"}`), call("c2", "echo_tool", `{"x":"b"}`)}},
		fakellm.Turn{Content: "ok"},
	)
	s, _ := toolRunner(t, st, fake, echoTool()).NewSession(ctx, "interactive", t.TempDir())
	send(t, s, "go")
	ms := full(t, fake.Messages(1))
	if roles(ms) != "system,user,assistant,tool,tool" || ms[3].ToolCallID != "c1" || ms[4].ToolCallID != "c2" ||
		!strings.Contains(ms[3].Content, `"a"`) || !strings.Contains(ms[4].Content, `"b"`) {
		t.Fatalf("request = %+v", ms)
	}
	checkPairing(t, ms)
}

func TestUnknownToolsAndBadArgumentsBecomeErrorResults(t *testing.T) {
	st := newStore(t)
	echo := echoTool()
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "no_such_tool", `{}`), call("c2", "echo_tool", `not json`)}},
		fakellm.Turn{Content: "recovered"},
	)
	s, _ := toolRunner(t, st, fake, echo).NewSession(ctx, "interactive", t.TempDir())
	if res := send(t, s, "go"); res.Content != "recovered" {
		t.Fatalf("result %+v", res)
	}
	ms := full(t, fake.Messages(1))
	if !strings.Contains(ms[3].Content, "unknown tool") || !strings.Contains(ms[4].Content, "invalid arguments") {
		t.Fatalf("error results = %q / %q", ms[3].Content, ms[4].Content)
	}
	if echo.runs.Load() != 0 {
		t.Fatal("a call with invalid arguments must not run")
	}
}

func TestToolStepsPersistAndReplayByteForByte(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "echo_tool", `{"x":"v"}`)}},
		fakellm.Turn{Content: "first answer"},
		fakellm.Turn{Content: "second answer"},
	)
	r := toolRunner(t, st, fake, echoTool())
	s, _ := r.NewSession(ctx, "interactive", t.TempDir())
	send(t, s, "go")
	stored, _ := st.Messages(ctx, s.ID())
	var storedRoles []string
	for _, m := range stored {
		storedRoles = append(storedRoles, m.Role)
	}
	if strings.Join(storedRoles, ",") != "user,assistant,tool,assistant" {
		t.Fatalf("stored roles %v", storedRoles)
	}

	resumed, err := toolRunner(t, st, fake, echoTool()).Resume(ctx, s.ID())
	if err != nil {
		t.Fatal(err)
	}
	send(t, resumed, "again")
	prev, next := fake.Messages(1), fake.Messages(2)
	for j := range prev {
		if !bytes.Equal(prev[j], next[j]) {
			t.Fatalf("message %d changed after a resume:\n%s\n%s", j, prev[j], next[j])
		}
	}
	tail := full(t, next[len(prev):])
	if roles(tail) != "assistant,user" || tail[0].Content != "first answer" || tail[1].Content != "again" {
		t.Fatalf("appended after resume: %+v", tail)
	}
}

func TestAFailureAfterAToolStepKeepsTheStep(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "echo_tool", `{}`)}},
		fakellm.Turn{Status: 503}, fakellm.Turn{Status: 503},
		fakellm.Turn{Content: "ok now"},
	)
	s, _ := toolRunner(t, st, fake, echoTool()).NewSession(ctx, "interactive", t.TempDir())
	if _, err := s.Send(ctx, "go", nil); err == nil {
		t.Fatal("the turn must fail")
	}
	stored, _ := st.Messages(ctx, s.ID())
	if len(stored) != 4 || stored[3].Role != "assistant" || stored[3].Content != agent.FailedText {
		t.Fatalf("stored %+v; the completed tool step (user, assistant, tool) must be kept and closed with %q", stored, agent.FailedText)
	}
	send(t, s, "retry")
	ms := full(t, fake.Messages(3))
	if roles(ms) != "system,user,assistant,tool,assistant,user" {
		t.Fatalf("after a failed step: %s", roles(ms))
	}
	checkPairing(t, ms)
}

func TestAnInterruptDuringToolsKeepsEveryCallAnswered(t *testing.T) {
	st := newStore(t)
	slow := &fakeTool{name: "slow_tool", delay: 30 * time.Second}
	echo := echoTool()
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "slow_tool", `{}`), call("c2", "echo_tool", `{}`)}},
	)
	s, _ := toolRunner(t, st, fake, slow, echo).NewSession(ctx, "interactive", t.TempDir())
	cctx, cancel := context.WithCancel(ctx)
	time.AfterFunc(100*time.Millisecond, cancel)
	start := time.Now()
	if _, err := s.Send(cctx, "go", nil); !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
	if time.Since(start) > 5*time.Second {
		t.Fatal("the interrupt must stop the running tool")
	}
	if echo.runs.Load() != 0 {
		t.Fatal("calls after the interrupt must not run")
	}
	stored, _ := st.Messages(ctx, s.ID())
	if len(stored) != 5 || stored[2].ToolCallID != "c1" || stored[3].ToolCallID != "c2" {
		t.Fatalf("stored %+v; both calls need results", stored)
	}
	if !strings.Contains(strings.ToLower(stored[3].Content), "cancel") {
		t.Fatalf("the skipped call's result = %q, want it to say it was cancelled", stored[3].Content)
	}
	if stored[4].Role != "assistant" || stored[4].Content != agent.InterruptedText {
		t.Fatalf("an interrupted tool step closes with %q, got %+v", agent.InterruptedText, stored[4])
	}
}

func TestTheStepBudgetEndsWithAToolFreeAnswer(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "echo_tool", `{}`)}},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c2", "echo_tool", `{}`)}},
		fakellm.Turn{Content: "summary of what I did"},
	)
	r := toolRunner(t, st, fake, echoTool())
	r.MaxSteps = 2
	s, _ := r.NewSession(ctx, "interactive", t.TempDir())
	if res := send(t, s, "go"); res.Content != "summary of what I did" {
		t.Fatalf("result %+v", res)
	}
	if fake.Count() != 3 {
		t.Fatalf("requests = %d, want 2 steps plus the final answer", fake.Count())
	}
	for i := 0; i < 2; i++ {
		if _, present := fake.Request(i)["tool_choice"]; present {
			t.Fatalf("request %d must not restrict tools", i)
		}
	}
	if fake.Request(2)["tool_choice"] != "none" {
		t.Fatalf("the final request must ask for an answer with tool_choice none: %v", fake.Request(2)["tool_choice"])
	}
	if rawTools(t, fake, 2) != rawTools(t, fake, 0) {
		t.Fatal("the final request keeps the same tool list (the prefix stays cached)")
	}
	ms := full(t, fake.Messages(2))
	if strings.Count(roles(ms), "user") != 1 {
		t.Fatalf("no synthetic user message may be injected: %s", roles(ms))
	}
	checkPairing(t, ms)
}

func TestLargeToolResultsAreTruncatedBeforeTheyEnterContext(t *testing.T) {
	st := newStore(t)
	big := &fakeTool{name: "big_tool", run: func(context.Context, json.RawMessage) string {
		return `{"output":"` + strings.Repeat("z", 300_000) + `"}`
	}}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "big_tool", `{}`)}},
		fakellm.Turn{Content: "ok"},
	)
	r := toolRunner(t, st, fake, big)
	r.MaxToolResultChars = 10_000
	s, _ := r.NewSession(ctx, "interactive", t.TempDir())
	send(t, s, "go")
	stored, _ := st.Messages(ctx, s.ID())
	got := stored[2].Content
	if len(got) > 11_000 || !strings.Contains(got, "TRUNCATED") {
		t.Fatalf("stored tool result: %d chars", len(got))
	}
}

func TestToolEventsStream(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "echo_tool", `{"x":"e"}`)}},
		fakellm.Turn{Content: "fin"},
	)
	s, _ := toolRunner(t, st, fake, echoTool()).NewSession(ctx, "interactive", t.TempDir())
	var kinds []string
	var start, end agent.Event
	if _, err := s.Send(ctx, "go", func(e agent.Event) {
		kinds = append(kinds, e.Kind)
		switch e.Kind {
		case "tool.start":
			start = e
		case "tool.end":
			end = e
		}
	}); err != nil {
		t.Fatal(err)
	}
	if start.Tool != "echo_tool" || start.CallID != "c1" || !strings.Contains(start.Text, `"e"`) {
		t.Fatalf("tool.start = %+v", start)
	}
	if end.Tool != "echo_tool" || end.CallID != "c1" || !strings.Contains(end.Text, "echo") {
		t.Fatalf("tool.end = %+v", end)
	}
	joined := strings.Join(kinds, ",")
	if strings.Index(joined, "tool.start") > strings.Index(joined, "tool.end") || !strings.Contains(joined, "assistant.delta") {
		t.Fatalf("event order = %v", kinds)
	}
}

func TestTheRealToolsGoThroughTheSessionsGate(t *testing.T) {
	st := newStore(t)
	work := t.TempDir()
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "terminal", `{"command":"echo made > made.txt"}`)}},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c2", "terminal", `{"command":"rm -r made.txt"}`)}},
		fakellm.Turn{Content: "done"},
	)
	r := toolRunner(t, st, fake, tools.Builtin()...)
	r.Mode = approval.Default // nobody to ask
	s, _ := r.NewSession(ctx, "interactive", work)
	send(t, s, "go")
	if _, err := os.Stat(filepath.Join(work, "made.txt")); err != nil {
		t.Fatal("the safe command must run in the session's cwd, and the dangerous one must be blocked")
	}
	ms := full(t, fake.Messages(2))
	if !strings.Contains(ms[len(ms)-1].Content, "BLOCKED") {
		t.Fatalf("the blocked call's result = %q", ms[len(ms)-1].Content)
	}
}
