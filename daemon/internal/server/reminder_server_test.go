package server_test

// A reminder (the reminder tool's cron job) is sent to its chat as it is,
// "⏰ Reminder: " and its text, with no model run and no job frame.

import (
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

func TestAReminderIsSentAsItIsWithNoModel(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	fake := fakellm.New(t)
	r := newChatRig(t, fake, "cron = true\ncron_tick = 1\n")
	origin := map[string]any{"platform": "signal", "chat_id": "group:" + sigGroup, "chat_name": nil, "thread_id": nil, "user_id": sigAccount}
	writeJobs(t, r.h.home, cronJob("bbbbbbbbbbb1", "Reminder: Text Uncle Mike and Daniel.", "", map[string]any{
		"kind": tools.ReminderKind, "message": "Text Uncle Mike and Daniel.", "deliver": "origin", "origin": origin,
		"schedule": map[string]any{"kind": "once", "run_at": time.Now().UTC().Add(-30 * time.Second).Format(time.RFC3339)},
		"repeat":   map[string]any{"times": 1, "completed": 0},
	}))
	r.reply(sigGroup, tools.ReminderPrefix+"Text Uncle Mike and Daniel.")
	done := waitJob(t, r.h.home, "bbbbbbbbbbb1", "the reminder to be recorded", func(j map[string]any) bool { return j["last_status"] == "ok" })
	if done["state"] != "completed" || done["enabled"] != false {
		t.Fatalf("a one-time reminder after it ran = %v", done)
	}
	if fake.Count() != 0 {
		t.Fatalf("%d model requests; a reminder runs no model", fake.Count())
	}
	for _, m := range r.sig.Sends() {
		if strings.Contains(m.Text, "Scheduled job") {
			t.Fatalf("a reminder came framed as a job: %q", m.Text)
		}
	}
}
