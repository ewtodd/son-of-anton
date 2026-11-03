package server_test

// Phase 5e: active hours for Signal chats (daemon/phases/05e-active-hours.md).
// The clock is set with server.SetNow; local time is UTC.

import (
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/server"
)

const hoursCfg = "[gateway]\nactive_hours = [20, 7]\ninactive_message = \"Off the clock until 8pm.\"\n"

// at is 2026-11-03 h:30 UTC.
func at(h int) func() time.Time {
	return func() time.Time { return time.Date(2026, 11, 3, h, 30, 0, 0, time.UTC) }
}

// sendsTo counts the messages sent to group g that contain part.
func sendsTo(r *chatRig, g, part string) int {
	n := 0
	for _, m := range r.sig.Sends() {
		if m.To == "group:"+g && strings.Contains(m.Text, part) {
			n++
		}
	}
	return n
}

// restartDaemon restarts the daemon and waits for it to be back on the stream.
func restartDaemon(r *chatRig) {
	r.t.Helper()
	before := r.sig.Connects()
	r.h.restart()
	waitUntil(r.t, "the daemon back on the event stream", func() bool { return r.sig.Connects() > before })
}

func TestOutsideActiveHoursMessagesAreHeldAndTheChatToldOnce(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	t.Cleanup(server.SetNow(at(12)))
	fake := fakellm.New(t)
	r := newChatRig(t, fake, hoursCfg)
	r.say("first question")
	r.reply(sigGroup, "Off the clock until 8pm.")
	r.say("second question")
	r.say("/status") // control commands work at any hour
	r.replyLike(sigGroup, "Session:")
	r.quiet(0)
	if n := sendsTo(r, sigGroup, "Off the clock"); n != 1 {
		t.Fatalf("the chat was told %d times in one closed stretch", n)
	}
}

func TestHeldMessagesAreOfferedWhenTheWindowOpens(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	t.Cleanup(server.SetNow(at(12)))
	fake := fakellm.New(t, fakellm.Turn{When: asFiona, Content: "Answered both."})
	r := newChatRig(t, fake, hoursCfg)
	r.say("first question")
	r.reply(sigGroup, "Off the clock until 8pm.")
	r.say("second question")
	r.say("/status")
	r.replyLike(sigGroup, "Session:") // the second is held by now

	server.SetNow(at(21))
	restartDaemon(r) // a start inside the window offers what was held
	r.replyLike(sigGroup, "2 messages came in while I was off duty:",
		"  • +15550000000: first question", "  • +15550000000: second question",
		"Process these? Reply **yes** to proceed or **no** to discard.")
	r.say("maybe")
	r.reply(sigGroup, "Reply **yes** to process the held messages, or **no** to discard them.")
	r.say("YES")
	r.reply(sigGroup, "Answered both.")
	u := lastUser(t, fake, 0)
	for _, want := range []string{
		"The following messages were held while I was off duty. Please address them:",
		"[+15550000000]: first question", "[+15550000000]: second question",
	} {
		if !strings.Contains(u, want) {
			t.Fatalf("the held turn lacks %q:\n%s", want, u)
		}
	}
	if strings.Contains(u, "maybe") || strings.Contains(u, "/status") {
		t.Fatalf("only held messages make the turn:\n%s", u)
	}
}

func TestNoDiscardsAndAMessageInsideTheWindowAsksFirst(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	t.Cleanup(server.SetNow(at(12)))
	fake := fakellm.New(t, fakellm.Turn{When: asFiona, Content: "Fresh answer."})
	r := newChatRig(t, fake, "[gateway]\nactive_hours = [20, 7]\n")
	r.say("early bird")
	r.reply(sigGroup, "💤 Not on duty right now — I run 20:00–07:00. Your message is saved; I'll ask about it when I'm back.")

	server.SetNow(at(22)) // no restart: the next message inside the window asks first
	r.say("are you there")
	r.replyLike(sigGroup, "2 messages came in while I was off duty:", "early bird", "are you there")
	r.say("no")
	r.reply(sigGroup, "Held messages discarded.")
	r.quiet(0)
	r.say("a new question")
	r.reply(sigGroup, "Fresh answer.")
	if u := lastUser(t, fake, 0); strings.Contains(u, "early bird") || !strings.Contains(u, "a new question") {
		t.Fatalf("after no: %s", u)
	}
}

func TestAControlCommandDropsTheQuestion(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	t.Cleanup(server.SetNow(at(12)))
	fake := fakellm.New(t, fakellm.Turn{When: asFiona, Content: "Just this one."})
	r := newChatRig(t, fake, hoursCfg)
	r.say("held one")
	r.reply(sigGroup, "Off the clock until 8pm.")
	server.SetNow(at(21))
	restartDaemon(r)
	r.replyLike(sigGroup, "1 message came in while I was off duty:", "held one")
	r.say("/new")
	r.reply(sigGroup, "New session started.")
	r.say("only this")
	r.reply(sigGroup, "Just this one.")
	if u := lastUser(t, fake, 0); strings.Contains(u, "held one") {
		t.Fatalf("a command left the held messages: %s", u)
	}
}

func TestNoWindowMeansAlwaysOpen(t *testing.T) {
	t.Cleanup(server.SetNow(at(12)))
	fake := fakellm.New(t, fakellm.Turn{When: asFiona, Content: "Any time."})
	r := newChatRig(t, fake, "")
	r.say("midday")
	r.reply(sigGroup, "Any time.")
}
