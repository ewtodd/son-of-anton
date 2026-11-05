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
