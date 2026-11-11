package tools_test

import (
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/cron"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

func TestAChatCannotRewriteAJobSetUpOutsideOne(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	home := t.TempDir()
	cj := tools.CronJob(home)
	terminal, chat := chatEnv(home, ""), chatEnv(home, "signal:dm:+15551111111")
	if res := call(t, cj, terminal, `{"action":"create","name":"nightly","schedule":"every 1d","prompt":"back up the notes"}`); res["success"] != true {
		t.Fatalf("terminal create = %v", res)
	}
	if res := call(t, cj, chat, `{"action":"create","name":"chatty","schedule":"every 1d","prompt":"say hi"}`); res["success"] != true {
		t.Fatalf("chat create = %v", res)
	}

	for _, args := range []string{
		`{"action":"update","job_id":"nightly","prompt":"something else"}`,
		`{"action":"update","job_id":"nightly","deliver":"local"}`,
		`{"action":"run","job_id":"nightly","prompt":"and also this"}`,
		`{"action":"trigger","job_id":"nightly","prompt":"and also this"}`,
	} {
		if res := call(t, cj, chat, args); res["success"] != false || !strings.Contains(str(res, "error"), "set up outside a chat") {
			t.Errorf("from a chat, %s = %v", args, res)
		}
	}
	for _, args := range []string{
		`{"action":"pause","job_id":"nightly"}`,
		`{"action":"resume","job_id":"nightly"}`,
		`{"action":"run","job_id":"nightly"}`,
		`{"action":"update","job_id":"chatty","prompt":"say hello"}`,
	} {
		if res := call(t, cj, chat, args); res["success"] != true {
			t.Errorf("from a chat, %s = %v", args, res)
		}
	}
	if res := call(t, cj, terminal, `{"action":"update","job_id":"nightly","prompt":"back up everything"}`); res["success"] != true {
		t.Errorf("the terminal's update = %v", res)
	}
	jobs, _ := cron.Load(home)
	for _, j := range jobs {
		if j["name"] == "nightly" && (j["prompt"] != "back up everything" || j["deliver"] != "local") {
			t.Errorf("nightly = %v", j)
		}
	}
	if res := call(t, cj, chat, `{"action":"remove","job_id":"nightly"}`); res["success"] != true {
		t.Errorf("a chat's remove = %v", res)
	}
}
