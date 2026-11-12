package tools_test

import (
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/cron"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

func TestListLeavesOutJobsThatHaveRunForTheLastTime(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	home := t.TempDir()
	cj, env := tools.CronJob(home), chatEnv(home, "signal:dm:+15551111111")
	for _, args := range []string{
		`{"action":"create","name":"hourly","schedule":"every 1h","prompt":"check the queue"}`,
		`{"action":"create","name":"once","schedule":"30m","prompt":"check the build"}`,
		`{"action":"create","name":"held","schedule":"every 1d","prompt":"tidy the notes"}`,
		`{"action":"pause","job_id":"held"}`,
	} {
		if res := call(t, cj, env, args); res["success"] != true {
			t.Fatalf("%s = %v", args, res)
		}
	}
	err := cron.Update(home, func(jobs []cron.Job) ([]cron.Job, error) {
		for _, j := range jobs {
			if j["name"] == "once" {
				j["enabled"], j["state"], j["next_run_at"] = false, "completed", nil
			}
		}
		return jobs, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	names := func(res map[string]any) []string {
		var out []string
		jobs, _ := res["jobs"].([]any)
		for _, j := range jobs {
			out = append(out, str(j.(map[string]any), "name"))
		}
		return out
	}

	list := call(t, cj, env, `{"action":"list"}`)
	if got := names(list); len(got) != 2 || got[0] != "hourly" || got[1] != "held" || list["completed_left_out"] != float64(1) {
		t.Fatalf("list = %v; the paused job stays, the completed one is left out and counted", list)
	}
	all := call(t, cj, env, `{"action":"list","include_completed":true}`)
	if got := names(all); len(got) != 3 || all["completed_left_out"] != nil {
		t.Fatalf("list with include_completed = %v", all)
	}
}

// An update replaces the whole prompt, so list shows all of it: from a
// 100-character preview ending in "...", a chat model rewrote a daily job's
// prompt as the preview plus one sentence.
func TestListShowsEachJobsWholePrompt(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	home := t.TempDir()
	cj, env := tools.CronJob(home), chatEnv(home, "signal:dm:+15551111111")
	long := strings.Repeat("Ask how the day went, kindly. ", 12) + "Then stop."
	if res := call(t, cj, env, `{"action":"create","name":"long","schedule":"every 1d","prompt":"`+long+`"}`); res["success"] != true {
		t.Fatalf("create = %v", res)
	}
	list := call(t, cj, env, `{"action":"list"}`)
	jobs, _ := list["jobs"].([]any)
	if len(jobs) != 1 || str(jobs[0].(map[string]any), "prompt") != long {
		t.Fatalf("list = %v", list)
	}
}
