package server

// Active hours for Signal chats (phase 5e): outside gateway.active_hours a
// chat's messages are saved, not answered, and when the window opens the chat
// is asked whether to go ahead. Reference: gateway/active_hours.py,
// gateway/held_messages.py and gateway/run.py.

import (
	"fmt"
	"log"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/signal"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

const (
	heldTextMax     = 2000
	heldShownText   = 200
	heldShownCount  = 10
	heldDiscarded   = "Held messages discarded."
	heldReminder    = "Reply **yes** to process the held messages, or **no** to discard them."
	heldPrompt      = "Process these? Reply **yes** to proceed or **no** to discard."
	heldTurnLead    = "The following messages were held while I was off duty. Please address them:"
	heldNothingLeft = "Nothing in the held messages could be processed."
)

var clock atomic.Pointer[func() time.Time]

// now is the server's clock.
func now() time.Time {
	if f := clock.Load(); f != nil {
		return (*f)()
	}
	return time.Now()
}

// setClock makes f the clock (nil: the real one) and returns the one it
// replaced.
func setClock(f func() time.Time) (old func() time.Time) {
	var p *func() time.Time
	if f != nil {
		p = &f
	}
	if prev := clock.Swap(p); prev != nil {
		return *prev
	}
	return nil
}

// hourIn says whether the local hour of t is in [start, end), wrapping
// midnight when start > end.
func hourIn(start, end int, t time.Time) bool {
	h := t.In(workerZone()).Hour()
	if start < end {
		return h >= start && h < end
	}
	return h >= start || h < end
}

// gapKey identifies the closed stretch t falls in: the date it began and the
// end hour.
func gapKey(end int, t time.Time) string {
	l := t.In(workerZone())
	if l.Hour() < end {
		l = l.AddDate(0, 0, -1)
	}
	return fmt.Sprintf("%s:%02d", l.Format("2006-01-02"), end)
}

// inactiveNotice is what a chat is told outside the window.
func (s *Server) inactiveNotice(start, end int) string {
	if m := strings.TrimSpace(s.cfg.Gateway.InactiveMessage); m != "" {
		return m
	}
	return fmt.Sprintf("💤 Not on duty right now — I run %02d:00–%02d:00. Your message is saved; I'll ask about it when I'm back.", start, end)
}

// holdWords and discardWords answer the question about held messages.
var (
	holdYes = map[string]bool{"yes": true, "y": true, "go": true, "proceed": true}
	holdNo  = map[string]bool{"no": true, "n": true, "skip": true, "discard": true}
)

// controlCommands are the commands command() knows; any of them drops an
// open question about held messages.
var controlCommands = map[string]bool{"stop": true, "new": true, "sendnow": true, "sendall": true, "status": true, "approve": true, "deny": true}

func heldSummary(msgs []store.Held) string {
	lines := []string{}
	for i, m := range msgs {
		if i == heldShownCount {
			lines = append(lines, fmt.Sprintf("  … and %d more", len(msgs)-heldShownCount))
			break
		}
		sender := m.Sender
		if sender == "" {
			sender = "someone"
		}
		text := strings.TrimSpace(m.Text)
		if r := []rune(text); len(r) > heldShownText {
			text = string(r[:heldShownText]) + "…"
		}
		if text == "" {
			text = "(empty or media-only)"
		}
		lines = append(lines, "  • "+sender+": "+text)
	}
	plural := "s"
	if len(msgs) == 1 {
		plural = ""
	}
	return strconv.Itoa(len(msgs)) + " message" + plural + " came in while I was off duty:\n\n" +
		strings.Join(lines, "\n") + "\n\n" + heldPrompt
}

// heldTurn is the one turn the held messages become ("" when none has text).
func heldTurn(msgs []store.Held) string {
	var parts []string
	for _, m := range msgs {
		sender := m.Sender
		if sender == "" {
			sender = "someone"
		}
		if text := strings.TrimSpace(m.Text); text != "" {
			parts = append(parts, "["+sender+"]: "+text)
		}
	}
	if len(parts) == 0 {
		return ""
	}
	return heldTurnLead + "\n\n" + strings.Join(parts, "\n\n")
}

// holdGate applies the window to an ordinary message. It reports whether the
// message was used up (held, or the answer to the question about held
// messages); a returned turn is the text to run in its place.
func (h *chatHub) holdGate(cs *chatState, in signal.Inbound) (used bool, turn string) {
	s := h.srv
	start, end, ok, _ := s.cfg.ActiveHours()
	if !ok {
		return false, ""
	}
	t := now()
	cs.hoursMu.Lock()
	defer cs.hoursMu.Unlock()
	hold := func() bool {
		text := in.Text
		if r := []rune(text); len(r) > heldTextMax {
			text = string(r[:heldTextMax])
		}
		sender := in.SenderName
		if sender == "" {
			sender = in.Sender
		}
		if sender == "" {
			sender = "someone"
		}
		if err := s.st.HoldMessage(s.base, cs.stamp, store.Held{Sender: sender, Text: text, TS: t.Unix()}); err != nil {
			log.Printf("signal: holding a message of %s: %v", cs.stamp, err)
			cs.say("Sorry, that failed: " + err.Error())
			return false
		}
		return true
	}
	if !hourIn(start, end, t) {
		if !hold() {
			return true, ""
		}
		if gap := gapKey(end, t); cs.told != gap {
			cs.told = gap
			cs.say(s.inactiveNotice(start, end))
		}
		return true, ""
	}
	asked, err := s.st.HeldAsked(s.base, cs.stamp)
	if err != nil {
		log.Printf("signal: reading the held question of %s: %v", cs.stamp, err)
		return false, ""
	}
	if asked {
		return true, h.answerHeld(cs, in.Text)
	}
	if held, err := s.st.HeldMessages(s.base, cs.stamp); err == nil && len(held) > 0 {
		if hold() {
			h.askHeld(cs)
		}
		return true, ""
	}
	return false, ""
}

// answerHeld handles the reply to the question about held messages, and
// returns the turn to run when it was yes. The caller holds cs.hoursMu.
func (h *chatHub) answerHeld(cs *chatState, text string) string {
	s := h.srv
	switch word := strings.ToLower(strings.TrimSpace(text)); {
	case holdYes[word]:
		msgs, err := s.st.HeldMessages(s.base, cs.stamp)
		if err == nil {
			err = s.st.ClearHeld(s.base, cs.stamp)
		}
		if err != nil {
			cs.say("Sorry, that failed: " + err.Error())
			return ""
		}
		turn := heldTurn(msgs)
		if turn == "" {
			cs.say(heldNothingLeft)
		}
		return turn
	case holdNo[word]:
		if err := s.st.ClearHeld(s.base, cs.stamp); err != nil {
			cs.say("Sorry, that failed: " + err.Error())
			return ""
		}
		cs.say(heldDiscarded)
	default:
		cs.say(heldReminder)
	}
	return ""
}

// askHeld sends the summary of the chat's held messages and marks the chat
// asked. The caller holds cs.hoursMu.
func (h *chatHub) askHeld(cs *chatState) {
	s := h.srv
	msgs, err := s.st.HeldMessages(s.base, cs.stamp)
	if err != nil || len(msgs) == 0 {
		return
	}
	if err := s.st.SetHeldAsked(s.base, cs.stamp, true); err != nil {
		log.Printf("signal: marking %s asked: %v", cs.stamp, err)
		return
	}
	cs.say(heldSummary(msgs))
}

// dropHeldQuestion is what a control command does to an open question: the
// held messages are cleared.
func (h *chatHub) dropHeldQuestion(cs *chatState) {
	cs.hoursMu.Lock()
	defer cs.hoursMu.Unlock()
	if asked, _ := h.srv.st.HeldAsked(h.srv.base, cs.stamp); asked {
		if err := h.srv.st.ClearHeld(h.srv.base, cs.stamp); err != nil {
			log.Printf("signal: dropping the held question of %s: %v", cs.stamp, err)
		}
	}
}

// offerHeld asks every chat that holds messages and has not been asked, when
// the window is open.
func (h *chatHub) offerHeld() {
	s := h.srv
	start, end, ok, warn := s.cfg.ActiveHours()
	if warn != "" {
		log.Printf("signal: %s", warn)
	}
	if !ok || s.base.Err() != nil || !hourIn(start, end, now()) {
		return
	}
	chats, err := s.st.HeldChats(s.base)
	if err != nil {
		log.Printf("signal: listing chats with held messages: %v", err)
		return
	}
	for _, stamp := range chats {
		cs := h.state(stamp)
		cs.hoursMu.Lock()
		if asked, err := s.st.HeldAsked(s.base, stamp); err == nil && !asked {
			h.askHeld(cs)
		}
		cs.hoursMu.Unlock()
	}
}
