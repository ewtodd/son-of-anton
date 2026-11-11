package tools_test

// The reminder tool: a chat's reminders are cron jobs of their own kind, set,
// listed, changed and cancelled here, and invisible to the cronjob tool.

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/cron"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

func call(t *testing.T, tool tools.Tool, env *tools.Env, args string) map[string]any {
	t.Helper()
	out, err := tool.Run(context.Background(), env, json.RawMessage(args))
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(out), &m); err != nil {
		t.Fatalf("%s: %v", out, err)
	}
	return m
}

func chatEnv(home, chat string) *tools.Env {
	return &tools.Env{Cron: &tools.Cron{Home: home, Chat: chat}}
}

func TestARemindersLife(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	home := t.TempDir()
	r, env := tools.Reminder(), chatEnv(home, "signal:group:gW:+15550000000")

	set := call(t, r, env, `{"action":"set","text":"Text Uncle Mike.","when":"1h"}`)
	rem, _ := set["reminder"].(map[string]any)
	id, _ := rem["reminder_id"].(string)
	if set["success"] != true || id == "" || rem["text"] != "Text Uncle Mike." || rem["repeats"] != false {
		t.Fatalf("set = %v", set)
	}
	jobs, _ := cron.Load(home)
	if len(jobs) != 1 || !tools.IsReminder(jobs[0]) || jobs[0]["message"] != "Text Uncle Mike." || jobs[0]["prompt"] != "" ||
		jobs[0]["deliver"] != "origin" {
		t.Fatalf("stored %v; a reminder of this chat, with its text and no prompt", jobs)
	}
	next, _ := time.Parse(time.RFC3339, jobs[0]["next_run_at"].(string))
	if d := time.Until(next); d < 59*time.Minute || d > 61*time.Minute {
		t.Fatalf("next run %v from now; '1h' is an hour", d)
	}

	if list := call(t, r, env, `{"action":"list"}`); list["count"] != float64(1) {
		t.Fatalf("list = %v", list)
	}
	other := chatEnv(home, "signal:dm:+15551111111")
	if list := call(t, r, other, `{"action":"list"}`); list["count"] != float64(0) {
		t.Fatalf("another chat's list = %v; a chat sees only its own reminders", list)
	}
	if res := call(t, r, other, `{"action":"cancel","reminder_id":"`+id+`"}`); res["success"] != false {
		t.Fatalf("another chat cancelled it: %v", res)
	}

	changed := call(t, r, env, `{"action":"change","reminder_id":"`+id+`","text":"Text Uncle Mike and Daniel.","when":"every 2h"}`)
	if c, _ := changed["reminder"].(map[string]any); changed["success"] != true || c["text"] != "Text Uncle Mike and Daniel." || c["repeats"] != true {
		t.Fatalf("change = %v", changed)
	}
	if res := call(t, r, env, `{"action":"cancel","reminder_id":"`+id+`"}`); res["success"] != true {
		t.Fatalf("cancel = %v", res)
	}
	if jobs, _ := cron.Load(home); len(jobs) != 0 {
		t.Fatalf("jobs after cancel = %v", jobs)
	}
}

func TestRemindersRefuseWhatTheyCannotDo(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	home := t.TempDir()
	r, env := tools.Reminder(), chatEnv(home, "signal:dm:+15551111111")
	for args, want := range map[string]string{
		`{"action":"set","when":"1h"}`:                             "needs the text",
		`{"action":"set","text":"x"}`:                              "needs when",
		`{"action":"set","text":"x","when":"2020-01-01T09:00:00"}`: "already passed",
		`{"action":"set","text":"x","when":"soonish"}`:             "",
		`{"action":"change","reminder_id":"nope","text":"x"}`:      "No reminder of this chat",
		`{"action":"snooze"}`:                                      "not a reminder action",
		`{"action":"cancel"}`:                                      "needs a reminder_id",
	} {
		res := call(t, r, env, args)
		if res["success"] != false || !strings.Contains(str(res, "error"), want) {
			t.Errorf("%s = %v; want an error containing %q", args, res, want)
		}
	}
	if res := call(t, r, chatEnv(home, ""), `{"action":"set","text":"x","when":"1h"}`); res["success"] != false ||
		!strings.Contains(str(res, "error"), "not a chat") {
		t.Fatalf("a terminal session set a reminder: %v", res)
	}
}

func TestTheCronjobToolLeavesRemindersAlone(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	home := t.TempDir()
	env := chatEnv(home, "signal:dm:+15551111111")
	call(t, tools.Reminder(), env, `{"action":"set","text":"Water the plants.","when":"1h"}`)
	cj := tools.CronJob(home)
	if list := call(t, cj, env, `{"action":"list"}`); list["count"] != float64(0) {
		t.Fatalf("cronjob list = %v; reminders are the reminder tool's", list)
	}
	for _, action := range []string{"remove", "update", "pause", "run"} {
		res := call(t, cj, env, `{"action":"`+action+`","job_id":"Reminder: Water the plants.","prompt":"x"}`)
		if res["success"] != false || !strings.Contains(str(res, "error"), "with the reminder tool") {
			t.Errorf("cronjob %s on a reminder = %v", action, res)
		}
	}
	if jobs, _ := cron.Load(home); len(jobs) != 1 {
		t.Fatalf("jobs = %v; the reminder is still there", jobs)
	}
}
