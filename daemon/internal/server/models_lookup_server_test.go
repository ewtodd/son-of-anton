package server_test

// model.list reads the catalog once per call even when that lookup fails, and
// a chat's outage warning is not cleared by a failed status lookup.
import (
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

func TestModelListUsesOneCatalogLookupWhenItFails(t *testing.T) {
	fake := fakellm.New(t)
	fake.SetModelsStatus(500)
	h := newHarness(t, fake, "")
	c := h.dial()
	asked := fake.ModelRequests()

	res := c.ok("model.list", map[string]any{})
	if boolField(res, "statuses") {
		t.Fatalf("statuses = true after a failed catalog: %v", res)
	}
	rows := objs(res, "models")
	if len(rows) != 2 {
		t.Fatalf("the configured routes survive a failed catalog: %v", rows)
	}
	if n := fake.ModelRequests(); n != asked+1 {
		t.Fatalf("/models requests = %d, want one more than %d", n, asked)
	}
}

func TestAChatDoesNotRepeatTheDownWarningAfterAFailedLookup(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "one"},
		fakellm.Turn{Content: "two"},
		fakellm.Turn{Content: "three"},
	)
	modelStatuses(fake, []string{"other-model"}, []string{"test-model"})
	r := newChatRig(t, fake, "")
	r.say("hi")
	r.replyLike(sigGroup, "test-model is not being served; pick another model")
	r.reply(sigGroup, "one")

	// Let the status cache expire, then make the catalog lookup fail. A failed
	// lookup must not be read as "serving" and clear the outage warning.
	time.Sleep(10*time.Second + 200*time.Millisecond)
	fake.SetModelsStatus(500)
	r.say("again")
	r.reply(sigGroup, "two")
	fake.SetModelsStatus(0)
	r.say("third")
	r.reply(sigGroup, "three")

	warnings := 0
	for _, m := range r.sig.Sends() {
		if m.Text == "test-model is not being served; pick another model" {
			warnings++
		}
	}
	if warnings != 1 {
		t.Fatalf("chat warnings = %d; one per outage, even through a failed lookup", warnings)
	}
	if fake.Count() != 3 {
		t.Fatalf("model requests = %d", fake.Count())
	}
}

// A slow /models must not stall the attach handshake: the model-status lookup
// runs on a short-lived goroutine, so the attach reply goes out at once and
// the warning arrives after it.
func TestAttachDoesNotStallOnASlowModelLookup(t *testing.T) {
	fake := fakellm.New(t)
	modelStatuses(fake, []string{"other-model"}, []string{"test-model"})
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())

	// First attach: load warms the catalog (no delay yet), and the down
	// model's notice arrives.
	if r := c.attach(id, "viewer"); str(r, "role") != "viewer" {
		t.Fatalf("attach = %v", r)
	}
	c.await("notice")

	// Let the status cache expire, then slow /models down: the lookup the
	// attaching client triggers must not hold the connection's read loop, so
	// the next request on it does not wait for the catalog.
	time.Sleep(10*time.Second + 200*time.Millisecond)
	fake.SetModelsDelay(time.Second)
	c2 := h.dial()
	if r := c2.attach(id, "viewer"); str(r, "role") != "viewer" {
		t.Fatalf("attach = %v", r)
	}
	start := time.Now()
	c2.ok("session.list", map[string]any{})
	if elapsed := time.Since(start); elapsed > 700*time.Millisecond {
		t.Fatalf("the request after attach took %v; the model-status lookup must not hold the connection", elapsed)
	}
	if n := c2.await("notice"); str(n, "text") != "test-model is not being served; pick another model" {
		t.Fatalf("the warning still arrives, after the reply: %v", n)
	}
	c.none("notice", 200*time.Millisecond)
}

// A failed /models fetch is remembered briefly: a second catalog lookup within
// the window makes no request at all, and the window expires.
func TestACatalogFailureIsRememberedBriefly(t *testing.T) {
	fake := fakellm.New(t)
	fake.SetModelsStatus(500)
	h := newHarness(t, fake, "")
	c := h.dial()

	before := fake.ModelRequests()
	c.ok("model.list", map[string]any{})
	c.ok("model.list", map[string]any{})
	if got := fake.ModelRequests(); got != before+1 {
		t.Fatalf("/models requests = %d, want %d: a failed catalog is negatively cached", got, before+1)
	}
	time.Sleep(5*time.Second + 200*time.Millisecond)
	c.ok("model.list", map[string]any{})
	if got := fake.ModelRequests(); got != before+2 {
		t.Fatalf("/models requests = %d, want %d: the negative cache expires", got, before+2)
	}
}

// A model that recovers (known again, no longer "unavailable") resets the
// outage latch, so a later outage warns again.
func TestARecoveredModelWarnsAgainWhenItFallsBackDown(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "one"},
		fakellm.Turn{Content: "two"},
		fakellm.Turn{Content: "three"},
	)
	modelStatuses(fake, []string{"other-model"}, []string{"test-model"})
	r := newChatRig(t, fake, "")
	r.say("hi") // test-model is unavailable: warns.
	r.replyLike(sigGroup, "test-model is not being served; pick another model")
	r.reply(sigGroup, "one")

	// test-model comes back as serving: the latch clears, no warning.
	time.Sleep(10*time.Second + 200*time.Millisecond)
	modelStatuses(fake, []string{"test-model", "other-model"}, nil)
	r.say("again")
	r.reply(sigGroup, "two")

	// It falls down again: the cleared latch warns.
	time.Sleep(10*time.Second + 200*time.Millisecond)
	modelStatuses(fake, []string{"other-model"}, []string{"test-model"})
	r.say("third")
	r.reply(sigGroup, "three")

	warnings := 0
	for _, m := range r.sig.Sends() {
		if m.Text == "test-model is not being served; pick another model" {
			warnings++
		}
	}
	if warnings != 2 {
		t.Fatalf("chat warnings = %d; one per outage, and a recovery clears the latch", warnings)
	}
}

func TestAttachingToADownModelWarnsOnlyTheAttachingClient(t *testing.T) {
	fake := fakellm.New(t)
	modelStatuses(fake, []string{"other-model"}, []string{"test-model"})
	h := newHarness(t, fake, "")
	c1 := h.dial()
	id := c1.create(t.TempDir())
	c1.attach(id, "viewer")
	if n := c1.await("notice"); str(n, "text") != "test-model is not being served; pick another model" {
		t.Fatalf("viewer attach notice = %v", n)
	}

	c2 := h.dial()
	c2.attach(id, "driver")
	if n := c2.await("notice"); str(n, "text") != "test-model is not being served; pick another model" {
		t.Fatalf("driver attach notice = %v", n)
	}
	c1.none("notice", 200*time.Millisecond)
}
