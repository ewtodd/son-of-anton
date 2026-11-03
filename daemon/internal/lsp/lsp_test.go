package lsp

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func errAt(line int, msg string) Diagnostic {
	d := Diagnostic{Severity: 1, Source: "t", Message: msg}
	d.Range.Start.Line = line
	return d
}

func TestNewErrorsAreBeyondTheOldOnesCount(t *testing.T) {
	before := []Diagnostic{errAt(1, "a"), errAt(2, "a"), {Severity: 2, Message: "w"}}
	after := []Diagnostic{errAt(5, "a"), errAt(6, "a"), errAt(7, "a"), errAt(8, "b"), {Severity: 2, Message: "w2"}}
	got := newErrors(before, after)
	if len(got) != 2 || got[0].Range.Start.Line != 7 || got[1].Message != "b" {
		t.Fatalf("one more of the same and a different one are new, moved ones are not: %+v", got)
	}
	if got := newErrors(after, before); len(got) != 0 {
		t.Fatalf("fewer errors introduce none: %+v", got)
	}
}

func TestReportLimits(t *testing.T) {
	var errs []Diagnostic
	for i := 0; i < 25; i++ {
		errs = append(errs, errAt(i, "m"))
	}
	got := Report("/p/a.py", errs)
	if strings.Count(got, "\nERROR ") != maxPerFile || !strings.Contains(got, "\n... and 5 more\n</diagnostics>") {
		t.Fatalf("20 lines then the rest counted:\n%s", got)
	}
	long := errAt(0, strings.Repeat("x", 5000))
	long.Code = "c\r\nd<"
	got = Report(`/p/"a'&.py`, []Diagnostic{long, long, long, long, long, long, long, long, long, long, long, long, long, long, long})
	if r := []rune(got); len(r) > maxTotal || !strings.HasSuffix(got, truncMarker) {
		t.Fatalf("cut at %d characters (%d): ...%q", maxTotal, len(r), got[len(got)-30:])
	}
	if !strings.Contains(got, `file="/p/&quot;a&#x27;&amp;.py"`) || !strings.Contains(got, "[c  d&lt;]") {
		t.Fatalf("escaping:\n%.300s", got)
	}
	if Report("/p/a.py", nil) != "" {
		t.Fatal("no errors, no report")
	}
}

func project(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	for _, p := range []string{"sub/deep", "pkg"} {
		if err := os.MkdirAll(filepath.Join(dir, p), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	// A .git file, as in a linked worktree, counts.
	if err := os.WriteFile(filepath.Join(dir, ".git"), []byte("gitdir: x\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func serverCfg(argv ...string) Config {
	return Config{WaitTimeout: time.Second, Servers: map[string]ServerConfig{"pyright": {Command: argv}}}
}

func TestResolve(t *testing.T) {
	dir := project(t)
	if err := os.WriteFile(filepath.Join(dir, "sub", "pyproject.toml"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	s := New(serverCfg("sh", "-c", "cat"))
	defer s.Close()
	sp := s.resolve(filepath.Join(dir, "sub", "deep", "a.py"))
	if sp == nil || sp.root != filepath.Join(dir, "sub") || sp.language != "python" {
		t.Fatalf("the nearest marker is the root: %+v", sp)
	}
	if sp := s.resolve(filepath.Join(dir, "pkg", "a.py")); sp == nil || sp.root != dir {
		t.Fatalf("no marker: the git root: %+v", sp)
	}
	if s.resolve(filepath.Join(dir, "a.txt")) != nil {
		t.Fatal("no server claims .txt")
	}
	if s.resolve(filepath.Join(t.TempDir(), "a.py")) != nil {
		t.Fatal("outside a git worktree")
	}
	off := New(Config{Servers: map[string]ServerConfig{"pyright": {Disabled: true}}})
	defer off.Close()
	if off.resolve(filepath.Join(dir, "a.py")) != nil {
		t.Fatal("a disabled server")
	}
	if New(Config{Disabled: true}) != nil {
		t.Fatal("a disabled service is nil")
	}
	var none *Service
	if none.Begin(filepath.Join(dir, "a.py")).Finish(context.Background(), "x") != "" {
		t.Fatal("a nil service reports nothing")
	}
}

func alive(pid int) bool { return syscall.Kill(pid, 0) == nil }

// A server that never answers holds the edit for no more than the wait
// timeout, and closing the service ends its process.
func TestSilentServerIsAbandonedAndStopped(t *testing.T) {
	dir := project(t)
	pidFile := filepath.Join(t.TempDir(), "pid")
	s := New(serverCfg("sh", "-c", fmt.Sprintf("echo $$ > %s; exec sleep 60", pidFile)))
	start := time.Now()
	e := s.Begin(filepath.Join(dir, "a.py"))
	if got := e.Finish(context.Background(), "ERROR\n"); got != "" {
		t.Fatalf("report from a silent server: %q", got)
	}
	if took := time.Since(start); took > 2*time.Second {
		t.Fatalf("held the edit for %v (wait timeout 1 s)", took)
	}
	b, err := os.ReadFile(pidFile)
	if err != nil {
		t.Fatal(err)
	}
	var pid int
	fmt.Sscan(string(b), &pid)
	s.Close()
	deadline := time.Now().Add(5 * time.Second)
	for alive(pid) && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if alive(pid) {
		t.Fatalf("server %d outlived Close", pid)
	}
}

// A server that dies during initialize is not started again.
func TestFailedServerIsNotRetried(t *testing.T) {
	dir := project(t)
	count := filepath.Join(t.TempDir(), "starts")
	s := New(serverCfg("sh", "-c", fmt.Sprintf("echo x >> %s", count)))
	defer s.Close()
	for i := 0; i < 3; i++ {
		s.Begin(filepath.Join(dir, "a.py")).Finish(context.Background(), "x\n")
		time.Sleep(100 * time.Millisecond)
	}
	b, _ := os.ReadFile(count)
	if n := strings.Count(string(b), "x"); n != 1 {
		t.Fatalf("started %d times", n)
	}
}
