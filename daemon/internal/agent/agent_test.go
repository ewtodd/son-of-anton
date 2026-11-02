package agent_test

// These tests pin the invariants in daemon/SPEC.md section 4 on the exact
// bytes the model server receives: a session's system prompt never changes,
// each request replays the previous one byte for byte, roles alternate, and a
// failed turn leaves no trace.

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

var ctx = context.Background()

func newStore(t *testing.T) *store.Store {
	t.Helper()
	st, err := store.Open(filepath.Join(t.TempDir(), "soa-d.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return st
}

func runner(st *store.Store, fake *fakellm.Server, prompt string, calls *int) *agent.Runner {
	c := provider.New(fake.URL, "k")
	c.Retry = provider.RetryPolicy{MaxAttempts: 2, BaseDelay: time.Millisecond, MaxDelay: time.Millisecond}
	return &agent.Runner{
		Store: st, Client: c, Model: "m", ReasoningEffort: "high",
		Prompt: func(context.Context, store.Session) (string, error) {
			if calls != nil {
				*calls++
			}
			return prompt, nil
		},
	}
}

type wireMsg struct {
	Role    string          `json:"role"`
	Content string          `json:"content"`
	Raw     json.RawMessage `json:"-"`
}

func decode(t *testing.T, raw []json.RawMessage) []wireMsg {
	t.Helper()
	out := make([]wireMsg, len(raw))
	for i, r := range raw {
		if err := json.Unmarshal(r, &out[i]); err != nil {
			t.Fatal(err)
		}
		out[i].Raw = r
	}
	return out
}

func send(t *testing.T, s *agent.Session, text string) agent.Result {
	t.Helper()
	res, err := s.Send(ctx, text, nil)
	if err != nil {
		t.Fatalf("Send(%q): %v", text, err)
	}
	return res
}

func TestTheSystemPromptIsBuiltOnceAndStored(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "one"}, fakellm.Turn{Content: "two"})
	builds := 0
	s, err := runner(st, fake, "you are a test agent", &builds).NewSession(ctx, "interactive", "/work")
	if err != nil {
		t.Fatal(err)
	}
	send(t, s, "first")
	send(t, s, "second")
	if builds != 1 {
		t.Fatalf("prompt built %d times, want once per session", builds)
	}
	a, b := fake.Messages(0)[0], fake.Messages(1)[0]
	if !bytes.Equal(a, b) {
		t.Fatalf("system message changed between turns:\n%s\n%s", a, b)
	}
	if m := decode(t, fake.Messages(0))[0]; m.Role != "system" || m.Content != "you are a test agent" {
		t.Fatalf("system message = %+v", m)
	}
	sess, err := st.Session(ctx, s.ID())
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte("you are a test agent"))
	if sess.PromptHash != hex.EncodeToString(sum[:]) || sess.Kind != "interactive" || sess.Cwd != "/work" {
		t.Fatalf("stored session = %+v", sess)
	}
}

func TestResumeReplaysTheStoredPromptNotARebuiltOne(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "one"}, fakellm.Turn{Content: "two"})
	s, err := runner(st, fake, "prompt A", nil).NewSession(ctx, "interactive", "/work")
	if err != nil {
		t.Fatal(err)
	}
	send(t, s, "first")

	builds := 0
	resumed, err := runner(st, fake, "prompt B", &builds).Resume(ctx, s.ID())
	if err != nil {
		t.Fatal(err)
	}
	send(t, resumed, "second")
	if builds != 0 {
		t.Fatal("Resume must use the stored prompt, never rebuild it")
	}
	if got := decode(t, fake.Messages(1))[0].Content; got != "prompt A" {
		t.Fatalf("resumed system prompt = %q, want the stored one", got)
	}
}

func TestEachRequestExtendsThePreviousOneByteForByte(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "r1"}, fakellm.Turn{Content: "r2"}, fakellm.Turn{Content: "r3"})
	s, _ := runner(st, fake, "sys", nil).NewSession(ctx, "interactive", "/w")
	for _, q := range []string{"q1", "q2", "q3"} {
		send(t, s, q)
	}
	for i := 0; i < 2; i++ {
		prev, next := fake.Messages(i), fake.Messages(i+1)
		if len(next) != len(prev)+2 {
			t.Fatalf("request %d has %d messages, want %d", i+1, len(next), len(prev)+2)
		}
		for j := range prev {
			if !bytes.Equal(prev[j], next[j]) {
				t.Fatalf("request %d message %d changed:\n%s\n%s", i+1, j, prev[j], next[j])
			}
		}
		added := decode(t, next[len(prev):])
		if added[0].Role != "assistant" || added[0].Content != []string{"r1", "r2"}[i] || added[1].Role != "user" {
			t.Fatalf("request %d appended %+v", i+1, added)
		}
	}
}

func TestRolesAlternate(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "a"}, fakellm.Turn{Content: "b"}, fakellm.Turn{Content: "c"})
	s, _ := runner(st, fake, "sys", nil).NewSession(ctx, "interactive", "/w")
	for _, q := range []string{"1", "2", "3"} {
		send(t, s, q)
	}
	var roles []string
	for _, m := range decode(t, fake.Messages(2)) {
		roles = append(roles, m.Role)
	}
	if strings.Join(roles, ",") != "system,user,assistant,user,assistant,user" {
		t.Fatalf("roles = %v", roles)
	}
}

func TestAFailedTurnLeavesNoTrace(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{Content: "hello"},
		fakellm.Turn{Status: 503}, fakellm.Turn{Status: 503}, // MaxAttempts 2: the turn fails
		fakellm.Turn{Content: "back"},
	)
	s, _ := runner(st, fake, "sys", nil).NewSession(ctx, "interactive", "/w")
	send(t, s, "one")
	if _, err := s.Send(ctx, "two", nil); err == nil {
		t.Fatal("the second turn must fail")
	}
	msgs, _ := st.Messages(ctx, s.ID())
	if len(msgs) != 2 {
		t.Fatalf("stored %d messages after a failed turn, want 2 (the failed turn rolled back)", len(msgs))
	}
	send(t, s, "three")
	got := decode(t, fake.Messages(3))
	var roles []string
	for _, m := range got {
		roles = append(roles, m.Role)
	}
	if strings.Join(roles, ",") != "system,user,assistant,user" || got[3].Content != "three" {
		t.Fatalf("after a failed turn the next request is %v / %q", roles, got[3].Content)
	}
}

func TestAnInterruptedTurnLeavesNoTrace(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Hang: true})
	s, _ := runner(st, fake, "sys", nil).NewSession(ctx, "interactive", "/w")
	cctx, cancel := context.WithCancel(ctx)
	time.AfterFunc(50*time.Millisecond, cancel)
	if _, err := s.Send(cctx, "slow", nil); !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
	if msgs, _ := st.Messages(ctx, s.ID()); len(msgs) != 0 {
		t.Fatalf("an interrupted first turn stored %d messages", len(msgs))
	}
}

func TestTheStoredAPIContentIsWhatGetsReplayed(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "next answer"})
	s, _ := runner(st, fake, "sys", nil).NewSession(ctx, "interactive", "/w")
	// An earlier turn whose user message was sent with an injection.
	if _, err := st.AppendMessages(ctx, s.ID(),
		store.Message{Role: "user", Content: "visible", APIContent: "visible\n\n<recalled-notes>n</recalled-notes>"},
		store.Message{Role: "assistant", Content: "ok"},
	); err != nil {
		t.Fatal(err)
	}
	resumed, err := runner(st, fake, "sys", nil).Resume(ctx, s.ID())
	if err != nil {
		t.Fatal(err)
	}
	send(t, resumed, "next")
	got := decode(t, fake.Messages(0))
	if got[1].Content != "visible\n\n<recalled-notes>n</recalled-notes>" {
		t.Fatalf("replayed user content = %q, want the stored api_content", got[1].Content)
	}
	msgs, _ := st.Messages(ctx, s.ID())
	last := msgs[len(msgs)-2]
	if last.Role != "user" || last.Content != "next" || last.APIContent != "next" {
		t.Fatalf("new user row = %+v; api_content must record exactly what was sent", last)
	}
}

func TestDeltasStreamAndReasoningIsStoredButNotReplayed(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{Content: "abcdef", Reasoning: "hmm", ChunkRunes: 2, PromptTokens: 9, CompletionTokens: 3},
		fakellm.Turn{Content: "second"},
	)
	s, _ := runner(st, fake, "sys", nil).NewSession(ctx, "interactive", "/w")
	var content, reasoning strings.Builder
	res, err := s.Send(ctx, "go", func(e agent.Event) {
		switch e.Kind {
		case "assistant.delta":
			content.WriteString(e.Text)
		case "reasoning.delta":
			reasoning.WriteString(e.Text)
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	if content.String() != "abcdef" || reasoning.String() != "hmm" {
		t.Fatalf("events: content %q reasoning %q", content.String(), reasoning.String())
	}
	if res.Content != "abcdef" || res.Reasoning != "hmm" || res.Usage.PromptTokens != 9 || res.Usage.CompletionTokens != 3 {
		t.Fatalf("result = %+v", res)
	}
	msgs, _ := st.Messages(ctx, s.ID())
	if msgs[1].Role != "assistant" || msgs[1].Content != "abcdef" || msgs[1].Reasoning != "hmm" {
		t.Fatalf("stored assistant = %+v", msgs[1])
	}
	send(t, s, "again")
	if bytes.Contains(fake.Messages(1)[2], []byte("hmm")) {
		t.Fatalf("past reasoning went back on the wire: %s", fake.Messages(1)[2])
	}
}

func TestTheRunnerSendsItsModelAndEffort(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "x"}, fakellm.Turn{Content: "y"})
	r := runner(st, fake, "sys", nil)
	s, _ := r.NewSession(ctx, "interactive", "/w")
	send(t, s, "a")
	if req := fake.Request(0); req["model"] != "m" || req["reasoning_effort"] != "high" {
		t.Fatalf("request = %v", req)
	}
	r.ReasoningEffort = ""
	s2, _ := r.NewSession(ctx, "interactive", "/w")
	send(t, s2, "b")
	if _, present := fake.Request(1)["reasoning_effort"]; present {
		t.Fatal("no effort configured: reasoning_effort must be omitted")
	}
}
