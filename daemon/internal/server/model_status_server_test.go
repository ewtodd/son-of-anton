package server_test

// The /model picker lists what a gateway is serving, not everything it fronts
// (phase 10a / the old Python contract). maitred's /models reports each model
// as "serving" or "unavailable"; statuses are trusted for 10 seconds, so a
// mode switch shows on the next open. The catalog stays complete (naming an
// unloaded model is still valid), and an endpoint with no statuses filters
// nothing and costs no extra request.

import (
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

// A fresh /models fetch normalizes statuses the same way a cache hit does:
// maitred's "Unavailable" and "Serving " read as "unavailable" and "serving".
func TestAFreshFetchNormalizesMaitredStatuses(t *testing.T) {
	fake := fakellm.New(t)
	fake.SetModels("test-model", "other-model")
	fake.SetModelMeta("test-model", map[string]any{"status": "Unavailable"})
	fake.SetModelMeta("other-model", map[string]any{"status": "Serving "})
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())

	rows := modelRows(t, c, id)
	down := rowBy(t, rows, "test-model")
	if str(down, "status") != "unavailable" || boolField(down, "serving") {
		t.Fatalf("a fresh fetch normalizes \"Unavailable\": %v", down)
	}
	up := rowBy(t, rows, "other-model")
	if str(up, "status") != "serving" || !boolField(up, "serving") {
		t.Fatalf("a fresh fetch normalizes \"Serving \": %v", up)
	}
	// A cache hit within the TTL answers the same.
	rows = modelRows(t, c, id)
	if str(rowBy(t, rows, "test-model"), "status") != "unavailable" || str(rowBy(t, rows, "other-model"), "status") != "serving" {
		t.Fatalf("the cache hit disagrees with the fresh fetch: %v", rows)
	}
}

func modelStatuses(fake *fakellm.Server, serving, unavailable []string) {
	ids := append(append([]string(nil), serving...), unavailable...)
	fake.SetModels(ids...)
	for _, id := range serving {
		fake.SetModelMeta(id, map[string]any{"status": "serving"})
	}
	for _, id := range unavailable {
		fake.SetModelMeta(id, map[string]any{"status": "unavailable"})
	}
}

// modelRows returns model.list's rows.
func modelRows(t *testing.T, c *client, id string) []map[string]any {
	t.Helper()
	return objs(c.ok("model.list", map[string]any{"session": id}), "models")
}

func rowBy(t *testing.T, rows []map[string]any, name string) map[string]any {
	t.Helper()
	for _, r := range rows {
		if str(r, "name") == name {
			return r
		}
	}
	t.Fatalf("no model row %q in %v", name, rows)
	return nil
}

func TestModelListFlagsTheServedAndUnservedAndKeepsTheCatalog(t *testing.T) {
	fake := fakellm.New(t)
	modelStatuses(fake, []string{"test-model", "other-model"}, []string{"down-model"})
	h := newHarness(t, fake, "[custom_providers.custom.models.\"down-model\"]\nreasoning_efforts = [\"low\", \"high\"]\n")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")

	res := c.ok("model.list", map[string]any{"session": id})
	if !boolField(res, "statuses") {
		t.Fatalf("model.list does not report statuses: %v", res)
	}
	rows := modelRows(t, c, id)
	served := rowBy(t, rows, "test-model")
	if str(served, "status") != "serving" || !boolField(served, "serving") {
		t.Fatalf("test-model row = %v", served)
	}
	down := rowBy(t, rows, "down-model")
	if str(down, "status") != "unavailable" || boolField(down, "serving") {
		t.Fatalf("down-model row = %v", down)
	}
	// A down model keeps its reasoning levels: /reasoning can still open on it.
	if efforts := strs(down, "reasoning_efforts"); len(efforts) != 2 || efforts[0] != "low" || efforts[1] != "high" {
		t.Fatalf("down-model efforts = %v", efforts)
	}
}

func TestAModeSwitchShowsOnTheNextOpen(t *testing.T) {
	fake := fakellm.New(t)
	modelStatuses(fake, []string{"test-model", "other-model"}, nil)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")

	first := rowBy(t, modelRows(t, c, id), "test-model")
	if str(first, "status") != "serving" {
		t.Fatalf("first status = %v", first)
	}

	// The gateway swaps the model out. Statuses are asked again once the 10s
	// window has passed, so the next open sees it.
	fake.SetModelMeta("test-model", map[string]any{"status": "unavailable"})
	time.Sleep(10*time.Second + 200*time.Millisecond)
	second := rowBy(t, modelRows(t, c, id), "test-model")
	if str(second, "status") != "unavailable" || boolField(second, "serving") {
		t.Fatalf("second status = %v", second)
	}
}

func TestNamingAnUnloadedModelStaysValid(t *testing.T) {
	fake := fakellm.New(t)
	modelStatuses(fake, []string{"test-model", "other-model"}, []string{"down-model"})
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.ok("session.set", map[string]any{"session": id, "model": "down-model"})
	sess := obj(c.attach(id, "driver"), "session")
	if str(sess, "model") != "down-model" {
		t.Fatalf("session model = %v", sess["model"])
	}
	// /model <name> names the model even though the picker omits it.
	rows := modelRows(t, c, id)
	if rowBy(t, rows, "down-model")["serving"] != false {
		t.Fatalf("down-model should be marked unserved")
	}
}

func TestAnEndpointWithoutStatusesFiltersNothingAndCostsNoExtraRequest(t *testing.T) {
	fake := fakellm.New(t)
	fake.SetModels("llama-3", "qwen-coder")
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	asked := fake.ModelRequests()

	res := c.ok("model.list", map[string]any{"session": id})
	if boolField(res, "statuses") {
		t.Fatalf("statuses = true for an endpoint that reports none: %v", res)
	}
	for _, r := range modelRows(t, c, id) {
		if !boolField(r, "serving") {
			t.Fatalf("status-less model is filtered: %v", r)
		}
	}
	// A second open inside the status window makes no request.
	c.ok("model.list", map[string]any{"session": id})
	if n := fake.ModelRequests(); n != asked {
		t.Fatalf("extra /models requests = %d, want %d", n, asked)
	}
}

func TestAttachingToADownModelWarnsAndAServedOneIsQuiet(t *testing.T) {
	fake := fakellm.New(t)
	modelStatuses(fake, []string{"other-model"}, []string{"test-model"})
	h := newHarness(t, fake, "")
	c := h.dial()

	down := c.create(t.TempDir())
	c.attach(down, "driver")
	notice := c.await("notice")
	if str(notice, "level") != "warning" || str(notice, "text") != "test-model is not being served; pick another model" {
		t.Fatalf("attach notice = %v", notice)
	}

	servedFake := fakellm.New(t)
	modelStatuses(servedFake, []string{"test-model"}, nil)
	h2 := newHarness(t, servedFake, "")
	c2 := h2.dial()
	served := c2.create(t.TempDir())
	c2.attach(served, "driver")
	c2.none("notice", 200*time.Millisecond)
}

func TestAChatWarnsOncePerOutage(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "one"},
		fakellm.Turn{Content: "two"},
	)
	modelStatuses(fake, []string{"other-model"}, []string{"test-model"})
	r := newChatRig(t, fake, "")
	r.say("hi")
	r.replyLike(sigGroup, "test-model is not being served; pick another model")
	r.reply(sigGroup, "one")
	r.say("again")
	r.reply(sigGroup, "two")

	warnings := 0
	for _, m := range r.sig.Sends() {
		if m.Text == "test-model is not being served; pick another model" {
			warnings++
		}
	}
	if warnings != 1 {
		t.Fatalf("chat warnings = %d; one per outage", warnings)
	}
	if fake.Count() != 2 {
		t.Fatalf("model requests = %d", fake.Count())
	}
}

// Her model being the chat model (different from model.default) means a chat
// falls back to the instance's model on its own: it is told so, not to pick.
func TestAChatWithAFallbackIsToldItIsBeingAskedFor(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "one"},
		fakellm.Turn{Content: "two"},
	)
	modelStatuses(fake, []string{"test-model", "other-model"}, []string{"fiona-model"})
	r := newChatRig(t, fake, "[gateway]\nmodel = \"fiona-model\"\n\n[custom_providers.custom.models.\"fiona-model\"]\n")
	r.say("hi")
	r.replyLike(sigGroup, "fiona-model is not being served; asking son-of-anton")
	r.reply(sigGroup, "one")
	r.say("again")
	r.reply(sigGroup, "two")

	warnings := 0
	for _, m := range r.sig.Sends() {
		if m.Text == "fiona-model is not being served; asking son-of-anton" {
			warnings++
		}
	}
	if warnings != 1 {
		t.Fatalf("chat warnings = %d; one per outage", warnings)
	}
}

// A 503 model_not_loaded does not change the session's model: the fallback
// request may go elsewhere, but the session keeps its pin.
func TestA503DoesNotSwitchTheSessionModel(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{When: `"model":"other-model"`, Status: 503, Body: `{"error":{"message":"model is loading"}}`},
		fakellm.Turn{Content: "the default answered"},
	)
	modelStatuses(fake, []string{"test-model"}, []string{"other-model"})
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.ok("session.set", map[string]any{"session": id, "model": "other-model"})
	c.send(id, "hi")
	c.turn()
	sess := obj(c.attach(id, "driver"), "session")
	if str(sess, "model") != "other-model" {
		t.Fatalf("after a 503 the session model = %v", sess["model"])
	}
}

func boolField(m map[string]any, k string) bool {
	v, _ := m[k].(bool)
	return v
}
