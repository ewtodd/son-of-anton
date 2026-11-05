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

// The provider is down when the first session loads: the failed fetch must
// not be stored as a status-less listing. Once the failure's window expires
// and the provider is back, statuses and the down warning work — without a
// restart.
func TestAFailedMetaFetchDoesNotBlindTheCatalog(t *testing.T) {
	fake := fakellm.New(t)
	fake.SetModelsStatus(503)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "viewer") // load: fetchMeta fails

	// During the outage: no statuses, and the failure is remembered — no
	// more /models requests.
	before := fake.ModelRequests()
	rows := modelRows(t, c, id)
	for _, row := range rows {
		if str(row, "status") != "" {
			t.Fatalf("a failed fetch stores no statuses: %v", row)
		}
	}
	if got := fake.ModelRequests(); got != before {
		t.Fatalf("/models requests = %d, want %d: the failure is remembered", got, before)
	}

	// The provider recovers; once the window expires, the catalog refetches.
	fake.SetModelsStatus(0)
	modelStatuses(fake, []string{"other-model"}, []string{"test-model"})
	time.Sleep(5*time.Second + 200*time.Millisecond)
	rows = modelRows(t, c, id)
	if str(rowBy(t, rows, "test-model"), "status") != "unavailable" {
		t.Fatalf("after the recovery, the statuses are back: %v", rows)
	}
	c2 := h.dial()
	c2.attach(id, "viewer")
	if n := c2.await("notice"); str(n, "text") != "test-model is not being served; pick another model" {
		t.Fatalf("the down warning works after the recovery: %v", n)
	}
}

// A hung /models must not make every chat turn wait: the first turn pays the
// timeout, and the failure is remembered for the next.
func TestAChatTurnDoesNotWaitOnAHungModelsListing(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "one"},
		fakellm.Turn{Content: "two"},
	)
	fake.SetModels("test-model", "other-model")
	fake.SetModelsDelay(6 * time.Second) // past workerModels' 5 s timeout
	r := newChatRig(t, fake, "")

	r.say("hi")
	r.reply(sigGroup, "one")

	start := time.Now()
	r.say("again")
	r.reply(sigGroup, "two")
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Fatalf("the second turn waited %v; the failed listing is remembered", elapsed)
	}
}

// A model that recovers — reported serving again, or a listing that reports
// no statuses — resets the outage latch, so a later outage warns again.
func TestARecoveredModelWarnsAgainWhenItFallsBackDown(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "one"},
		fakellm.Turn{Content: "two"},
		fakellm.Turn{Content: "three"},
		fakellm.Turn{Content: "four"},
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

	// A listing with no statuses clears the latch too, without warning.
	time.Sleep(10*time.Second + 200*time.Millisecond)
	fake.SetModels("test-model", "other-model")
	r.say("fourth")
	r.reply(sigGroup, "four")

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

// An outage that changes state — the model reported "loading" — is the same
// outage: the latch stays, and the warning does not repeat.
func TestALoadingStatusKeepsTheOutageLatch(t *testing.T) {
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

	// It goes "loading": the outage is ongoing, the latch stays.
	time.Sleep(10*time.Second + 200*time.Millisecond)
	fake.SetModelMeta("test-model", map[string]any{"status": "loading"})
	r.say("again")
	r.reply(sigGroup, "two")

	// Unavailable again, in the same outage: no second warning.
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
	if warnings != 1 {
		t.Fatalf("chat warnings = %d; a loading model is the same outage", warnings)
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
