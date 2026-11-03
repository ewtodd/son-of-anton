package server_test

// A chat follows the instance's chat model (gateway.model) until /model in
// that chat chooses one; /model default follows it again.

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

func TestAChatFollowsTheInstanceModelUntilModelPinsOne(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, Content: "one"},
		fakellm.Turn{When: asFiona, Content: "two"},
		fakellm.Turn{When: asFiona, Content: "three"},
		fakellm.Turn{When: asFiona, Content: "four"},
	)
	r := newChatRig(t, fake, "")
	r.say("hi")
	r.reply(sigGroup, "one")
	if got := fake.Request(0)["model"]; got != "test-model" {
		t.Fatalf("first request on %v", got)
	}

	// The instance's chat model changes: the existing chat follows it.
	f, err := os.OpenFile(filepath.Join(r.h.home, "config.toml"), os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString("\n[gateway]\nmodel = \"other-model\"\n"); err != nil {
		t.Fatal(err)
	}
	f.Close()
	before := r.sig.Connects()
	r.h.restart()
	waitUntil(t, "the daemon back on the event stream", func() bool { return r.sig.Connects() > before })
	r.say("again")
	r.reply(sigGroup, "two")
	if got := fake.Request(1)["model"]; got != "other-model" {
		t.Fatalf("after gateway.model changed, the existing chat used %v", got)
	}

	r.say("/model test-model")
	r.reply(sigGroup, "Model: test-model, from the next message.")
	r.say("pinned")
	r.reply(sigGroup, "three")
	if got := fake.Request(2)["model"]; got != "test-model" {
		t.Fatalf("after /model test-model the chat used %v", got)
	}

	r.say("/model default")
	r.reply(sigGroup, "Model: other-model (this instance's model), from the next message.")
	r.say("following")
	r.reply(sigGroup, "four")
	if got := fake.Request(3)["model"]; got != "other-model" {
		t.Fatalf("after /model default the chat used %v", got)
	}
}
