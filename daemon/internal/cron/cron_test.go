package cron_test

// Phase 6: the job file, schedules, due logic, silence and framing
// (daemon/phases/06-cron.md), as the Python's cron/jobs.py and
// cron/scheduler.py have them.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/cron"
)

var utc = time.UTC

func at(s string) time.Time {
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		panic(err)
	}
	return t
}

func TestParseSchedule(t *testing.T) {
	now := at("2026-11-03T10:00:00Z")
	cases := []struct {
		in    string
		kind  string
		check func(cron.Schedule) bool
	}{
		{"every 30m", "interval", func(s cron.Schedule) bool { return s.Minutes == 30 && s.Display == "every 30m" }},
		{"every 2h", "interval", func(s cron.Schedule) bool { return s.Minutes == 120 && s.Display == "every 120m" }},
		{"Every 1 day", "interval", func(s cron.Schedule) bool { return s.Minutes == 1440 }},
		{"0 7 * * *", "cron", func(s cron.Schedule) bool { return s.Expr == "0 7 * * *" && s.Display == "0 7 * * *" }},
		{"*/15 9-17 * * 1-5", "cron", func(s cron.Schedule) bool { return s.Expr == "*/15 9-17 * * 1-5" }},
		{"2026-11-04T09:30:00+00:00", "once", func(s cron.Schedule) bool {
			return at(s.RunAt).Equal(at("2026-11-04T09:30:00Z")) && s.Display == "once at 2026-11-04 09:30"
		}},
		{"2026-11-04T09:30", "once", func(s cron.Schedule) bool { return at(s.RunAt).Equal(at("2026-11-04T09:30:00Z")) }},
		{"45m", "once", func(s cron.Schedule) bool {
			return at(s.RunAt).Equal(now.Add(45*time.Minute)) && s.Display == "once in 45m"
		}},
		{"2 hours", "once", func(s cron.Schedule) bool { return at(s.RunAt).Equal(now.Add(2 * time.Hour)) }},
	}
	for _, c := range cases {
		s, err := cron.ParseSchedule(c.in, now.In(utc))
		if err != nil || s.Kind != c.kind || !c.check(s) {
			t.Errorf("ParseSchedule(%q) = %+v, %v", c.in, s, err)
		}
	}
	for _, bad := range []string{"whenever", "every", "61 * * * *x", "0 7 * * * *", "every 5 weeks", ""} {
		if _, err := cron.ParseSchedule(bad, now); err == nil {
			t.Errorf("ParseSchedule(%q) should fail", bad)
		}
	}
}

func TestNextRun(t *testing.T) {
	now := at("2026-11-03T10:00:00Z")
	daily, _ := cron.ParseSchedule("0 7 * * *", now)
	if n, ok := cron.NextRun(daily, nil, now); !ok || !n.Equal(at("2026-11-04T07:00:00Z")) {
		t.Fatalf("daily next = %v, %v", n, ok)
	}
	last := at("2026-11-03T06:59:00Z")
	if n, _ := cron.NextRun(daily, &last, now); !n.Equal(at("2026-11-03T07:00:00Z")) {
		t.Fatalf("daily after last = %v", n)
	}
	weekdays, _ := cron.ParseSchedule("30 9 * * 1-5", now) // 2026-11-07 is a Saturday
	sat := at("2026-11-06T10:00:00Z")
	if n, _ := cron.NextRun(weekdays, &sat, now); !n.Equal(at("2026-11-09T09:30:00Z")) {
		t.Fatalf("weekdays after Friday = %v", n)
	}
	every, _ := cron.ParseSchedule("every 10m", now)
	end := at("2026-11-03T10:03:00Z")
	if n, _ := cron.NextRun(every, &end, now); !n.Equal(at("2026-11-03T10:13:00Z")) {
		t.Fatalf("an interval anchors on the last run's end: %v", n)
	}
	soon, _ := cron.ParseSchedule("2026-11-03T09:59:00Z", now)
	if n, ok := cron.NextRun(soon, nil, now); !ok || !n.Equal(at("2026-11-03T09:59:00Z")) {
		t.Fatalf("a one-shot within 120 s = %v, %v", n, ok)
	}
	stale, _ := cron.ParseSchedule("2026-11-03T09:00:00Z", now)
	if _, ok := cron.NextRun(stale, nil, now); ok {
		t.Fatal("a one-shot more than 120 s past has no next run")
	}
	if _, ok := cron.NextRun(soon, &end, now); ok {
		t.Fatal("a one-shot that ran has no next run")
	}
}

func TestGrace(t *testing.T) {
	for in, want := range map[string]time.Duration{
		"every 1m": 120 * time.Second, "every 10m": 300 * time.Second, "every 1h": 30 * time.Minute, "every 1d": 2 * time.Hour,
	} {
		s, _ := cron.ParseSchedule(in, time.Now())
		if g := cron.Grace(s); g != want {
			t.Errorf("Grace(%s) = %v, want %v", in, g, want)
		}
	}
}

func job(id string, enabled bool, next string, sched map[string]any) cron.Job {
	j := cron.Job{"id": id, "name": id, "enabled": enabled, "state": "scheduled", "schedule": sched, "repeat": map[string]any{"times": nil, "completed": 0}}
	if next != "" {
		j["next_run_at"] = next
	}
	return j
}

func TestDue(t *testing.T) {
	now := at("2026-11-03T10:00:00Z")
	every := map[string]any{"kind": "interval", "minutes": 10, "display": "every 10m"}
	jobs := []cron.Job{
		job("aaaaaaaaaaa1", true, "2026-11-03T09:59:00+00:00", every),
		job("aaaaaaaaaaa2", true, "2026-11-03T10:05:00+00:00", every),
		job("aaaaaaaaaaa3", false, "2026-11-03T09:00:00+00:00", every),
		job("aaaaaaaaaaa4", true, "2026-11-01T00:00:00+00:00", every), // days late: still fires once
		job("aaaaaaaaaaa5", true, "2026-11-01T00:00:00+00:00", map[string]any{"kind": "once", "run_at": "2026-11-01T00:00:00+00:00"}),
	}
	paused := job("aaaaaaaaaaa6", true, "2026-11-03T09:00:00+00:00", every)
	paused["state"], paused["paused_at"] = "paused", "2026-11-02T00:00:00+00:00"
	jobs = append(jobs, paused)
	got := strings.Join(cron.Due(jobs, now), ",")
	if got != "aaaaaaaaaaa1,aaaaaaaaaaa4,aaaaaaaaaaa5" {
		t.Fatalf("Due = %s", got)
	}
}

func TestSilentAndFrame(t *testing.T) {
	for _, s := range []string{"[SILENT]", "silent", "  NO_REPLY ", "NO  REPLY", "[SILENT] nothing new", "Checked.\n\n[SILENT]", "[SILENT]\nall quiet"} {
		if !cron.Silent(s) {
			t.Errorf("Silent(%q) = false", s)
		}
	}
	for _, s := range []string{"The disk is 91% full.", "", "not silent at all"} {
		if cron.Silent(s) {
			t.Errorf("Silent(%q) = true", s)
		}
	}
	want := "Scheduled job: Morning digest (id abc123abc123)\n\nAll good.\n\n" +
		"To change or cancel this job, message me, for instance \"cancel the Morning digest job\"."
	if got := cron.Frame("Morning digest", "abc123abc123", "All good."); got != want {
		t.Fatalf("Frame =\n%s", got)
	}
}

func TestTheFileRoundTrips(t *testing.T) {
	home := t.TempDir()
	dir := filepath.Join(home, "cron")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	src := "\ufeff" + `{"jobs": [{"id": "abc123abc123", "name": "kept", "prompt": "p", "monitor_state": {"last_output_hash": "x"},
		"model_snapshot": "auto", "custom_future_field": [1, 2], "schedule": {"kind": "interval", "minutes": 10, "display": "every 10m"},
		"repeat": {"times": null, "completed": 7}, "enabled": true, "state": "scheduled"},
		{"job_id": "def456def456", "name": "old key"}], "updated_at": "2026-01-01T00:00:00+00:00"}`
	if err := os.WriteFile(filepath.Join(dir, "jobs.json"), []byte(src), 0o600); err != nil {
		t.Fatal(err)
	}
	jobs, err := cron.Load(home)
	if err != nil || len(jobs) != 2 || jobs[1]["id"] != "def456def456" {
		t.Fatalf("Load = %v, %v", jobs, err)
	}
	if err := cron.Update(home, func(js []cron.Job) ([]cron.Job, error) {
		js[0]["last_status"] = "ok"
		js[0]["prompt"] = "use <b> & café"
		return js, nil
	}); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(filepath.Join(dir, "jobs.json"))
	var file struct {
		Jobs      []map[string]any `json:"jobs"`
		UpdatedAt string           `json:"updated_at"`
	}
	if err := json.Unmarshal(b, &file); err != nil {
		t.Fatalf("written: %s", b)
	}
	j := file.Jobs[0]
	if j["last_status"] != "ok" || j["custom_future_field"] == nil || j["monitor_state"] == nil || j["model_snapshot"] != "auto" ||
		file.UpdatedAt == "2026-01-01T00:00:00+00:00" || file.UpdatedAt == "" {
		t.Fatalf("unknown fields survive and updated_at moves: %s", b)
	}
	if !strings.Contains(string(b), `"use <b> & café"`) {
		t.Fatalf("text is written as is, as the Python's ensure_ascii=False writes it: %s", b)
	}
	if !strings.HasPrefix(string(b), "{\n  \"jobs\": [\n    {") {
		t.Fatalf("two-space indents, as the Python writes: %.40q", b)
	}
	if fi, _ := os.Stat(filepath.Join(dir, "jobs.json")); fi.Mode().Perm() != 0o600 {
		t.Fatalf("mode = %v", fi.Mode().Perm())
	}
	if _, err := os.Stat(filepath.Join(dir, ".jobs.lock")); err != nil {
		t.Fatal("the jobs lock file is the Python's")
	}
	if err := os.WriteFile(filepath.Join(dir, "jobs.json"), []byte(`[{"id": "aaa111aaa111"}]`), 0o600); err != nil {
		t.Fatal(err)
	}
	if jobs, err := cron.Load(home); err != nil || len(jobs) != 1 {
		t.Fatalf("a bare list = %v, %v", jobs, err)
	}
}
