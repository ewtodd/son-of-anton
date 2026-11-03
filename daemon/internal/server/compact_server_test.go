package server_test

// Phase 3a through the socket: the session's context length, /compact
// (preview, focus, history), automatic compaction events, and titles.

import (
	"bytes"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

const codeBusy = -32013

func auxCompaction(aux *fakellm.Server) string {
	return "[auxiliary.compaction]\nbase_url = \"" + aux.URL + "\"\nkey_env = \"SOA_D_TEST_KEY\"\nmodel = \"summarizer\"\n\n"
}

const ctxModel = "[custom_providers.custom.models.\"ctx-model\"]\ncontext_length = 1000\n\n"

var big = strings.Repeat("a", 4000)

func TestTheSessionKnowsItsContextLength(t *testing.T) {
	fake := fakellm.New(t)
	fake.SetModels("other-model")
	fake.SetModelMeta("other-model", map[string]any{"context_length": 32768})
	h := newHarness(t, fake, ctxModel)
	c := h.dial()
	id := c.create(t.TempDir())
	if s := obj(c.attach(id, "driver"), "session"); s["context_length"] != 256000.0 || s["compactions"] != 0.0 {
		t.Fatalf("session = %v; an unknown window is 256000", s)
	}
	r := c.ok("session.set", map[string]any{"session": id, "model": "ctx-model"})
	if obj(r, "session")["context_length"] != 1000.0 {
		t.Fatalf("a route's context_length wins: %v", obj(r, "session"))
	}
	r = c.ok("session.set", map[string]any{"session": id, "model": "other-model"})
	if obj(r, "session")["context_length"] != 32768.0 {
		t.Fatalf("then the provider's /models metadata: %v", obj(r, "session"))
	}
}

func TestTheCompactCommand(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: strings.Repeat("A", 4000)}, fakellm.Turn{Content: "B"})
	aux := fakellm.New(t, fakellm.Turn{Content: "SUMMARY-BODY"})
	h := newHarness(t, fake, auxCompaction(aux))
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, big)
	c.turn()
	c.send(id, "b")
	c.turn()

	r := c.ok("command.run", map[string]any{"session": id, "name": "compact", "args": "--preview"})
	if p := obj(r, "preview"); p["rows"] != 2.0 || p["through_seq"] != 2.0 || p["tokens"].(float64) <= 0 || aux.Count() != 0 {
		t.Fatalf("--preview = %v (summary requests %d); it changes nothing", r, aux.Count())
	}
	r = c.ok("command.run", map[string]any{"session": id, "name": "compact", "args": "the parser"})
	if r["through_seq"] != 2.0 {
		t.Fatalf("compact = %v", r)
	}
	e := c.await("compaction")
	if str(e, "summary") != "SUMMARY-BODY" || e["through_seq"] != 2.0 || e["before_tokens"] == nil {
		t.Fatalf("compaction event = %v", e)
	}
	if !bytes.Contains(aux.Bodies()[0], []byte(`FOCUS: \"the parser\"`)) {
		t.Fatal("the args are the focus topic")
	}
	att := h.dial().attach(id, "viewer")
	cs := objs(att, "compactions")
	if len(cs) != 1 || cs[0]["through_seq"] != 2.0 || str(cs[0], "summary") != "SUMMARY-BODY" || obj(att, "session")["compactions"] != 1.0 {
		t.Fatalf("attach = %v; history carries the compactions", att)
	}
	if len(objs(att, "messages")) != 4 {
		t.Fatalf("history keeps every row: %v", att["messages"])
	}
}

func TestCompactingARunningTurnIsRefused(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Hang: true})
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "x")
	c.await("turn.start")
	c.fails("command.run", map[string]any{"session": id, "name": "compact"}, codeBusy)
	c.ok("turn.interrupt", map[string]any{"session": id})
	c.turn()
}

func TestAutomaticCompactionIsAnEvent(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: strings.Repeat("A", 4000), PromptTokens: 100},
		fakellm.Turn{Content: "B", PromptTokens: 800},
		fakellm.Turn{Content: "C", ChunkRunes: 1, PromptTokens: 100},
	)
	aux := fakellm.New(t, fakellm.Turn{Content: "AUTO-SUMMARY"})
	h := newHarness(t, fake, auxCompaction(aux)+ctxModel)
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.ok("session.set", map[string]any{"session": id, "model": "ctx-model"})
	c.send(id, big)
	c.turn()
	c.send(id, "b")
	c.turn()
	c.send(id, "c")
	evs := c.turn()
	at, delta := indexOf(evs, "compaction"), indexOf(evs, "assistant.delta")
	if at < 0 || delta < at || str(evs[at], "summary") != "AUTO-SUMMARY" {
		t.Fatalf("events = %v; the compaction comes before the turn's reply", kinds(evs))
	}
}

func TestTitlesReachTheSession(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "sure"}, fakellm.Turn{Content: "again"})
	titles := fakellm.New(t, fakellm.Turn{Content: `{"title": "Fix login button on mobile"}`})
	h := newHarness(t, fake, "[auxiliary.title_generation]\nbase_url = \""+titles.URL+"\"\nkey_env = \"SOA_D_TEST_KEY\"\nmodel = \"little-titles\"\n\n")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "the login button is broken on mobile")
	c.awaitWhere("session.updated", func(e map[string]any) bool {
		return str(obj(e, "state"), "title") == "the login button is broken on mobile"
	})
	c.awaitWhere("session.updated", func(e map[string]any) bool {
		return str(obj(e, "state"), "title") == "Fix login button on mobile"
	})
	if titles.Request(0)["model"] != "little-titles" {
		t.Fatalf("title request = %v", titles.Request(0))
	}
	if list := objs(c.ok("session.list", map[string]any{}), "sessions"); str(list[0], "title") != "Fix login button on mobile" {
		t.Fatalf("session.list = %v", list)
	}
	c.send(id, "and on the tablet")
	c.turn()
	if titles.Count() != 1 {
		t.Fatalf("title requests = %d; a session is titled once", titles.Count())
	}
	r := c.ok("session.set", map[string]any{"session": id, "title": "Login bugs"})
	if str(obj(r, "session"), "title") != "Login bugs" {
		t.Fatalf("a title the user sets wins: %v", r)
	}
}

func TestUnderARoutedModelTheWindowIsTheServedModels(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "one", ServedModel: "small/model", PromptTokens: 300, CompletionTokens: 50},
		fakellm.Turn{Content: "two", ServedModel: "big/model", PromptTokens: 400, CompletionTokens: 50},
	)
	fake.SetModels("auto", "small/model", "big/model")
	fake.SetModelMeta("auto", map[string]any{"context_length": 500000}) // the largest on offer
	fake.SetModelMeta("small/model", map[string]any{"context_length": 1000})
	fake.SetModelMeta("big/model", map[string]any{"context_length": 500000})
	h := newHarness(t, fake, "[custom_providers.custom.models.\"auto\"]\n")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.ok("session.set", map[string]any{"session": id, "model": "auto"})
	c.send(id, "hi")
	c.turn()
	st := obj(c.awaitWhere("session.updated", func(e map[string]any) bool { return obj(e, "state")["status"] == "idle" }), "state")
	if st["served_model"] != "small/model" || st["context_length"] != 1000.0 || st["context_tokens"] != 350.0 {
		t.Fatalf("after small/model answered: %v", st)
	}
	c.send(id, "more")
	c.turn()
	st = obj(c.awaitWhere("session.updated", func(e map[string]any) bool { return obj(e, "state")["status"] == "idle" }), "state")
	if st["served_model"] != "big/model" || st["context_length"] != 500000.0 {
		t.Fatalf("after big/model answered: %v", st)
	}
}

// The served model and its window show as soon as the response headers name
// it, and the context tokens after each request: not at the turn's end.
func TestTheServedModelShowsMidTurn(t *testing.T) {
	hold := make(chan struct{})
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("t1", "terminal", `{"command":"true"}`)}, ServedModel: "small/model", PromptTokens: 300, CompletionTokens: 50},
		fakellm.Turn{Content: "done", ServedModel: "small/model", Release: hold},
	)
	fake.SetModels("auto", "small/model")
	fake.SetModelMeta("auto", map[string]any{"context_length": 500000})
	fake.SetModelMeta("small/model", map[string]any{"context_length": 1000})
	h := newHarness(t, fake, "[custom_providers.custom.models.\"auto\"]\n")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.ok("session.set", map[string]any{"session": id, "model": "auto"})
	c.send(id, "hi")
	st := obj(c.awaitWhere("session.updated", func(e map[string]any) bool {
		s := obj(e, "state")
		return s["status"] == "running" && s["context_tokens"] == 350.0
	}), "state")
	if st["served_model"] != "small/model" || st["context_length"] != 1000.0 {
		t.Fatalf("mid-turn, after the first request: %v", st)
	}
	close(hold)
	c.turn()
}
