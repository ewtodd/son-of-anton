package agent_test

import (
	"bytes"
	"context"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// A user message's send time is rendered once into its API copy, never into
// the stored content; later turns replay those exact bytes even when the zone
// changes.
func TestSendTimeIsInTheRequestAndNotTheStoredContent(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "one"}, fakellm.Turn{Content: "two"})
	r := runner(st, fake, "sys "+prompt.Guidance, nil) // the prompt carries the rule
	r.MessageTimestamps = true
	s, err := r.NewSession(ctx, "interactive", "/w")
	if err != nil {
		t.Fatal(err)
	}

	t.Setenv("TZ", "America/Chicago")
	sent := time.Date(2026, 10, 24, 19, 32, 0, 0, time.UTC) // Sat 14:32 CDT
	want := "[Sat 2026-10-24 14:32 CDT] remind me at 6"
	if _, err := s.SendAt(ctx, "remind me at 6", sent, nil); err != nil {
		t.Fatal(err)
	}
	req1 := decode(t, fake.Messages(0))
	if req1[1].Role != "user" || req1[1].Content != want {
		t.Fatalf("first request user = %+v, want %q", req1[1], want)
	}
	msgs, err := st.Messages(ctx, s.ID())
	if err != nil {
		t.Fatal(err)
	}
	if msgs[0].Role != "user" || msgs[0].Content != "remind me at 6" || msgs[0].APIContent != want {
		t.Fatalf("stored user row = %+v; content stays clean, api_content carries the prefix", msgs[0])
	}

	// A zone change renders the new turn in the new zone, but history is not
	// rewritten: the first user message replays its original bytes.
	t.Setenv("TZ", "UTC")
	sent2 := time.Date(2026, 10, 25, 12, 0, 0, 0, time.UTC)
	if _, err := s.SendAt(ctx, "second", sent2, nil); err != nil {
		t.Fatal(err)
	}
	req2 := decode(t, fake.Messages(1))
	if !bytes.Equal(fake.Messages(1)[1], fake.Messages(0)[1]) {
		t.Fatalf("the first user message changed in request 2:\n%s\n%s", fake.Messages(0)[1], fake.Messages(1)[1])
	}
	if req2[3].Role != "user" || req2[3].Content != "[Sun 2026-10-25 12:00 UTC] second" {
		t.Fatalf("second user = %+v", req2[3])
	}
}

func TestMessageTimestampSwitchOffSendsTheTextAsIs(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "ok"})
	r := runner(st, fake, "sys", nil)
	r.MessageTimestamps = false
	s, err := r.NewSession(ctx, "interactive", "/w")
	if err != nil {
		t.Fatal(err)
	}
	sent := time.Date(2026, 10, 24, 19, 32, 0, 0, time.UTC)
	if _, err := s.SendAt(ctx, "plain", sent, nil); err != nil {
		t.Fatal(err)
	}
	if got := decode(t, fake.Messages(0))[1].Content; got != "plain" {
		t.Fatalf("request user = %q; the switch is off", got)
	}
	msgs, err := st.Messages(ctx, s.ID())
	if err != nil {
		t.Fatal(err)
	}
	if msgs[0].Content != "plain" || msgs[0].APIContent != "plain" {
		t.Fatalf("stored user row = %+v", msgs[0])
	}
}

// A prompt without the guidance's send-time rule gets no prefix, even with
// the switch on: the stamp goes out only where the rule that explains it is.
func TestNoPrefixWhenThePromptLacksTheRule(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "ok"})
	r := runner(st, fake, "a prompt with no rule", nil)
	r.MessageTimestamps = true
	s, err := r.NewSession(ctx, "interactive", "/w")
	if err != nil {
		t.Fatal(err)
	}
	sent := time.Date(2026, 10, 24, 19, 32, 0, 0, time.UTC)
	if _, err := s.SendAt(ctx, "plain", sent, nil); err != nil {
		t.Fatal(err)
	}
	if got := decode(t, fake.Messages(0))[1].Content; got != "plain" {
		t.Fatalf("request user = %q; the prompt lacks the rule, so no prefix", got)
	}
	msgs, err := st.Messages(ctx, s.ID())
	if err != nil {
		t.Fatal(err)
	}
	if msgs[0].Content != "plain" || msgs[0].APIContent != "plain" {
		t.Fatalf("stored user row = %+v", msgs[0])
	}
}

// A session stored with a prompt that predates the rule stays unstamped until
// compaction rebuilds the prompt with it: the next turn's message is stamped,
// and the earlier stored bytes are unchanged.
func TestCompactionRebuildsThePromptWithTheRule(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{Content: "B", PromptTokens: 800},
		fakellm.Turn{Content: "C", PromptTokens: 800},
		fakellm.Turn{Content: "D", PromptTokens: 300},
		fakellm.Turn{Content: "E", PromptTokens: 300},
	)
	aux := fakellm.New(t, fakellm.Turn{Content: "SUMMARY-BODY"})
	prompts := 0
	r := &agent.Runner{
		Store: st, Client: provider.New(fake.URL, "k"), Model: "m", ReasoningEffort: "high",
		MessageTimestamps: true,
		Prompt: func(context.Context, store.Session) (string, error) {
			prompts++
			if prompts == 1 {
				return "an old prompt, no rule", nil
			}
			return "a rebuilt prompt: " + prompt.Guidance, nil
		},
	}
	r.Client.Retry = provider.RetryPolicy{MaxAttempts: 2, BaseDelay: time.Millisecond, MaxDelay: time.Millisecond}
	c := provider.New(aux.URL, "k")
	c.Retry = provider.RetryPolicy{MaxAttempts: 1, BaseDelay: time.Millisecond, MaxDelay: time.Millisecond}
	r.Compaction = agent.Compaction{Enabled: true, Threshold: 0.75, ContextLength: 1000, Client: c, Model: "summarizer"}
	s, err := r.NewSession(ctx, "interactive", "/w")
	if err != nil {
		t.Fatal(err)
	}
	sent1 := time.Date(2026, 10, 24, 19, 32, 0, 0, time.UTC)
	if _, err := s.SendAt(ctx, bigUser, sent1, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := s.SendAt(ctx, "b", time.Time{}, nil); err != nil {
		t.Fatal(err)
	}
	sent2 := time.Date(2026, 10, 24, 19, 33, 0, 0, time.UTC)
	if _, err := s.SendAt(ctx, "c", sent2, nil); err != nil {
		t.Fatal(err)
	}
	sent3 := time.Date(2026, 10, 24, 19, 34, 0, 0, time.UTC)
	if _, err := s.SendAt(ctx, "d", sent3, nil); err != nil {
		t.Fatal(err)
	}
	if prompts != 2 || aux.Count() != 1 {
		t.Fatalf("prompt builds %d, summary requests %d; compaction rebuilds the prompt once", prompts, aux.Count())
	}
	req0 := decode(t, fake.Messages(0))
	if req0[0].Content != "an old prompt, no rule" || strings.Contains(req0[0].Content, prompt.SentTimeRule) {
		t.Fatalf("first request system = %q", req0[0].Content)
	}
	if req0[1].Content != bigUser {
		t.Fatalf("the rule-less prompt stamps nothing: %q", req0[1].Content)
	}
	// The compaction turn's own message was built before the rebuild.
	req2 := decode(t, fake.Messages(2))
	if !strings.Contains(req2[0].Content, prompt.SentTimeRule) {
		t.Fatalf("the rebuilt prompt carries the rule: %q", req2[0].Content)
	}
	if req2[len(req2)-1].Content != "c" {
		t.Fatalf("the compaction turn's message keeps its own bytes: %q", req2[len(req2)-1].Content)
	}
	req3 := decode(t, fake.Messages(3))
	want := prompt.FormatSentAt(sent3) + " d"
	if req3[len(req3)-1].Content != want {
		t.Fatalf("the next turn's message is stamped: %q, want %q", req3[len(req3)-1].Content, want)
	}
	msgs, err := st.Messages(ctx, s.ID())
	if err != nil {
		t.Fatal(err)
	}
	if msgs[0].Content != bigUser || msgs[0].APIContent != bigUser {
		t.Fatalf("the earlier stored bytes are unchanged: %+v", msgs[0])
	}
}

// The format itself: the same instant, different local zones.
func TestSentAtFormatUsesTheLocalZone(t *testing.T) {
	t.Setenv("TZ", "America/Chicago")
	instant := time.Date(2026, 10, 24, 19, 32, 0, 0, time.UTC)
	if got := prompt.FormatSentAt(instant); got != "[Sat 2026-10-24 14:32 CDT]" {
		t.Fatalf("Chicago = %q", got)
	}
	t.Setenv("TZ", "UTC")
	if got := prompt.FormatSentAt(instant); got != "[Sat 2026-10-24 19:32 UTC]" {
		t.Fatalf("UTC = %q", got)
	}
}

// Fiona cannot run commands, so the guidance must not tell her to read later
// time from a terminal. The rule also covers a stamp that a compaction
// summary has pushed out of the start of the message.
func TestGuidanceExplainsTheSendTimePrefixWithoutATerminal(t *testing.T) {
	g := prompt.Guidance
	if !contains(g, prompt.SentTimeRule) || !contains(g, "was sent at that time") || !contains(g, "otherwise do not guess") {
		t.Fatalf("guidance lacks the send-time rule:\n%s", g)
	}
	if !contains(g, "compaction summary") {
		t.Fatalf("the send-time rule does not cover a stamp after a compaction summary:\n%s", g)
	}
}

func contains(s, sub string) bool { return bytes.Contains([]byte(s), []byte(sub)) }
