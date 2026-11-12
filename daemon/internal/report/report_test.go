package report_test

// Phase 13: a session as a PDF (daemon/phases/13-reports.md).

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/report"
)

var at = time.Date(2026, 11, 8, 9, 30, 0, 0, time.UTC)

func ts(d time.Duration) string { return at.Add(d).Format(time.RFC3339Nano) }

func calls(t *testing.T, cs ...[3]string) []report.ToolCall {
	t.Helper()
	var out []report.ToolCall
	for _, c := range cs {
		out = append(out, report.ToolCall{ID: c[0], Name: c[1], Arguments: c[2]})
	}
	return out
}

func toolMsg(id, name, content string) report.Message {
	return report.Message{Role: "tool", ToolCallID: id, ToolName: name, Content: content}
}

// fixture is a worker's session: two requests, a background receipt, tool
// calls of every kind of output, a subagent its task call names, and one no
// call names.
func fixture(t *testing.T) report.Session {
	long := make([]string, 30)
	for i := range long {
		long[i] = "line " + string(rune('a'+i%26))
	}
	termOut, _ := json.Marshal(map[string]any{"output": "\x1b[1;31mFAIL\x1b[0m relay\r\nok  store", "exit_code": 1})
	longOut, _ := json.Marshal(map[string]any{"output": strings.Join(long, "\n"), "exit_code": 0})
	blocked, _ := json.Marshal(map[string]any{"error": "blocked: rm -rf / is never run", "status": "blocked", "exit_code": -1, "output": ""})
	explore := report.Session{
		ID: "20261108_093100_child1", Kind: "child", Title: "Find the filter (@explore subagent)", Agent: "explore",
		Messages: []report.Message{
			{Role: "user", Content: "Find where empty sessions are left out.", TS: ts(60 * time.Second)},
			{Role: "assistant", ToolCalls: calls(t, [3]string{"x1", "search_files", `{"pattern":"Messages == 0"}`}), TS: ts(61 * time.Second)},
			toolMsg("x1", "search_files", "relay.go:625: if sum.Messages == 0"),
			{Role: "assistant", Content: "`relayList` skips them.", TS: ts(62 * time.Second)},
		},
	}
	stray := report.Session{
		ID: "20261108_093200_child2", Kind: "child", Title: "Look around (@general subagent)", Agent: "general",
		Messages: []report.Message{{Role: "user", Content: "Look around.", TS: ts(70 * time.Second)}},
	}
	return report.Session{
		ID: "20261108_093000_abcdef", Kind: "interactive", Title: "Fix the flaky relay test", Cwd: "/srv/ricky/repo",
		Model: "glm-5.3-flash", Created: at, Updated: at.Add(42 * time.Minute), InputTokens: 41203, OutputTokens: 2110,
		Messages: []report.Message{
			{Role: "user", Content: "The relay test **fails** sometimes.", TS: ts(0), Images: []report.Image{{ID: "i1", Mime: "image/png"}}},
			{Role: "assistant", Content: "Let me look.", Reasoning: "secret thoughts", TS: ts(30 * time.Second), ToolCalls: calls(t,
				[3]string{"c1", "terminal", `{"command":"go test ./internal/server/"}`},
				[3]string{"c2", "read_file", `{"path":"internal/server/relay_queue_test.go"}`},
				[3]string{"c3", "terminal", `{"command":"seq 30"}`},
				[3]string{"c4", "terminal", `{"command":"rm -rf /"}`},
				[3]string{"c5", "task", `{"description":"Find the filter","prompt":"find it","subagent_type":"explore"}`},
			)},
			toolMsg("c1", "terminal", string(termOut)),
			toolMsg("c2", "read_file", `{"content":"package server_test\n..."}`),
			toolMsg("c3", "terminal", string(longOut)),
			toolMsg("c4", "terminal", string(blocked)),
			toolMsg("c5", "task", `<task id="20261108_093100_child1" state="completed">`+"\n`relayList` skips them.\n</task>"),
			{Role: "assistant", Content: "[response interrupted]", TS: ts(90 * time.Second)},
			{Role: "user", Content: "[background receipt] process p1 exited with code 0\n$ make\nok", TS: ts(100 * time.Second)},
			{Role: "user", Content: "Carry on.", TS: ts(110 * time.Second)},
			{Role: "assistant", Content: "Fixed: the interrupt lands **before** the first step.", TS: ts(42 * time.Minute)},
		},
		Children: []report.Session{explore, stray},
	}
}

func TestBuild(t *testing.T) {
	doc := report.Build(fixture(t), time.UTC)
	if doc.Title != "Fix the flaky relay test" || doc.ID != "20261108_093000_abcdef" || doc.Kind != "interactive" || doc.Cwd != "/srv/ricky/repo" {
		t.Fatalf("header = %q %q %q %q", doc.Title, doc.ID, doc.Kind, doc.Cwd)
	}
	wantFacts := [][2]string{
		{"Model", "glm-5.3-flash"},
		{"Started", "2026-11-08 09:30"},
		{"Last activity", "2026-11-08 10:12"},
		{"Requests", "2"},
		{"Tool calls", "5"},
		{"Tokens", "41,203 in · 2,110 out"},
	}
	if !reflect.DeepEqual(doc.Facts, wantFacts) {
		t.Fatalf("facts = %q", doc.Facts)
	}
	long := []string{}
	for i := range 12 {
		long = append(long, "line "+string(rune('a'+i)))
	}
	want := []report.Entry{
		{Type: "user", TS: "2026-11-08 09:30", Text: "The relay test **fails** sometimes.\n\n[1 image]"},
		{Type: "assistant", TS: "2026-11-08 09:30", Text: "Let me look."},
		{Type: "tool", Name: "terminal", Summary: "go test ./internal/server/", Output: "exit 1\nFAIL relay\nok  store"},
		{Type: "tool", Name: "read_file", Summary: "internal/server/relay_queue_test.go"},
		{Type: "tool", Name: "terminal", Summary: "seq 30", Output: strings.Join(long, "\n"), More: 18},
		{Type: "tool", Name: "terminal", Summary: "rm -rf /", Output: "blocked: rm -rf / is never run", Error: true},
		{Type: "tool", Name: "task", Summary: "Explore Task — Find the filter"},
		{Type: "subagent", Title: "Find the filter", Agent: "explore", Entries: []report.Entry{
			{Type: "user", TS: "2026-11-08 09:31", Text: "Find where empty sessions are left out."},
			{Type: "tool", Name: "search_files", Summary: "Messages == 0", Output: "relay.go:625: if sum.Messages == 0"},
			{Type: "assistant", TS: "2026-11-08 09:31", Text: "`relayList` skips them."},
		}},
		{Type: "note", Text: "[response interrupted]"},
		{Type: "note", Text: "[background receipt] process p1 exited with code 0\n$ make\nok"},
		{Type: "user", TS: "2026-11-08 09:31", Text: "Carry on."},
		{Type: "assistant", TS: "2026-11-08 10:12", Text: "Fixed: the interrupt lands **before** the first step."},
		{Type: "subagent", Title: "Look around", Agent: "general", Entries: []report.Entry{
			{Type: "user", TS: "2026-11-08 09:31", Text: "Look around."},
		}},
	}
	if len(doc.Entries) != len(want) {
		got, _ := json.MarshalIndent(doc.Entries, "", " ")
		t.Fatalf("%d entries, want %d:\n%s", len(doc.Entries), len(want), got)
	}
	for i := range want {
		if !reflect.DeepEqual(doc.Entries[i], want[i]) {
			got, _ := json.Marshal(doc.Entries[i])
			w, _ := json.Marshal(want[i])
			t.Errorf("entry %d:\n got %s\nwant %s", i, got, w)
		}
	}
}

func TestBuildCutsLongOutputByCharacters(t *testing.T) {
	big, _ := json.Marshal(map[string]any{"output": strings.Repeat("x", 4000) + "\nsecond\nthird", "exit_code": 0})
	s := report.Session{ID: "s", Messages: []report.Message{
		{Role: "assistant", ToolCalls: []report.ToolCall{{ID: "c1", Name: "terminal", Arguments: `{"command":"cat big"}`}}},
		toolMsg("c1", "terminal", string(big)),
	}}
	e := report.Build(s, time.UTC).Entries[0]
	if len([]rune(e.Output)) > 1501 || !strings.HasPrefix(e.Output, "xxxx") || e.More != 2 {
		t.Fatalf("output %d runes, more %d", len([]rune(e.Output)), e.More)
	}
	if d := report.Build(report.Session{ID: "s"}, time.UTC); d.Title != "(untitled)" || len(d.Entries) != 0 {
		t.Fatalf("an empty session: %+v", d)
	}
}

func TestSummaryAndIsError(t *testing.T) {
	for _, c := range [][3]string{
		{"terminal", `{"command":"go   test\n./..."}`, "go test ./..."},
		{"patch", `{"path":"a.go","old":"x"}`, "a.go"},
		{"todo", `{"todos":[{},{}]}`, "2 items"},
		{"task", `{"description":"Look","subagent_type":"explore","background":true}`, "Explore Task (background) — Look"},
		{"web_search", `{"query":"x"}`, `{"query":"x"}`},
		{"Bash", `{"command":"go   test\n./...","description":"Run them"}`, "go test ./..."},
		{"Grep", `{"pattern":"func main","output_mode":"content"}`, "func main"},
		{"KillShell", `{"shell_id":"bash_ab12"}`, "kill bash_ab12"},
		{"AskUserQuestion", `{"questions":[{"question":"Which?"},{"question":"And?"}]}`, "Which? (+1 more)"},
		{"WebFetch", `{"url":"https://example.com","prompt":"p"}`, "https://example.com"},
		{"WebSearch", `{"query":"x"}`, "x"},
		{"Task", `{"description":"Look","subagent_type":"general-purpose","run_in_background":true}`, "General-Purpose Task (background) — Look"},
	} {
		if got := report.Summary(c[0], c[1]); got != c[2] {
			t.Errorf("Summary(%s) = %q, want %q", c[0], got, c[2])
		}
	}
	if !report.IsError("terminal", `{"error":"no"}`) || report.IsError("terminal", `{"error":""}`) ||
		!report.IsError("task", "<task id=\"x\" state=\"error\">\nboom") || report.IsError("task", "<task id=\"x\" state=\"completed\">") ||
		!report.IsError("Task", "<task id=\"x\" state=\"error\">\nboom") {
		t.Fatal("IsError")
	}
}

func TestFileName(t *testing.T) {
	for _, c := range [][3]string{
		{"Fix the flaky relay test", "s1", "fix-the-flaky-relay-test.pdf"},
		{"  ../../etc/passwd ", "s1", "etc-passwd.pdf"},
		{"Größe prüfen!", "s1", "größe-prüfen.pdf"},
		{"", "20261108_093000_abcdef", "20261108_093000_abcdef.pdf"},
		{"???", "s2", "s2.pdf"},
		{strings.Repeat("word ", 30), "s3", strings.TrimSuffix(strings.Repeat("word-", 12), "-") + ".pdf"},
	} {
		if got := report.FileName(report.Session{Title: c[0], ID: c[1]}); got != c[2] {
			t.Errorf("FileName(%q) = %q, want %q", c[0], got, c[2])
		}
	}
}

// pdfText is the text of a PDF, laid out.
func pdfText(t *testing.T, pdf []byte) string {
	t.Helper()
	if !strings.HasPrefix(string(pdf), "%PDF-") {
		t.Fatalf("not a PDF: %.40q", pdf)
	}
	f := filepath.Join(t.TempDir(), "r.pdf")
	if err := os.WriteFile(f, pdf, 0o600); err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command("pdftotext", "-layout", f, "-").Output()
	if err != nil {
		t.Fatalf("pdftotext (poppler-utils, in the dev shell): %v", err)
	}
	return string(out)
}

func TestRender(t *testing.T) {
	s := fixture(t)
	s.Messages = append(s.Messages, report.Message{Role: "assistant", TS: ts(43 * time.Minute),
		Content: "A table:\n\n| run | result |\n|---|---|\n| before | 3/50 fail |\n\n<!--raw-typst #panic(\"ran typst from a reply\")-->\n\n#panic(\"plain\")"})
	pdf, err := report.Render(context.Background(), s, time.UTC)
	if err != nil {
		t.Fatalf("Render: %v (the dev shell sets SON_OF_ANTON_TYPST)", err)
	}
	text := pdfText(t, pdf)
	for _, want := range []string{"Fix the flaky relay test", "20261108_093000_abcdef", "glm-5.3-flash", "41,203 in · 2,110 out",
		"Request", "The relay test fails sometimes.", "Reply", "go test ./internal/server/", "FAIL relay", "… 18 more lines",
		"Subagent: Find the filter", "relayList skips them.", "3/50 fail", "#panic(\"plain\")", "1 of"} {
		if !strings.Contains(text, want) {
			t.Errorf("the PDF lacks %q:\n%s", want, text)
		}
	}
	if strings.Contains(text, "secret thoughts") || strings.Contains(text, "ran typst") {
		t.Errorf("reasoning or raw typst reached the PDF:\n%s", text)
	}
}

func TestRenderWithoutTypst(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TYPST", filepath.Join(t.TempDir(), "no-typst"))
	_, err := report.Render(context.Background(), report.Session{ID: "s"}, time.UTC)
	if err == nil || !strings.Contains(err.Error(), "typst") || !strings.Contains(err.Error(), "cmarker") {
		t.Fatalf("Render without typst: %v", err)
	}
}

// The fixture above is a session stored before the tools took Claude Code's
// names; one stored after reads the same way.
func TestBuildWithTheToolsNewNames(t *testing.T) {
	out, _ := json.Marshal(map[string]any{"output": "ok  store", "exit_code": 0})
	child := report.Session{ID: "20261112_100100_child1", Kind: "child", Title: "Find it (@Explore subagent)", Agent: "Explore",
		Messages: []report.Message{{Role: "user", Content: "Find it.", TS: ts(60 * time.Second)}}}
	s := report.Session{ID: "s", Messages: []report.Message{
		{Role: "assistant", TS: ts(0), ToolCalls: calls(t,
			[3]string{"c1", "Bash", `{"command":"go test ./internal/store/","description":"Run the store tests"}`},
			[3]string{"c2", "Read", `{"file_path":"internal/store/store.go"}`},
			[3]string{"c3", "Edit", `{"file_path":"a.go","old_string":"x","new_string":"y"}`},
			[3]string{"c4", "Task", `{"description":"Find it","prompt":"find it","subagent_type":"Explore"}`},
			[3]string{"c5", "TodoWrite", `{"todos":[{"content":"a","status":"pending","activeForm":"A"}]}`},
		)},
		toolMsg("c1", "Bash", string(out)),
		toolMsg("c2", "Read", `{"content":"     1\tpackage store"}`),
		toolMsg("c3", "Edit", `{"success":true,"diff":"--- a/a.go"}`),
		toolMsg("c4", "Task", `<task id="20261112_100100_child1" state="completed">`+"\nfound\n</task>"),
		toolMsg("c5", "TodoWrite", `{"todos":[{"content":"a","status":"pending","activeForm":"A"}]}`),
	}, Children: []report.Session{child}}
	want := []report.Entry{
		{Type: "tool", Name: "Bash", Summary: "go test ./internal/store/", Output: "ok  store"},
		{Type: "tool", Name: "Read", Summary: "internal/store/store.go"},
		{Type: "tool", Name: "Edit", Summary: "a.go"},
		{Type: "tool", Name: "Task", Summary: "Explore Task — Find it"},
		{Type: "subagent", Title: "Find it", Agent: "Explore", Entries: []report.Entry{
			{Type: "user", TS: "2026-11-08 09:31", Text: "Find it."},
		}},
		{Type: "tool", Name: "TodoWrite", Summary: "1 item"},
	}
	got := report.Build(s, time.UTC).Entries
	if !reflect.DeepEqual(got, want) {
		g, _ := json.MarshalIndent(got, "", " ")
		t.Fatalf("entries:\n%s", g)
	}
}
