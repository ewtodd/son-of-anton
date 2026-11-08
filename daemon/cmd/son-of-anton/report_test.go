package main

// Phase 13: son-of-anton view ... report and sessions report
// (daemon/phases/13-reports.md).

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// pdfText is the text of the PDF file f, laid out.
func pdfText(t *testing.T, f string) string {
	t.Helper()
	b, err := os.ReadFile(f)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(string(b), "%PDF-") {
		t.Fatalf("%s is not a PDF: %.40q", f, b)
	}
	out, err := exec.Command("pdftotext", "-layout", f, "-").Output()
	if err != nil {
		t.Fatalf("pdftotext (poppler-utils, in the dev shell): %v", err)
	}
	return string(out)
}

// worker stores a worker with a request, a tool call and a reply, and a
// subagent its task call names.
func worker(t *testing.T, h string) string {
	t.Helper()
	at := time.Date(2026, 11, 8, 9, 30, 0, 0, time.UTC)
	calls, _ := json.Marshal([]map[string]any{
		{"id": "c1", "type": "function", "function": map[string]any{"name": "terminal", "arguments": `{"command":"go build ./..."}`}},
		{"id": "c2", "type": "function", "function": map[string]any{"name": "task", "arguments": `{"description":"Check the imports","prompt":"check","subagent_type":"explore"}`}},
	})
	id := session(t, h, store.Session{Kind: "interactive", Title: "Fix the build", Cwd: "/srv/shop"},
		store.Message{Role: "user", Content: "fix the build please", TS: at},
		store.Message{Role: "assistant", Content: "Building first.", ToolCalls: calls, TS: at.Add(time.Second)},
		store.Message{Role: "tool", ToolCallID: "c1", ToolName: "terminal", Content: `{"output":"main.go:3: missing import","exit_code":1}`, TS: at.Add(2 * time.Second)},
	)
	child := session(t, h, store.Session{Kind: "child", ParentID: id, Title: "Check the imports (@explore subagent)", Agent: "explore", Cwd: "/srv/shop"},
		store.Message{Role: "user", Content: "check the imports", TS: at.Add(3 * time.Second)},
		store.Message{Role: "assistant", Content: "strings is missing", TS: at.Add(4 * time.Second)},
	)
	st, err := store.Open(filepath.Join(h, "soa-d.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	if _, err := st.AppendMessages(t.Context(), id,
		store.Message{Role: "tool", ToolCallID: "c2", ToolName: "task", Content: `<task id="` + child + `" state="completed">` + "\nstrings is missing\n</task>", TS: at.Add(5 * time.Second)},
		store.Message{Role: "assistant", Content: "Fixed: **added** the import.", TS: at.Add(6 * time.Second)},
	); err != nil {
		t.Fatal(err)
	}
	return id
}

func TestViewReport(t *testing.T) {
	h := home(t, "")
	sock := serveView(t, h)
	id := worker(t, h)
	chat := session(t, h, store.Session{Kind: "chat", Title: "a private chat", Scope: "gateway", Chat: "signal:group.x"},
		store.Message{Role: "user", Content: "a private message"})
	dir := t.TempDir()

	r := cli(t, dir, "", nil, "view", "--socket", sock, "report", id)
	if r.code != 0 || r.out != "wrote "+id+".pdf\n" {
		t.Fatalf("view report: %d %q %q", r.code, r.out, r.errOut)
	}
	text := pdfText(t, filepath.Join(dir, id+".pdf"))
	for _, want := range []string{"Fix the build", id, "fix the build please", "Building first.", "go build ./...",
		"main.go:3: missing import", "Subagent: Check the imports", "strings is missing", "Fixed: added the import."} {
		if !strings.Contains(text, want) {
			t.Errorf("the report lacks %q:\n%s", want, text)
		}
	}

	out := filepath.Join(dir, "sub", "r.pdf")
	if err := os.Mkdir(filepath.Dir(out), 0o755); err != nil {
		t.Fatal(err)
	}
	if r := cli(t, dir, "", nil, "view", "--socket", sock, "report", id, "-o", out); r.code != 0 || r.out != "wrote "+out+"\n" {
		t.Fatalf("view report -o: %d %q %q", r.code, r.out, r.errOut)
	}
	pdfText(t, out)
	r = cli(t, dir, "", nil, "view", "--socket", sock, "report", "-o", "-", id)
	if r.code != 0 || !strings.HasPrefix(r.out, "%PDF-") || strings.Contains(r.out, "wrote") {
		t.Fatalf("view report -o -: %d %.40q %q", r.code, r.out, r.errOut)
	}

	if r := cli(t, dir, "", nil, "view", "--socket", sock, "report", chat); r.code != 1 || !strings.Contains(r.errOut, "no such session") {
		t.Fatalf("a chat's report: %d %q %q", r.code, r.out, r.errOut)
	}
	if _, err := os.Stat(filepath.Join(dir, chat+".pdf")); err == nil {
		t.Fatal("a chat's report was written")
	}
}

func TestSessionsReport(t *testing.T) {
	h := home(t, "")
	serveView(t, h)
	id := worker(t, h)
	dir := t.TempDir()
	if r := cli(t, dir, "", nil, "--home", h, "sessions", "report", id, "-o", "mine.pdf"); r.code != 0 || r.out != "wrote mine.pdf\n" {
		t.Fatalf("sessions report: %d %q %q", r.code, r.out, r.errOut)
	}
	if text := pdfText(t, filepath.Join(dir, "mine.pdf")); !strings.Contains(text, "fix the build please") {
		t.Fatalf("the report:\n%s", text)
	}
}

func TestReportNeverStartsADaemon(t *testing.T) {
	h := home(t, "")
	bin, record := stub(t)
	missing := filepath.Join(t.TempDir(), "view.sock")
	r := cli(t, t.TempDir(), "", []string{"SOA_D_BIN=" + bin, "SON_OF_ANTON_HOME=" + h}, "view", "--socket", missing, "report", "20261108_093000_abcdef")
	if r.code != 1 || !strings.Contains(r.errOut, "cannot reach the view socket") {
		t.Fatalf("exit %d, %q", r.code, r.errOut)
	}
	if _, err := os.Stat(record); err == nil {
		t.Fatal("view report ran soa-d")
	}
	if _, err := os.Stat(filepath.Join(h, "run")); err == nil {
		t.Fatal("view report touched the home")
	}
}

func TestReportUsageErrors(t *testing.T) {
	h := home(t, "")
	bin, record := stub(t)
	for _, args := range [][]string{
		{"view", "shop", "report"},
		{"view", "shop", "report", "a", "b"},
		{"view", "shop", "report", "a", "-o"},
		{"view", "shop", "report", "a", "--all"},
		{"view", "shop", "show", "a", "-o", "x.pdf"},
		{"view", "shop", "a", "-o", "x.pdf"},
		{"--home", h, "sessions", "report"},
		{"--home", h, "sessions", "report", "a", "b"},
		{"--home", h, "sessions", "report", "a", "-o"},
		{"--home", h, "sessions", "list", "-o", "x.pdf"},
	} {
		_ = os.Remove(record)
		r := cli(t, t.TempDir(), "", []string{"SOA_D_BIN=" + bin}, args...)
		if r.code != 2 || r.errOut == "" {
			t.Errorf("%v: exit %d, stderr %q", args, r.code, r.errOut)
		}
		if _, err := os.Stat(record); err == nil {
			t.Errorf("%v ran soa-d", args)
		}
	}
}
