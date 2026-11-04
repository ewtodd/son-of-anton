package agent_test

// An empty reply is not an answer. A complete response with no text and no
// tool calls (a server dropped what the model wrote, or the model ended its
// turn on nothing) is sent again, byte for byte and without a stored row, so
// the prompt stays cached and no synthetic message appears; a reply that stays
// empty fails the turn, so it never just stops.

import (
	"bytes"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

// warnings returns an onEvent collecting the turn's warnings.
func warnings(into *[]string) func(agent.Event) {
	return func(e agent.Event) {
		if e.Kind == "warning" {
			*into = append(*into, e.Text)
		}
	}
}

func sameBody(t *testing.T, fake *fakellm.Server, a, b int) {
	t.Helper()
	bodies := fake.Bodies()
	if !bytes.Equal(bodies[a], bodies[b]) {
		t.Fatalf("request %d must repeat request %d byte for byte:\n%s\n%s", b, a, bodies[a], bodies[b])
	}
}

func TestAnEmptyReplyIsSentAgainUnchanged(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{PromptTokens: 10, CompletionTokens: 354},
		fakellm.Turn{Content: "the answer", PromptTokens: 12, CompletionTokens: 5},
	)
	s, err := runner(st, fake, "sys", nil).NewSession(ctx, "interactive", "/work")
	if err != nil {
		t.Fatal(err)
	}
	var warned []string
	res, err := s.Send(ctx, "q", warnings(&warned))
	if err != nil {
		t.Fatal(err)
	}
	if res.Content != "the answer" || fake.Count() != 2 {
		t.Fatalf("result %+v after %d requests", res, fake.Count())
	}
	sameBody(t, fake, 0, 1)
	if res.APICalls != 2 || res.Usage.CompletionTokens != 359 {
		t.Fatalf("the empty attempt still counts: %+v", res)
	}
	if len(warned) != 1 || !strings.Contains(warned[0], "354") {
		t.Fatalf("warnings = %q, want one naming the tokens that were lost", warned)
	}
	stored, _ := st.Messages(ctx, s.ID())
	if len(stored) != 2 || stored[0].Content != "q" || stored[1].Content != "the answer" {
		t.Fatalf("stored %+v, want the user's message and the answer: an empty attempt leaves no row", stored)
	}
}

// The way it was found: a turn stopping right after a tool result.
func TestAnEmptyReplyAfterAToolStepContinuesTheTurn(t *testing.T) {
	st := newStore(t)
	echo := echoTool()
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "echo_tool", `{}`)}},
		fakellm.Turn{},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c2", "echo_tool", `{}`)}},
		fakellm.Turn{Content: "done"},
	)
	s, _ := toolRunner(t, st, fake, echo).NewSession(ctx, "interactive", t.TempDir())
	if res := send(t, s, "go"); res.Content != "done" {
		t.Fatalf("result %+v", res)
	}
	sameBody(t, fake, 1, 2)
	if echo.runs.Load() != 2 {
		t.Fatalf("tool ran %d times", echo.runs.Load())
	}
	stored, _ := st.Messages(ctx, s.ID())
	var got []string
	for _, m := range stored {
		got = append(got, m.Role)
		if m.Role == "assistant" && m.Content == "" && len(m.ToolCalls) == 0 {
			t.Fatalf("a blank row was stored: %+v", stored)
		}
	}
	if r := strings.Join(got, ","); r != "user,assistant,tool,assistant,tool,assistant" {
		t.Fatalf("stored roles %s", r)
	}
}

func TestAReplyOfOnlyReasoningOrSpaceIsEmpty(t *testing.T) {
	for name, turn := range map[string]fakellm.Turn{
		"reasoning": {Reasoning: "I will call a tool"},
		"space":     {Content: " \n\t "},
	} {
		t.Run(name, func(t *testing.T) {
			st := newStore(t)
			fake := fakellm.New(t, turn, fakellm.Turn{Content: "the answer"})
			s, _ := runner(st, fake, "sys", nil).NewSession(ctx, "interactive", "/work")
			if res := send(t, s, "q"); res.Content != "the answer" || fake.Count() != 2 {
				t.Fatalf("result %+v after %d requests", res, fake.Count())
			}
		})
	}
}

func TestRepliesThatStayEmptyFailTheTurn(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{}, fakellm.Turn{}, fakellm.Turn{Reasoning: "still nothing"},
		fakellm.Turn{Content: "back again"},
	)
	s, _ := runner(st, fake, "sys", nil).NewSession(ctx, "interactive", "/work")
	var warned []string
	res, err := s.Send(ctx, "q", warnings(&warned))
	if !errors.Is(err, agent.ErrEmptyReply) {
		t.Fatalf("err = %v, want agent.ErrEmptyReply", err)
	}
	if fake.Count() != 3 || res.APICalls != 3 {
		t.Fatalf("%d requests, %d calls; a reply that stays empty gets the first try and two more", fake.Count(), res.APICalls)
	}
	if len(warned) != 2 {
		t.Fatalf("warnings = %q, want one per retry", warned)
	}
	stored, _ := st.Messages(ctx, s.ID())
	if len(stored) != 2 || stored[1].Role != "assistant" || stored[1].Content != agent.FailedText || stored[1].Reasoning != "still nothing" {
		t.Fatalf("stored %+v, want the user's message and a closing %q keeping the last reasoning", stored, agent.FailedText)
	}
	if res := send(t, s, "continue"); res.Content != "back again" {
		t.Fatalf("the next turn after a failed one: %+v", res)
	}
}

func TestEachToolStepGetsItsOwnRetries(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{}, fakellm.Turn{},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "echo_tool", `{}`)}},
		fakellm.Turn{}, fakellm.Turn{},
		fakellm.Turn{Content: "done"},
	)
	s, _ := toolRunner(t, st, fake, echoTool()).NewSession(ctx, "interactive", t.TempDir())
	if res := send(t, s, "go"); res.Content != "done" {
		t.Fatalf("result %+v", res)
	}
}

// The final request of a turn out of steps ignores any tool calls, so a reply
// of nothing else has no answer in it either.
func TestAFinalRequestAnsweredOnlyWithToolCallsIsEmpty(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "echo_tool", `{}`)}},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c2", "echo_tool", `{}`)}},
		fakellm.Turn{Content: "summary"},
	)
	r := toolRunner(t, st, fake, echoTool())
	r.MaxSteps = 1
	s, _ := r.NewSession(ctx, "interactive", t.TempDir())
	if res := send(t, s, "go"); res.Content != "summary" {
		t.Fatalf("result %+v", res)
	}
	sameBody(t, fake, 1, 2)
}

func TestARetryCanBeStopped(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{}, fakellm.Turn{Hang: true})
	s, _ := runner(st, fake, "sys", nil).NewSession(ctx, "interactive", "/work")
	onEvent, retrying := signalOn(func(e agent.Event) bool { return e.Kind == "warning" })
	done := sendAsync(s, "q", onEvent)
	select {
	case <-retrying:
	case <-time.After(10 * time.Second):
		t.Fatal("the empty reply was not retried")
	}
	s.Stop(false)
	if err := wait(t, done); !errors.Is(err, agent.ErrStopped) {
		t.Fatalf("err = %v, want agent.ErrStopped", err)
	}
	stored, _ := st.Messages(ctx, s.ID())
	if len(stored) != 2 || stored[1].Content != agent.InterruptedText {
		t.Fatalf("stored %+v, want a closing %q", stored, agent.InterruptedText)
	}
}
