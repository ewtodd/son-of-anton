package server_test

// An empty reply seen from the clients: the retry is announced, and a reply
// that stays empty ends the turn as an error on every surface, so a turn never
// just stops.

import (
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

func TestAClientIsToldOfARetriedEmptyReply(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{CompletionTokens: 354}, fakellm.Turn{Content: "the answer"})
	h := newHarness(t, fake, "")
	c := h.dial()
	defer c.close()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "q")
	evs := c.turn()
	notices := of(evs, "notice")
	if len(notices) != 1 || str(notices[0], "level") != "warning" || !strings.Contains(str(notices[0], "text"), "empty reply") {
		t.Fatalf("notices = %v (events %v)", notices, kinds(evs))
	}
	if ms := committed(evs); roleList(ms) != "user,assistant" || str(ms[1], "content") != "the answer" {
		t.Fatalf("committed rows = %v", ms)
	}
	if end := evs[len(evs)-1]; str(end, "status") != "done" || end["api_calls"] != 2.0 {
		t.Fatalf("turn.end = %v", end)
	}
}

func TestAReplyThatStaysEmptyEndsTheTurnAsAnError(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{}, fakellm.Turn{}, fakellm.Turn{})
	h := newHarness(t, fake, "")
	c := h.dial()
	defer c.close()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "q")
	evs := c.turn()
	end := evs[len(evs)-1]
	if str(end, "status") != "error" || !strings.Contains(str(end, "error"), "empty reply") {
		t.Fatalf("turn.end = %v", end)
	}
	if ms := committed(evs); roleList(ms) != "user,assistant" || str(ms[1], "content") != "[response failed]" {
		t.Fatalf("committed rows = %v", ms)
	}
}

func TestAChatIsToldOfAReplyThatStaysEmpty(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{}, fakellm.Turn{}, fakellm.Turn{})
	r := newChatRig(t, fake, "")
	r.say("hello")
	r.replyLike(sigGroup, "That did not work: ", "empty reply")
}
