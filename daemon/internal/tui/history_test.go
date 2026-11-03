package tui_test

// Phase 9a: the prompt history file, shared with the Textual TUI
// (prompt_toolkit's format; daemon/phases/09a-tui-core.md).

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/tui"
)

func TestHistoryReadsAndWritesTheSharedFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), ".son_of_anton_history")
	if got := tui.ReadHistory(path); len(got) != 0 {
		t.Fatalf("a missing file = %q", got)
	}
	if err := tui.AppendHistory(path, "first"); err != nil {
		t.Fatal(err)
	}
	if fi, err := os.Stat(path); err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("created %v, %v", fi, err)
	}
	// What the Textual TUI (and prompt_toolkit before it) wrote.
	py := "\n# 2026-10-01 09:00:00.000001\n+git status\n" +
		"\n# 2026-10-01 09:01:00.000002\n+line one\n+line two\n" +
		"\n# 2026-10-01 09:02:00.000003\n+git status\n"
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString(py); err != nil {
		t.Fatal(err)
	}
	f.Close()
	if err := tui.AppendHistory(path, "a\nb"); err != nil {
		t.Fatal(err)
	}
	got := tui.ReadHistory(path)
	want := []string{"first", "git status", "line one\nline two", "git status", "a\nb"}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("read %q, want %q", got, want)
	}
	b, _ := os.ReadFile(path)
	if !regexp.MustCompile(`\n# \d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{6}\n\+a\n\+b\n$`).Match(b) {
		t.Fatalf("an appended entry is not in prompt_toolkit's shape: %q", b[max(0, len(b)-60):])
	}
}

func TestHistoryReadsOnlyTheTail(t *testing.T) {
	path := filepath.Join(t.TempDir(), ".son_of_anton_history")
	entry := func(i int) string { return fmt.Sprintf("entry-%04d-%s", i, strings.Repeat("x", 1000)) }
	var sb strings.Builder
	for i := range 600 {
		fmt.Fprintf(&sb, "\n# 2026-10-01 09:00:00.%06d\n+%s\n", i, entry(i))
	}
	if sb.Len() <= 512*1024 {
		t.Fatalf("the fixture is only %d bytes", sb.Len())
	}
	if err := os.WriteFile(path, []byte(sb.String()), 0o600); err != nil {
		t.Fatal(err)
	}
	got := tui.ReadHistory(path)
	if len(got) != 500 || got[0] != entry(100) || got[499] != entry(599) {
		t.Fatalf("read %d entries, from %.10q to %.10q", len(got), got[0], got[len(got)-1])
	}
}
