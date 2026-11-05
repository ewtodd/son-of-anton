package agent_test

import (
	"bytes"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
)

// A user message's send time is rendered once into its API copy, never into
// the stored content; later turns replay those exact bytes even when the zone
// changes.
func TestSendTimeIsInTheRequestAndNotTheStoredContent(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "one"}, fakellm.Turn{Content: "two"})
	r := runner(st, fake, "sys", nil)
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
// time from a terminal.
func TestGuidanceExplainsTheSendTimePrefixWithoutATerminal(t *testing.T) {
	g := prompt.Guidance
	if !contains(g, "[Day YYYY-MM-DD HH:MM ZONE]") || !contains(g, "was sent at that time") || !contains(g, "otherwise do not guess") {
		t.Fatalf("guidance lacks the send-time rule:\n%s", g)
	}
}

func contains(s, sub string) bool { return bytes.Contains([]byte(s), []byte(sub)) }
