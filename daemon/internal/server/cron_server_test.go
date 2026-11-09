package server_test

// Phase 6 through the daemon: jobs from cron/jobs.json run, are recorded as
// the Python records them, and deliver to Signal (daemon/phases/06-cron.md).

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/cron"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
	"github.com/ewtodd/son-of-anton/daemon/internal/signal"
	"github.com/ewtodd/son-of-anton/daemon/internal/signalfake"
)

const cronHint = "[Scheduled run. No one is present and no one can answer questions. " +
	"Whatever you reply last is sent to the user as it stands, so make it a finished report " +
	"and do not try to send it yourself. " +
	"If there is nothing worth telling the user, reply with the single word [SILENT] " +
	"and nothing else; that sends nothing. [SILENT] must never be mixed with other text.]\n\n"

func cronJob(id, name, prompt string, extra map[string]any) map[string]any {
	j := map[string]any{
		"id": id, "name": name, "prompt": prompt, "model": nil, "provider": nil, "script": nil, "no_agent": false,
		"schedule":         map[string]any{"kind": "interval", "minutes": 10, "display": "every 10m"},
		"schedule_display": "every 10m", "repeat": map[string]any{"times": nil, "completed": 3},
		"enabled": true, "state": "scheduled", "paused_at": nil, "paused_reason": nil,
		"created_at": "2026-11-01T00:00:00+00:00", "next_run_at": time.Now().UTC().Add(-time.Minute).Format(time.RFC3339),
		"last_run_at": nil, "last_status": nil, "last_error": nil, "last_delivery_error": nil, "failure_streak": 0,
		"deliver": "local", "origin": nil, "workdir": nil, "monitor_state": map[string]any{"kept": "as is"},
	}
	for k, v := range extra {
		j[k] = v
	}
	return j
}

func writeJobs(t *testing.T, home string, jobs ...map[string]any) {
	t.Helper()
	dir := filepath.Join(home, "cron")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	b, _ := json.MarshalIndent(map[string]any{"jobs": jobs, "updated_at": "2026-11-01T00:00:00+00:00"}, "", "  ")
	if err := os.WriteFile(filepath.Join(dir, "jobs.json"), b, 0o600); err != nil {
		t.Fatal(err)
	}
}

func readJob(t *testing.T, home, id string) map[string]any {
	t.Helper()
	jobs, err := cron.Load(home)
	if err != nil {
		t.Fatal(err)
	}
	for _, j := range jobs {
		if j["id"] == id {
			return j
		}
	}
	t.Fatalf("no job %s", id)
	return nil
}

func waitJob(t *testing.T, home, id, what string, pred func(map[string]any) bool) map[string]any {
	t.Helper()
	var j map[string]any
	waitUntil(t, what, func() bool {
		jobs, err := cron.Load(home)
		if err != nil {
			return false
		}
		for _, x := range jobs {
			if x["id"] == id && pred(x) {
				j = x
				return true
			}
		}
		return false
	})
	return j
}

func cronOn(t *testing.T) string {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	return "[daemon]\ncron = true\ncron_tick = 1\n"
}

func TestACronJobRunsAndIsRecorded(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{When: "check the disk", Content: "the disk is fine"})
	h := newHarness(t, fake, cronOn(t))
	writeJobs(t, h.home, cronJob("abcabcabcabc", "disk check", "check the disk", nil))
	j := waitJob(t, h.home, "abcabcabcabc", "the run to be recorded", func(j map[string]any) bool { return j["last_status"] == "ok" })

	if got := lastUser(t, fake, 0); got != cronHint+"check the disk" {
		t.Fatalf("the cron prompt = %q", got)
	}
	if !strings.Contains(lastUser(t, fake, 0), "[SILENT]") {
		t.Fatal("the cron hint must name the [SILENT] marker")
	}
	for _, n := range toolNames(t, fake.Request(0)) {
		if n == "cronjob" || n == "clarify" || n == "task" {
			t.Fatalf("a cron run has %s", n)
		}
	}
	rep := j["repeat"].(map[string]any)
	next, err := time.Parse(time.RFC3339Nano, j["next_run_at"].(string))
	if rep["completed"] != float64(4) || j["last_error"] != nil || j["failure_streak"] != float64(0) || j["last_run_at"] == nil ||
		err != nil || time.Until(next) < 9*time.Minute || j["monitor_state"] == nil || j["state"] != "scheduled" {
		t.Fatalf("the record = %v", j)
	}
	outs, _ := filepath.Glob(filepath.Join(h.home, "cron", "output", "abcabcabcabc", "*.md"))
	if len(outs) != 1 || !regexp.MustCompile(`/\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d\.md$`).MatchString(outs[0]) {
		t.Fatalf("outputs = %v", outs)
	}
	doc, _ := os.ReadFile(outs[0])
	for _, want := range []string{"# Cron Job: disk check", "**Job ID:** abcabcabcabc", "## Prompt", "## Response", "the disk is fine"} {
		if !strings.Contains(string(doc), want) {
			t.Errorf("the output lacks %q:\n%s", want, doc)
		}
	}
}

func TestCronDeliversToTheOriginChat(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	fake := fakellm.New(t,
		fakellm.Turn{When: "the daily digest", Content: "**All** good"},
		fakellm.Turn{When: "the quiet one", Content: "[SILENT]"},
		fakellm.Turn{When: "the broken one", Status: 400, Body: `{"error":{"message":"no such model"}}`},
	)
	r := newChatRig(t, fake, "cron = true\ncron_tick = 1\n")
	origin := map[string]any{"platform": "signal", "chat_id": "group:" + sigGroup, "chat_name": "The Group", "thread_id": nil, "user_id": sigAccount}
	scripts := filepath.Join(r.h.home, "scripts")
	if err := os.MkdirAll(scripts, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(scripts, "health.sh"), []byte("#!/bin/sh\necho 'health ok'\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(scripts, "quiet.sh"), []byte("#!/bin/sh\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	o := map[string]any{"deliver": "origin", "origin": origin}
	writeJobs(t, r.h.home,
		cronJob("aaaaaaaaaaa1", "Digest", "the daily digest", o),
		cronJob("aaaaaaaaaaa2", "Health", "", map[string]any{"deliver": "origin", "origin": origin, "script": "health.sh", "no_agent": true}),
		cronJob("aaaaaaaaaaa3", "Quiet", "the quiet one", o),
		cronJob("aaaaaaaaaaa4", "Broken", "the broken one", o),
		cronJob("aaaaaaaaaaa5", "Nothing", "", map[string]any{"deliver": "origin", "origin": origin, "script": "quiet.sh", "no_agent": true}),
	)
	digest, _ := signal.Format(cron.Frame("Digest", "aaaaaaaaaaa1", "**All** good"))
	r.reply(sigGroup, digest)
	health, _ := signal.Format(cron.Frame("Health", "aaaaaaaaaaa2", "health ok"))
	r.reply(sigGroup, health)
	r.replyLike(sigGroup, "⚠️ The scheduled job 'Broken' failed: ")
	broken := waitJob(t, r.h.home, "aaaaaaaaaaa4", "the failure to be recorded", func(j map[string]any) bool { return j["last_status"] == "error" })
	if broken["failure_streak"] != float64(1) || broken["last_error"] == nil {
		t.Fatalf("a failed run = %v", broken)
	}
	for _, id := range []string{"aaaaaaaaaaa3", "aaaaaaaaaaa5"} {
		waitJob(t, r.h.home, id, id+" to run", func(j map[string]any) bool { return j["last_status"] == "ok" })
	}
	time.Sleep(300 * time.Millisecond)
	for _, m := range r.sig.Sends() {
		if strings.Contains(m.Text, "Quiet") || strings.Contains(m.Text, "Nothing") {
			t.Fatalf("a silent run delivered: %q", m.Text)
		}
	}
	// The echoes of our deliveries never start chat turns.
	for _, m := range r.sig.Sends() {
		r.sig.Push(signalfake.OwnerInGroup(sigAccount, sigGroup, m.Text, m.TS))
	}
	before := fake.Count()
	time.Sleep(400 * time.Millisecond)
	if fake.Count() != before {
		t.Fatal("an echoed delivery reached the model")
	}
}

// SPEC 12: a job that delivers to Signal has the chat's limits (its tools but
// clarify, relay and cronjob; files in the notes folder only; the notes folder
// as its directory whatever its workdir) and a prompt that says so in place of
// the coding guidance. A local job keeps every tool and the coding guidance.
func TestASignalJobHasTheChatsLimits(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	escape := filepath.Join(t.TempDir(), "escape.md")
	fake := fakellm.New(t,
		fakellm.Turn{When: "the signal job", ToolCalls: []fakellm.ToolCall{
			call("w1", "write_file", `{"path":"chores.md","content":"tally"}`),
			call("w2", "write_file", `{"path":"`+escape+`","content":"x"}`),
		}},
		fakellm.Turn{When: "the signal job", Content: "Chores logged."},
		fakellm.Turn{When: "the local job", Content: "local done"},
	)
	r := newChatRig(t, fake, "cron = true\ncron_tick = 1\n")
	origin := map[string]any{"platform": "signal", "chat_id": "group:" + sigGroup, "chat_name": "The Group", "thread_id": nil, "user_id": sigAccount}
	writeJobs(t, r.h.home,
		cronJob("bbbbbbbbbbb1", "Chores", "the signal job", map[string]any{"deliver": "origin", "origin": origin, "workdir": t.TempDir()}),
		cronJob("bbbbbbbbbbb2", "Local", "the local job", nil),
	)
	chores, _ := signal.Format(cron.Frame("Chores", "bbbbbbbbbbb1", "Chores logged."))
	r.reply(sigGroup, chores)
	waitJob(t, r.h.home, "bbbbbbbbbbb2", "the local job to run", func(j map[string]any) bool { return j["last_status"] == "ok" })

	request := func(marker string) map[string]any {
		for i := 0; i < fake.Count(); i++ {
			if b, _ := json.Marshal(fake.Request(i)); strings.Contains(string(b), marker) {
				return fake.Request(i)
			}
		}
		t.Fatalf("no request for %q", marker)
		return nil
	}
	system := func(req map[string]any) string {
		ms, _ := req["messages"].([]any)
		return str(ms[0].(map[string]any), "content")
	}
	notes := filepath.Join(r.h.home, "notes", "gateway")

	sig := request("the signal job")
	names := toolNames(t, sig)
	sort.Strings(names)
	if got := strings.Join(names, ","); got != "memory,patch,read_file,search_files,session_search,todo,web_extract,web_search,write_file" {
		t.Fatalf("a Signal job's tools = %s", got)
	}
	if sys := system(sig); !strings.HasPrefix(sys, prompt.SignalJobIdentity()+"\n\nYour notes folder: "+notes) || strings.Contains(sys, prompt.Guidance) {
		t.Fatalf("a Signal job's prompt:\n%.900s", sys)
	}
	if b, err := os.ReadFile(filepath.Join(notes, "chores.md")); err != nil || string(b) != "tally" {
		t.Fatalf("a relative path lands in the notes folder: %q, %v", b, err)
	}
	if _, err := os.Stat(escape); err == nil {
		t.Fatal("a Signal job wrote outside the notes folder")
	}
	if res := toolResult(t, fake, "w2"); !strings.Contains(str(res, "error"), "outside the notes folder") {
		t.Fatalf("the escape's result = %v", res)
	}

	local := request("the local job")
	if names := strings.Join(toolNames(t, local), ","); !strings.Contains(names, "terminal") {
		t.Fatalf("a local job keeps every tool: %s", names)
	}
	if sys := system(local); !strings.HasPrefix(sys, prompt.DefaultIdentity+"\n\n"+prompt.Guidance) {
		t.Fatalf("a local job's prompt:\n%.400s", sys)
	}
}

func TestCronIsOffByDefault(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	fake := fakellm.New(t, fakellm.Turn{Content: "ran"})
	h := newHarness(t, fake, "[daemon]\ncron_tick = 1\n")
	writeJobs(t, h.home, cronJob("abcabcabcabc", "disk check", "check the disk", nil))
	time.Sleep(2500 * time.Millisecond)
	if fake.Count() != 0 || readJob(t, h.home, "abcabcabcabc")["last_status"] != nil {
		t.Fatal("cron runs only with [daemon] cron = true")
	}
}

func TestTheTickLockIsThePythons(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "ran"})
	h := newHarness(t, fake, cronOn(t))
	if err := os.MkdirAll(filepath.Join(h.home, "cron"), 0o700); err != nil {
		t.Fatal(err)
	}
	lock, err := os.OpenFile(filepath.Join(h.home, "cron", ".tick.lock"), os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		t.Fatal(err)
	}
	writeJobs(t, h.home, cronJob("abcabcabcabc", "disk check", "check the disk", nil))
	time.Sleep(2500 * time.Millisecond)
	if fake.Count() != 0 {
		t.Fatal("a tick ran while another scheduler held the tick lock")
	}
	_ = syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	waitJob(t, h.home, "abcabcabcabc", "the run once the lock is free", func(j map[string]any) bool { return j["last_status"] == "ok" })
}

func TestTheCronjobTool(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("j1", "cronjob", `{"action":"create","schedule":"every 30m","prompt":"check the queue","name":"queue check"}`)}},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("j2", "cronjob", `{"action":"list"}`), call("j3", "cronjob", `{"action":"pause","job_id":"queue check"}`)}},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("j4", "cronjob", `{"action":"remove","job_id":"queue check"}`), call("j5", "cronjob", `{"action":"remove","job_id":"nope"}`)}},
		fakellm.Turn{Content: "done"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "schedule it")
	rows := toolRows(c.turn())
	if len(rows) != 5 {
		t.Fatalf("rows = %v", rows)
	}
	created := decodeRow(t, rows[0])
	jobID, _ := created["job_id"].(string)
	if created["success"] != true || !regexp.MustCompile(`^[0-9a-f]{12}$`).MatchString(jobID) {
		t.Fatalf("create = %v", created)
	}
	if list := decodeRow(t, rows[1]); list["success"] != true || list["count"] != float64(1) {
		t.Fatalf("list = %v", list)
	}
	if paused := decodeRow(t, rows[2]); paused["success"] != true {
		t.Fatalf("pause by name = %v", paused)
	}
	if removed := decodeRow(t, rows[3]); removed["success"] != true {
		t.Fatalf("remove = %v", removed)
	}
	if missing := decodeRow(t, rows[4]); missing["success"] != false ||
		!strings.Contains(str(missing, "error"), "No job has the id or name 'nope'") {
		t.Fatalf("an unknown job = %v", missing)
	}
	if jobs, _ := cron.Load(h.home); len(jobs) != 0 {
		t.Fatalf("jobs after remove = %v", jobs)
	}

	// The record create wrote, before the remove, is the Python's.
	fake2 := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("j1", "cronjob", `{"action":"create","schedule":"every 30m","prompt":"check the queue","name":"queue check"}`)}},
		fakellm.Turn{Content: "done"},
	)
	h2 := newHarness(t, fake2, "")
	c2 := h2.dial()
	id2 := c2.create(t.TempDir())
	c2.attach(id2, "driver")
	c2.send(id2, "schedule it")
	c2.turn()
	jobs, err := cron.Load(h2.home)
	if err != nil || len(jobs) != 1 {
		t.Fatalf("jobs = %v, %v", jobs, err)
	}
	j := jobs[0]
	sched, _ := j["schedule"].(map[string]any)
	rep, _ := j["repeat"].(map[string]any)
	next, err := time.Parse(time.RFC3339Nano, str(j, "next_run_at"))
	if j["name"] != "queue check" || j["prompt"] != "check the queue" || sched["kind"] != "interval" || sched["minutes"] != float64(30) ||
		sched["display"] != "every 30m" || j["deliver"] != "local" || j["state"] != "scheduled" || j["enabled"] != true ||
		rep["completed"] != float64(0) || rep["times"] != nil || err != nil || time.Until(next) < 29*time.Minute || j["created_at"] == nil {
		t.Fatalf("the created record = %v", j)
	}
}
