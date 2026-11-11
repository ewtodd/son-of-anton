package agent_test

// The end-of-turn check: a reply that would end the turn is offered to
// Runner.Check first; a note sends it back for that request only.

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/check"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

type checkCall struct {
	model   string
	in      check.Input
	attempt int
}

func TestACheckedDraftIsSentBackAndNeverStored(t *testing.T) {
	st := newStore(t)
	echo := echoTool()
	fake := fakellm.New(t,
		fakellm.Turn{Content: "I've logged it."},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "echo_tool", `{"x":"log"}`)}},
		fakellm.Turn{Content: "Logged: log."},
	)
	r := toolRunner(t, st, fake, echo)
	r.Recall = func(context.Context, string) string { return "<recalled-notes>\nweights\n</recalled-notes>" }
	var calls []checkCall
	r.Check = func(_ context.Context, model string, in check.Input, attempt int) string {
		calls = append(calls, checkCall{model, in, attempt})
		if attempt == 0 {
			return "NOTE: you did not log it"
		}
		return ""
	}
	s, err := r.NewSession(ctx, "interactive", t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if res := send(t, s, "please log it"); res.Content != "Logged: log." {
		t.Fatalf("result %+v", res)
	}
	retry := full(t, fake.Messages(1))
	if roles(retry) != "system,user,assistant,user" || retry[2].Content != "I've logged it." || retry[3].Content != "NOTE: you did not log it" {
		t.Fatalf("the retry request = %+v; the draft, then the note", retry)
	}
	after := full(t, fake.Messages(2))
	if roles(after) != "system,user,assistant,tool" {
		t.Fatalf("after the retry's tool step = %s; rebuilt from the store, without the draft or the note", roles(after))
	}
	if len(calls) != 2 || calls[0].attempt != 0 || calls[1].attempt != 1 || calls[0].model != "m" {
		t.Fatalf("checks = %+v", calls)
	}
	first, second := calls[0].in, calls[1].in
	if first.Draft != "I've logged it." || len(first.Steps) != 0 || first.User != retry[1].Content ||
		!strings.Contains(first.User, "recalled-notes") || !strings.Contains(strings.Join(first.Tools, ","), "echo_tool") {
		t.Fatalf("first check input = %+v; the user message as the model saw it (the heads were fitted on that)", first)
	}
	if second.Draft != "Logged: log." || len(second.Steps) != 1 || second.Steps[0].Calls[0].Name != "echo_tool" ||
		!strings.Contains(second.Steps[0].Results[0], "log") {
		t.Fatalf("second check input = %+v; it sees the retry's tool step", second)
	}
	rows, _ := st.Messages(ctx, s.ID())
	for _, m := range rows {
		if strings.Contains(m.Content, "I've logged it.") || strings.Contains(m.Content, "NOTE") ||
			strings.Contains(m.APIContent, "NOTE") {
			t.Fatalf("stored %+v; neither the draft nor the note enters the transcript", m)
		}
	}
	if last := rows[len(rows)-1]; last.Role != "assistant" || last.Content != "Logged: log." {
		t.Fatalf("last stored row = %+v", last)
	}
}

func TestEachDraftIsOfferedUntilTheCheckerLetsOneStand(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "one"}, fakellm.Turn{Content: "two"}, fakellm.Turn{Content: "three"})
	r := runner(st, fake, "sys", nil)
	var attempts []int
	r.Check = func(_ context.Context, _ string, in check.Input, attempt int) string {
		attempts = append(attempts, attempt)
		if attempt < 2 {
			return "again"
		}
		return ""
	}
	s, _ := r.NewSession(ctx, "interactive", "/w")
	if res := send(t, s, "hi"); res.Content != "three" {
		t.Fatalf("result %q", res.Content)
	}
	if len(attempts) != 3 || attempts[2] != 2 {
		t.Fatalf("attempts %v", attempts)
	}
	if last := full(t, fake.Messages(2)); roles(last) != "system,user,assistant,user,assistant,user" {
		t.Fatalf("third request = %s; each draft and its note ride the request", roles(last))
	}
	rows, _ := st.Messages(ctx, s.ID())
	if len(rows) != 2 || rows[1].Content != "three" {
		t.Fatalf("stored %+v; only the user message and the reply that stood", rows)
	}
}

func TestAnAnswerUnderToolChoiceNoneIsNotChecked(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "echo_tool", `{}`)}},
		fakellm.Turn{Content: "summary"},
	)
	r := toolRunner(t, st, fake, echoTool())
	r.MaxSteps = 1
	asked := 0
	r.Check = func(context.Context, string, check.Input, int) string { asked++; return "again" }
	s, _ := r.NewSession(ctx, "interactive", t.TempDir())
	if res := send(t, s, "go"); res.Content != "summary" || asked != 0 || fake.Count() != 2 {
		t.Fatalf("result %q, checked %d times, %d requests; the step budget's answer cannot make the tool call a note asks for",
			res.Content, asked, fake.Count())
	}
}

func TestAStopDuringTheCheckLetsTheDraftStand(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "draft"}, fakellm.Turn{Content: "retry"})
	r := runner(st, fake, "sys", nil)
	entered := make(chan struct{})
	var cancelled bool
	r.Check = func(c context.Context, _ string, _ check.Input, _ int) string {
		close(entered)
		select {
		case <-c.Done():
			cancelled = true
		case <-time.After(2 * time.Second):
		}
		return "again"
	}
	s, _ := r.NewSession(ctx, "interactive", "/w")
	done := sendAsync(s, "hi", nil)
	<-entered
	s.Stop(false)
	if err := wait(t, done); err != nil || !cancelled || fake.Count() != 1 {
		t.Fatalf("err %v, check cancelled %v, %d requests; the stop cancels the check and no retry goes out", err, cancelled, fake.Count())
	}
	rows, _ := st.Messages(ctx, s.ID())
	if len(rows) != 2 || rows[1].Content != "draft" {
		t.Fatalf("stored %+v; the draft stands", rows)
	}
}
