package cron_test

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/cron"
)

func TestResolveTargets(t *testing.T) {
	origin := map[string]any{"platform": "signal", "chat_id": "group:G1"}
	cases := []struct {
		deliver string
		origin  any
		home    string
		want    string
		problem bool
	}{
		{"local", origin, "group:H", "", false},
		{"origin", origin, "group:H", "group:G1", false},
		{"origin", nil, "group:H", "group:H", false},
		{"origin", nil, "", "", false}, // nowhere to send: local
		{"signal", origin, "group:H", "group:H", false},
		{"signal", origin, "", "group:G1", false},
		{"signal:+15551234567", nil, "", "+15551234567", false},
		{"origin,all,signal:group:G2", origin, "group:H", "group:G1,group:H,group:G2", false},
		{"origin,origin", origin, "", "group:G1", false},
		{"signal", nil, "", "", true},
		{"telegram", origin, "", "", true},
	}
	for _, c := range cases {
		got, problem := cron.ResolveTargets(cron.Job{"deliver": c.deliver, "origin": c.origin}, c.home)
		if strings.Join(got, ",") != c.want || (problem != "") != c.problem {
			t.Errorf("deliver %q origin %v home %q = %v, %q", c.deliver, c.origin, c.home, got, problem)
		}
	}
}

func TestScriptsStayInTheScriptsFolder(t *testing.T) {
	home := t.TempDir()
	scripts := filepath.Join(home, "scripts")
	if err := os.MkdirAll(scripts, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(scripts, "ok.sh"), []byte("echo hi\nexit 0\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(scripts, "bad.sh"), []byte("echo oops >&2\nexit 3\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(home, "outside.sh")
	if err := os.WriteFile(outside, []byte("echo escaped\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(scripts, "link.sh")); err != nil {
		t.Fatal(err)
	}
	if out, ok := cron.RunScript(context.Background(), home, "ok.sh", ""); !ok || out != "hi" {
		t.Fatalf("ok.sh = %q, %v", out, ok)
	}
	if out, ok := cron.RunScript(context.Background(), home, "bad.sh", ""); ok || !strings.Contains(out, "code 3") || !strings.Contains(out, "oops") {
		t.Fatalf("bad.sh = %q, %v", out, ok)
	}
	for _, s := range []string{"../outside.sh", outside, "link.sh"} {
		if out, ok := cron.RunScript(context.Background(), home, s, ""); ok || !strings.Contains(out, "outside the scripts directory") {
			t.Errorf("%s ran: %q, %v", s, out, ok)
		}
	}
}

func TestOnlyTheNewestOutputsAreKept(t *testing.T) {
	home := t.TempDir()
	start := time.Date(2026, 11, 3, 0, 0, 0, 0, time.UTC)
	for i := 0; i < 55; i++ {
		if err := cron.SaveOutput(home, "abc", "run", start.Add(time.Duration(i)*time.Second)); err != nil {
			t.Fatal(err)
		}
	}
	files, _ := filepath.Glob(filepath.Join(home, "cron", "output", "abc", "*.md"))
	if len(files) != 50 {
		t.Fatalf("kept %d outputs", len(files))
	}
	if _, err := os.Stat(filepath.Join(home, "cron", "output", "abc", "2026-11-03_00-00-00.md")); err == nil {
		t.Fatal("the oldest output survived")
	}
}

func TestADueRecurringJobMovesToItsNextSlot(t *testing.T) {
	now := at("2026-11-03T10:00:00Z")
	j := job("aaaaaaaaaaa1", true, "2026-11-03T09:00:00+00:00", map[string]any{"kind": "cron", "expr": "0 * * * *", "display": "0 * * * *"})
	if ids := cron.Due([]cron.Job{j}, now); len(ids) != 1 {
		t.Fatalf("a job 60 minutes late is due once: %v", ids)
	}
	if next := j["next_run_at"].(string); !at(next).Equal(at("2026-11-03T11:00:00Z")) {
		t.Fatalf("a late job's slot moves to the next one after now: %s", next)
	}
}
