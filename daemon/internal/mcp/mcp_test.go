package mcp

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestToolNamesAreSafe(t *testing.T) {
	for _, in := range []string{"echo", "shout-loud", "a.b c/d", "ünï"} {
		n := toolName("my-srv", in)
		if !regexpSafe(n) || !strings.HasPrefix(n, "mcp__my_srv__") {
			t.Errorf("toolName(%q) = %q", in, n)
		}
	}
}

func regexpSafe(s string) bool { return !unsafeName.MatchString(s) }

func TestFiltersIncludeWins(t *testing.T) {
	cfg := Server{Include: []string{"get_*"}, Exclude: []string{"get_secret"}}
	if !allowed(cfg, "get_secret") || allowed(cfg, "put_x") {
		t.Fatal("include decides; exclude is ignored when include is set")
	}
	cfg = Server{Exclude: []string{"hidden*"}}
	if allowed(cfg, "hidden_tool") || !allowed(cfg, "echo") {
		t.Fatal("exclude globs")
	}
}

func TestRankFindsByWordsThenBySubstring(t *testing.T) {
	cat := []entry{
		newEntry("s", "send_mail", "Send an email message.", map[string]any{"properties": map[string]any{"to": 1}}),
		newEntry("s", "list_files", "List the files of a folder.", nil),
		newEntry("s", "zzqq", "Odd.", nil),
	}
	if got := rank(cat, "email to send", 5); len(got) == 0 || got[0].raw != "send_mail" {
		t.Fatalf("by words: %v", got)
	}
	if got := rank(cat, "zz", 5); len(got) != 1 || got[0].raw != "zzqq" {
		t.Fatalf("by substring: %v", got)
	}
	if got := rank(cat, "nothing like it", 5); len(got) != 0 {
		t.Fatalf("no match: %v", got)
	}
}

func TestClipKeepsHeadAndTailAndSavesTheWhole(t *testing.T) {
	dir := t.TempDir()
	text := strings.Repeat("a", headChars) + strings.Repeat("m", 20000) + strings.Repeat("z", tailChars)
	got := clip(dir, "call1", text)
	if !strings.HasPrefix(got, strings.Repeat("a", headChars)) || !strings.HasSuffix(got, strings.Repeat("z", tailChars)) ||
		!strings.Contains(got, "20000 characters omitted; the full result is saved at "+filepath.Join(dir, "call1.txt")) {
		t.Fatalf("clip: %.80s ... %d chars", got, len(got))
	}
	if b, _ := os.ReadFile(filepath.Join(dir, "call1.txt")); string(b) != text {
		t.Fatal("the full text is saved")
	}
	if clip(dir, "x", "short") != "short" {
		t.Fatal("a short text is untouched")
	}
}

func TestExpandNamesTheMissingVariable(t *testing.T) {
	m := &Manager{getenv: func(n string) string {
		if n == "SET" {
			return "v"
		}
		return ""
	}}
	if got, err := m.expand("a ${SET} ${env:SET}"); err != nil || got != "a v v" {
		t.Fatalf("expand = %q, %v", got, err)
	}
	if _, err := m.expand("Bearer ${GONE}"); err == nil || !strings.Contains(err.Error(), "GONE") {
		t.Fatalf("expand error = %v", err)
	}
}
