package server_test

// /handoff: the terminal gives its session to the chat and leaves.

import (
	"context"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/signalfake"
)

func TestHandoffGivesARunningSessionToTheChat(t *testing.T) {
	dir := t.TempDir()
	rel := make(chan struct{})
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, Content: "Hello."},
		fakellm.Turn{When: asWorker, Content: "long job done", Release: rel},
		fakellm.Turn{When: workerNotice, Content: "Your session is with me now."},
		fakellm.Turn{When: workerNotice, Content: "The long job is done."},
	)
	r := newChatRig(t, fake, singleUser)
	r.say("hello")
	r.reply(sigGroup, "Hello.")

	c := r.h.dial()
	desk := c.create(dir)
	c.attach(desk, "driver")
	c.send(desk, "start the long job")
	c.await("turn.start")
	c.ok("session.handoff", map[string]any{"session": desk})
	c.close()

	r.reply(sigGroup, "Your session is with me now.")
	notice := lastUser(t, fake, requestWith(t, fake, "handed to this chat"))
	if !strings.HasPrefix(notice, "[worker ") || !strings.Contains(notice, desk) || !strings.Contains(notice, "it is running") {
		t.Fatalf("the chat hears = %q", notice)
	}
	close(rel)
	r.reply(sigGroup, "The long job is done.")
	st := openStore(t, r.h.home)
	if s, err := st.Session(context.Background(), desk); err != nil || s.Owner == "" || s.Kind != "interactive" {
		t.Fatalf("the session = %+v, %v", s, err)
	}
}

func TestHandoffMovesAWaitingApproval(t *testing.T) {
	dir := t.TempDir()
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, Content: "Hello."},
		fakellm.Turn{When: asWorker, ToolCalls: []fakellm.ToolCall{terminal("c1", "chmod 777 f.txt")}},
		fakellm.Turn{When: workerNotice, Content: "It is with me."},
	)
	r := newChatRig(t, fake, singleUser)
	r.say("hello")
	r.reply(sigGroup, "Hello.")
	c := r.h.dial()
	desk := c.create(dir)
	c.attach(desk, "driver")
	c.send(desk, "open it up")
	c.await("approval.request")
	c.ok("session.handoff", map[string]any{"session": desk})
	c.close()
	r.replyLike(sigGroup, "Worker ", "⚠️ Permission requested", "chmod 777")
}

func TestHandoffNeedsAChatToGoTo(t *testing.T) {
	for name, extra := range map[string]string{"no chat yet": singleUser, "shared instance": ""} {
		t.Run(name, func(t *testing.T) {
			fake := fakellm.New(t)
			r := newChatRig(t, fake, extra)
			c := r.h.dial()
			id := c.create(t.TempDir())
			c.attach(id, "driver")
			c.fails("session.handoff", map[string]any{"session": id}, -32602)
		})
	}
	// Only the driver can give a session away.
	h := newHarness(t, fakellm.New(t), "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.fails("session.handoff", map[string]any{"session": id}, -32010)
}

// One group can reach a single-user instance under more than one stamp, so
// who took a session on says nothing about who may let it go.
func TestAnyChatLetsATerminalSessionGo(t *testing.T) {
	var desk string
	fake := fakellm.New(t,
		fakellm.Turn{When: asWorker, Content: "desk work done"},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relaySendTo("r1", &desk, "carry on")}},
		fakellm.Turn{When: asFiona, Content: "Sent."},
		fakellm.Turn{When: asWorker, Content: "carried on"},
		fakellm.Turn{When: workerNotice, Content: "It carried on."},
		fakellm.Turn{When: "let it go", ToolCalls: []fakellm.ToolCall{{ID: "r2", Name: "relay", ArgumentsFunc: func() string {
			return `{"action":"detach","session":"` + desk + `"}`
		}}}},
		fakellm.Turn{When: asFiona, Content: "Let go."},
	)
	r := newChatRig(t, fake, singleUser)
	desk = deskSession(t, r, t.TempDir(), "start at the desk")
	r.say("carry on")
	r.reply(sigGroup, "It carried on.")

	r.ts++
	r.sig.Push(signalfake.GroupMessage(sigMember, sigGroup, "let it go", r.ts))
	r.reply(sigGroup, "Let go.")
	st := openStore(t, r.h.home)
	if s, err := st.Session(context.Background(), desk); err != nil || s.Owner != "" {
		t.Fatalf("the session = %+v, %v", s, err)
	}
}
