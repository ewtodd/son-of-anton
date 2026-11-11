package server_test

// In a group whose one session every member shares (group_sessions_per_user
// = false), each message reaches fiona with its sender's name, and her prompt
// says so; a group of per-member sessions is unchanged.

import (
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
	"github.com/ewtodd/son-of-anton/daemon/internal/signalfake"
)

func TestASharedGroupNamesEachSender(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "one"}, fakellm.Turn{Content: "two"})
	r := newChatRig(t, fake, "[gateway]\ngroup_sessions_per_user = false\n")
	r.ts++
	r.sig.Push(signalfake.GroupMessage(sigMember, sigGroup, "I am the owner", r.ts))
	r.reply(sigGroup, "one")
	if got := lastUser(t, fake, 0); got != "Member: I am the owner" {
		t.Fatalf("a member's message reached fiona as %q", got)
	}
	r.say("hi")
	r.reply(sigGroup, "two")
	if got := lastUser(t, fake, 1); !strings.HasPrefix(got, sigAccount+": hi\n") {
		t.Fatalf("the owner's message (no profile name in the envelope) reached fiona as %q", got)
	}
	if sys := systemPrompt(t, fake, 0); !strings.Contains(sys, "People: this is a group chat") {
		t.Fatalf("a shared group's prompt does not say messages name their senders:\n%.600s", sys)
	}
}

func TestPerMemberGroupSessionsAreUnlabelled(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "one"})
	r := newChatRig(t, fake, "")
	r.say("hi")
	r.reply(sigGroup, "one")
	if got := lastUser(t, fake, 0); got != "hi" {
		t.Fatalf("a member's own session labelled the message: %q", got)
	}
	if sys := systemPrompt(t, fake, 0); !strings.HasPrefix(sys, prompt.FionaIdentity("", nil, nil)) {
		t.Fatalf("a per-member session's prompt changed:\n%.600s", sys)
	}
}

func TestASharedGroupLabelsSendnowAndTidiesNames(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "one"}, fakellm.Turn{Content: "two"})
	r := newChatRig(t, fake, "[gateway]\ngroup_sessions_per_user = false\n")
	r.ts++
	msg := signalfake.GroupMessage(sigMember, sigGroup, "/sendnow look at this", r.ts)
	msg["envelope"].(map[string]any)["sourceName"] = "  Sam\n  Lee " + strings.Repeat("x", 50)
	r.sig.Push(msg)
	r.reply(sigGroup, "one")
	if got := lastUser(t, fake, 0); got != "Sam Lee "+strings.Repeat("x", 32)+": look at this" {
		t.Fatalf("/sendnow's text reached fiona as %q; the name on one line, at most 40 characters", got)
	}
}

func TestASharedGroupsPromptNamesItsMembers(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "one"})
	r := newChatRig(t, fake, "[gateway]\ngroup_sessions_per_user = false\n")
	r.sig.SetGroup(sigGroup, signalfake.Contact{Number: sigMember, Name: "Sam Lee"},
		signalfake.Contact{Number: sigAccount, Profile: "Zed Owner"})
	r.say("who is in this chat?")
	r.reply(sigGroup, "one")
	if sys := systemPrompt(t, fake, 0); !strings.Contains(sys, "Its members are Zed Owner (the owner) and Sam Lee; ") {
		t.Fatalf("a shared group's prompt does not name its members, the owner first:\n%.900s", sys)
	}
}
