package server_test

// Phase 8i through the daemon: fiona picks a worker's model with relay send,
// and a chat's turn carries a models note (daemon/phases/08i-worker-models.md).

import (
	"context"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// workerRequests are the indexes of the requests a worker made.
func workerRequests(t *testing.T, fake *fakellm.Server) []int {
	t.Helper()
	var out []int
	for i := 0; i < fake.Count(); i++ {
		if strings.HasPrefix(systemPrompt(t, fake, i), asWorker) {
			out = append(out, i)
		}
	}
	return out
}

func TestRelaySendChoosesTheWorkersModel(t *testing.T) {
	dir := t.TempDir()
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "send", "new_in": dir, "model": "other-model", "message": "job one"})}},
		fakellm.Turn{When: asFiona, Content: "Started one."},
		fakellm.Turn{When: asWorker, Content: "done one", ServedModel: "served-x"},
		fakellm.Turn{When: asFiona, Content: "Reported one."},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{
			relayCall("r2", map[string]any{"action": "list"}),
			relayCall("r3", map[string]any{"action": "status"}),
		}},
		fakellm.Turn{When: asFiona, Content: "Listed."},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r4", map[string]any{"action": "send", "model": "default", "message": "job two"})}},
		fakellm.Turn{When: asFiona, Content: "Started two."},
		fakellm.Turn{When: asWorker, Content: "done two"},
		fakellm.Turn{When: asFiona, Content: "Reported two."},
	)
	fake.SetModels("test-model", "other-model")
	r := newChatRig(t, fake, "")
	r.say("go")
	r.reply(sigGroup, "Started one.")
	r.reply(sigGroup, "Reported one.")

	sent := toolResult(t, fake, "r1")
	worker, _ := sent["worker"].(string)
	if sent["success"] != true || worker == "" {
		t.Fatalf("send = %v", sent)
	}
	if ws := workerRequests(t, fake); len(ws) != 1 || str(fake.Request(ws[0]), "model") != "other-model" {
		t.Fatalf("the new worker starts on the chosen model: %v", ws)
	}
	st := openStore(t, r.h.home)
	if w, err := st.Session(context.Background(), worker); err != nil || w.Model != "other-model" {
		t.Fatalf("the stored model = %+v, %v", w, err)
	}

	r.say("what do you have")
	r.reply(sigGroup, "Listed.")
	// list and status name each worker's model (and the model that served).
	r.say("again, on the default")
	r.reply(sigGroup, "Started two.")
	r.reply(sigGroup, "Reported two.")

	list := toolResult(t, fake, "r2")
	ws, _ := list["workers"].([]any)
	if len(ws) != 1 {
		t.Fatalf("list = %v", list)
	}
	w0, _ := ws[0].(map[string]any)
	if w0["model"] != "other-model" || w0["served_model"] != "served-x" {
		t.Fatalf("list worker = %v", w0)
	}
	if status := toolResult(t, fake, "r3"); status["model"] != "other-model" || status["served_model"] != "served-x" {
		t.Fatalf("status = %v", status)
	}

	if sent := toolResult(t, fake, "r4"); sent["success"] != true {
		t.Fatalf("send default = %v", sent)
	}
	reqs := workerRequests(t, fake)
	if len(reqs) != 2 || str(fake.Request(reqs[1]), "model") != "test-model" {
		t.Fatalf("\"default\" returns the worker to model.default: %v", reqs)
	}
	if w, err := st.Session(context.Background(), worker); err != nil || w.Model != "test-model" {
		t.Fatalf("the stored model = %+v, %v", w, err)
	}
}

func TestRelaySendWithAnUnknownModelSendsNothing(t *testing.T) {
	dir := t.TempDir()
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "send", "new_in": dir, "model": "nope", "message": "job"})}},
		fakellm.Turn{When: asFiona, Content: "No such model."},
	)
	fake.SetModels("test-model", "other-model")
	r := newChatRig(t, fake, "")
	r.say("go")
	r.reply(sigGroup, "No such model.")
	res := toolResult(t, fake, "r1")
	if res["success"] != false || res["error"] != "unknown model nope; known: other-model, test-model" {
		t.Fatalf("send = %v", res)
	}
	r.quiet(2)
	if requestWith(t, fake, asWorker) >= 0 {
		t.Fatal("a worker ran")
	}
	sums, err := openStore(t, r.h.home).ListReadableIn(context.Background(), "common", "", "", []string{"interactive"}, 0)
	if err != nil || len(sums) != 0 {
		t.Fatalf("workers after a failed send = %v, %v", sums, err)
	}
}

const modelsRoutes = "[custom_providers.custom.models.\"deep-model\"]\ndescription = \"strongest reasoning, slow\"\ncontext_length = 128000\n"

// withoutServed drops the served-model line only, keeping any models note.
func withoutServed(s string) string {
	return strings.TrimSuffix(s, "\n\n[Model: your last reply came from test-model.]")
}

func TestAChatTurnCarriesTheModelsNoteWhenItChanges(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "r1"}, fakellm.Turn{Content: "r2"}, fakellm.Turn{Content: "r3"}, fakellm.Turn{Content: "r4"},
	)
	// Only what the provider lists as live is offered: other-model is configured
	// but not listed, so it is not in the note.
	fake.SetModels("test-model", "auto", "deep-model")
	fake.SetModelMeta("auto", map[string]any{"context_length": 1048576})
	r := newChatRig(t, fake, modelsRoutes)

	note := "[Models for relay send's model, up now: auto (1M context); deep-model (strongest reasoning, slow; 128k context); test-model; default is test-model.]"
	r.say("hello")
	r.reply(sigGroup, "r1")
	if got := lastUser(t, fake, 0); got != "hello\n\n"+note {
		t.Fatalf("first turn = %q", got)
	}
	r.say("again")
	r.reply(sigGroup, "r2")
	if got := withoutServed(lastUser(t, fake, 1)); got != "again" {
		t.Fatalf("an unchanged list is not noted again: %q", got)
	}
	fake.SetModels("test-model", "auto", "deep-model", "new-model")
	r.say("third")
	r.reply(sigGroup, "r3")
	note2 := "[Models for relay send's model, up now: auto (1M context); deep-model (strongest reasoning, slow; 128k context); new-model; test-model; default is test-model.]"
	if got := withoutServed(lastUser(t, fake, 2)); got != "third\n\n"+note2 {
		t.Fatalf("a changed list is noted: %q", got)
	}
	r.say("fourth")
	r.reply(sigGroup, "r4")
	if got := withoutServed(lastUser(t, fake, 3)); got != "fourth" {
		t.Fatalf("%q", got)
	}
	// Past messages are replayed as they were, and the system prompt never changes.
	var users []string
	for _, m := range wire(t, fake, 3) {
		if str(m, "role") == "user" {
			users = append(users, str(m, "content"))
		}
	}
	if len(users) != 4 || users[0] != "hello\n\n"+note {
		t.Fatalf("the transcript = %q", users)
	}
	sys := systemPrompt(t, fake, 0)
	for i := 1; i < 4; i++ {
		if systemPrompt(t, fake, i) != sys {
			t.Fatalf("the system prompt changed at request %d", i)
		}
	}
	if strings.Contains(sys, "[Models for") {
		t.Fatal("the system prompt carries the models")
	}
}

func TestWorkersAndTerminalsNeverGetTheModelsNote(t *testing.T) {
	dir := t.TempDir()
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "send", "new_in": dir, "message": "job"})}},
		fakellm.Turn{When: asFiona, Content: "Started."},
		fakellm.Turn{When: asWorker, Content: "done"},
		fakellm.Turn{When: asFiona, Content: "Reported."},
	)
	fake.SetModels("test-model", "auto")
	r := newChatRig(t, fake, modelsRoutes)
	r.say("go")
	r.reply(sigGroup, "Started.")
	r.reply(sigGroup, "Reported.")
	ws := workerRequests(t, fake)
	if len(ws) != 1 || lastUser(t, fake, ws[0]) != "job" || strings.Contains(string(fake.Bodies()[ws[0]]), "[Models for") {
		t.Fatalf("a worker's request carries the note: %v", ws)
	}
	if !strings.Contains(lastUser(t, fake, 0), "[Models for") {
		t.Fatal("the chat has it")
	}
}

func TestATerminalSessionNeverGetsTheModelsNote(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "a"})
	fake.SetModels("test-model", "auto")
	h := newHarness(t, fake, "")
	c := h.dial()
	s1 := c.create(t.TempDir())
	c.attach(s1, "driver")
	c.send(s1, "hello")
	c.turn()
	if got := lastUser(t, fake, 0); got != "hello" {
		t.Fatalf("an interactive turn = %q", got)
	}
}

func TestTheRelaySchemaOffersAModel(t *testing.T) {
	sch := tools.RelayTool().Schema().Function
	if !strings.HasSuffix(sch.Description, " A send can also choose the worker's model: pick from the latest [Models] note by what the task needs, and tell the user which you chose.") {
		t.Fatalf("the description ends: %q", sch.Description)
	}
	if !strings.Contains(string(sch.Parameters), `"model": {"type": "string", "description": "send: the model this worker runs on, from its next turn: one of the names in the latest [Models] note, or \"default\" for this instance's model. Leave it out to keep the worker's model."}`) {
		t.Fatalf("the schema: %s", sch.Parameters)
	}
}

// A chat offers workers only what the provider lists as live, less
// relay.hidden_models (exact names and "prefix/" entries); a send naming a
// model kept out is refused like an unknown one.
func TestWorkerModelsAreTheLiveOnesLessTheHidden(t *testing.T) {
	dir := t.TempDir()
	fake := fakellm.New(t,
		fakellm.Turn{Content: "r1"},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r2", map[string]any{"action": "send", "new_in": dir, "model": "edge/small", "message": "job"})}},
		fakellm.Turn{When: asFiona, Content: "Refused."},
	)
	fake.SetModels("auto", "fiona", "big/model", "down/model", "edge/small", "cloud-api")
	fake.SetModelMeta("down/model", map[string]any{"status": "unavailable"})
	fake.SetModelMeta("big/model", map[string]any{"status": "serving"})
	r := newChatRig(t, fake, "[relay]\nhidden_models = [\"fiona\", \"edge/\", \"cloud-api\"]\n")

	r.say("hello")
	r.reply(sigGroup, "r1")
	note := "[Models for relay send's model, up now: auto; big/model; default is test-model.]"
	if got := lastUser(t, fake, 0); got != "hello\n\n"+note {
		t.Fatalf("first turn = %q", got)
	}
	r.say("go")
	r.reply(sigGroup, "Refused.")
	res := toolResult(t, fake, "r2")
	if res["success"] != false || res["error"] != "unknown model edge/small; known: auto, big/model" {
		t.Fatalf("send = %v", res)
	}
	if requestWith(t, fake, asWorker) >= 0 {
		t.Fatal("a worker ran")
	}
}
