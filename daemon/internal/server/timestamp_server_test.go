package server_test

// A Signal chat message carries its own send time in the model's API copy,
// once, persisted as api_content; stored content stays clean. The switch
// [agent] message_timestamps turns it off.
import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
)

func TestSignalSendTimeRidesTheChatAPICopy(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "one"}, fakellm.Turn{Content: "two"})
	r := newChatRig(t, fake, "")
	ts := r.say("what time is it?")
	r.reply(sigGroup, "one")
	want := prompt.FormatSentAt(time.UnixMilli(ts).In(prompt.LocalZone())) + " what time is it?"
	req1 := rawWire(t, fake, 0)
	if got := str(req1[1], "content"); got != want {
		t.Fatalf("first request user = %q, want %q", got, want)
	}

	st := openStore(t, r.h.home)
	id, err := st.ChatSession(context.Background(), "signal:group:"+sigGroup+":"+sigAccount)
	if err != nil {
		t.Fatal(err)
	}
	msgs, err := st.Messages(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	if msgs[0].Role != "user" || msgs[0].Content != "what time is it?" || msgs[0].APIContent != want {
		t.Fatalf("stored user row = %+v; content stays clean and api_content carries the prefix", msgs[0])
	}

	ts2 := r.say("again")
	r.reply(sigGroup, "two")
	req2 := rawWire(t, fake, 1)
	if got := str(req2[1], "content"); got != want {
		t.Fatalf("the first message did not replay its original bytes: %q", got)
	}
	want2 := prompt.FormatSentAt(time.UnixMilli(ts2).In(prompt.LocalZone())) + " again"
	if got := str(req2[3], "content"); !strings.HasPrefix(got, want2) {
		t.Fatalf("second request user = %q, want it to start with %q", got, want2)
	}
}

func TestChatMessageTimestampSwitchOff(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "ok"})
	r := newChatRig(t, fake, "[agent]\nmessage_timestamps = false\n")
	r.say("hi")
	r.reply(sigGroup, "ok")
	if got := lastUser(t, fake, 0); got != "hi" {
		t.Fatalf("request user = %q; the switch is off", got)
	}
}
