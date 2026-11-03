package server_test

// Phase 8g: fiona knows which model answered, /model in a chat, a dead model
// pin falls back, declared renames (daemon/phases/08g-models-and-cleanup.md).

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

const modelNote = "[Model: your last reply came from eklow/ornith-x.]"

func TestFionaIsToldWhichModelAnsweredLast(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, Content: "first answer", ServedModel: "eklow/ornith-x"},
		fakellm.Turn{When: asFiona, Content: "second answer", ServedModel: "eklow/ornith-x"},
	)
	r := newChatRig(t, fake, "")
	r.say("hello")
	r.reply(sigGroup, "first answer")
	first := lastUser(t, fake, 0)
	if strings.Contains(first, "[Model:") {
		t.Fatalf("a note before any reply: %s", first)
	}
	r.say("which model are you?")
	r.reply(sigGroup, "second answer")
	if u := lastUser(t, fake, 1); !strings.HasSuffix(strings.TrimSpace(u), modelNote) || !strings.Contains(u, "which model are you?") {
		t.Fatalf("the second message's context: %q", u)
	}
	// The earlier message replays byte for byte: the note never rewrites history.
	replayed := false
	for _, m := range wire(t, fake, 1) {
		if str(m, "role") == "user" && str(m, "content") == first {
			replayed = true
		}
	}
	if !replayed {
		t.Fatal("the first message did not replay unchanged")
	}
}

func TestTheModelCommandInAChat(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, Content: "one", ServedModel: "eklow/ornith-x"},
		fakellm.Turn{When: asFiona, Content: "two"},
	)
	r := newChatRig(t, fake, "")
	r.say("hi")
	r.reply(sigGroup, "one")
	r.say("/model")
	r.replyLike(sigGroup, "Model: test-model", "the last reply came from eklow/ornith-x", "/model <name> switches this chat")
	r.say("/model nosuch")
	r.replyLike(sigGroup, "Unknown model nosuch. Known:", "other-model", "test-model")
	r.say("/model other-model")
	r.reply(sigGroup, "Model: other-model, from the next message.")
	r.say("again")
	r.reply(sigGroup, "two")
	if got := fake.Request(1)["model"]; got != "other-model" {
		t.Fatalf("after /model other-model the request used %v", got)
	}
	r.say("/nonsense")
	r.replyLike(sigGroup, "/nonsense is not a command here", "/model")
}

func TestADeadModelPinFallsBackToTheDefault(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{When: `"model":"gone-model"`, Status: 404,
			Body: `{"error":{"message":"unknown model gone-model","type":"invalid_request_error","code":"model_not_found"}}`},
		fakellm.Turn{Content: "back on the default"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.ok("session.set", map[string]any{"session": id, "model": "gone-model"})
	c.send(id, "hi")
	evs := c.turn()
	if end := of(evs, "turn.end"); len(end) != 1 || str(end[0], "status") != "done" {
		t.Fatalf("turn.end = %v", end)
	}
	noted := false
	for _, n := range of(evs, "notice") {
		if strings.Contains(str(n, "text"), "gone-model is no longer served; this session is back on test-model.") {
			noted = true
		}
	}
	if !noted {
		t.Fatalf("no notice among %v", of(evs, "notice"))
	}
	if got := fake.Request(1)["model"]; got != "test-model" {
		t.Fatalf("the retry used %v", got)
	}
	if s := obj(c.attach(id, "driver"), "session"); s["model"] != "test-model" {
		t.Fatalf("the session kept %v", s["model"])
	}
}

func TestADeclaredRenameRewritesPinsAtStart(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "renamed ok"})
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.ok("session.set", map[string]any{"session": id, "model": "old/name"})
	c.close()

	f, err := os.OpenFile(filepath.Join(h.home, "config.toml"), os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString("\n[models.renamed]\n\"old/name\" = \"other-model\"\n"); err != nil {
		t.Fatal(err)
	}
	f.Close()
	h.restart()

	c = h.dial()
	if s := obj(c.attach(id, "driver"), "session"); s["model"] != "other-model" {
		t.Fatalf("after a restart with the rename, the session is on %v", s["model"])
	}
	c.send(id, "hi")
	c.turn()
	if got := fake.Request(0)["model"]; got != "other-model" {
		t.Fatalf("the request used %v", got)
	}
}
