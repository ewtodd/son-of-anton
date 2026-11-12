package tools_test

// Contract tests for the shell and file tools. Every test runs in temp
// directories; no test runs a command that would do harm if a guard failed.

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/paths"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

var ctx = context.Background()

type fixture struct {
	env  *tools.Env
	home string
	cwd  string
	reg  *tools.Registry
}

func setup(t *testing.T, gate *approval.Gate) *fixture {
	t.Helper()
	home := t.TempDir()
	soa := filepath.Join(home, ".son-of-anton")
	cwd := filepath.Join(home, "proj")
	for _, d := range []string{soa, cwd} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	st, err := tools.NewState(cwd)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	if gate == nil {
		gate = approval.NewGate(approval.Default, nil, nil)
	}
	return &fixture{
		env:  &tools.Env{Policy: paths.Policy{Home: home, SoaHome: soa}, Gate: gate, State: st},
		home: home, cwd: cwd,
		reg: tools.NewRegistry(tools.Builtin()...),
	}
}

func (f *fixture) callCtx(t *testing.T, c context.Context, name string, args any) map[string]any {
	t.Helper()
	tool, ok := f.reg.Lookup(name)
	if !ok {
		t.Fatalf("no tool %q", name)
	}
	raw, err := json.Marshal(args)
	if err != nil {
		t.Fatal(err)
	}
	out, err := tool.Run(c, f.env, raw)
	if err != nil {
		t.Fatalf("%s: infrastructure error: %v", name, err)
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(out), &m); err != nil {
		t.Fatalf("%s returned non-JSON %q: %v", name, out, err)
	}
	return m
}

func (f *fixture) call(t *testing.T, name string, args any) map[string]any {
	t.Helper()
	return f.callCtx(t, ctx, name, args)
}

func (f *fixture) write(t *testing.T, rel, content string) string {
	t.Helper()
	p := filepath.Join(f.cwd, rel)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func read(t *testing.T, p string) string {
	t.Helper()
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func str(m map[string]any, k string) string { s, _ := m[k].(string); return s }
func num(m map[string]any, k string) int    { n, _ := m[k].(float64); return int(n) }
func isErr(m map[string]any) bool           { return str(m, "error") != "" }

// ---- schemas ----

// The tools Claude Code has a counterpart for take its names and parameter
// names (owner, 2026-11), so a model trained on those tools calls them right.
func TestBuiltinToolsAndSchemas(t *testing.T) {
	reg := tools.NewRegistry(tools.Builtin()...)
	var names []string
	params := map[string][]string{}
	for _, s := range reg.Schemas() {
		if s.Type != "function" {
			t.Errorf("%s: type %q", s.Function.Name, s.Type)
		}
		var schema map[string]any
		if err := json.Unmarshal(s.Function.Parameters, &schema); err != nil || schema["type"] != "object" {
			t.Errorf("%s: parameters must be a JSON object schema: %s", s.Function.Name, s.Function.Parameters)
		}
		if s.Function.Description == "" {
			t.Errorf("%s: no description", s.Function.Name)
		}
		props, _ := schema["properties"].(map[string]any)
		for p := range props {
			params[s.Function.Name] = append(params[s.Function.Name], p)
		}
		names = append(names, s.Function.Name)
	}
	want := map[string]string{
		"Bash":            "command,description,run_in_background,timeout",
		"BashOutput":      "bash_id,filter",
		"KillShell":       "shell_id",
		"Read":            "file_path,limit,offset",
		"Write":           "content,file_path",
		"Edit":            "file_path,new_string,old_string,replace_all",
		"Glob":            "path,pattern",
		"Grep":            "-A,-B,-C,-i,-n,glob,head_limit,multiline,output_mode,path,pattern,type",
		"TodoWrite":       "todos",
		"AskUserQuestion": "questions",
		"WebSearch":       "allowed_domains,blocked_domains,query",
		"WebFetch":        "prompt,url",
	}
	for name, ps := range want {
		if got := strings.Join(sorted(params[name]), ","); got != ps {
			t.Errorf("%s parameters = %q, want %q", name, got, ps)
		}
	}
	last := -1
	for _, want := range []string{"Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "Glob", "Grep", "TodoWrite", "AskUserQuestion"} {
		i := strings.Index(","+strings.Join(names, ",")+",", ","+want+",")
		if i <= last {
			t.Fatalf("builtin tools = %v; missing or out of order: %s", names, want)
		}
		last = i
	}
	for _, old := range []string{"terminal", "process", "read_file", "write_file", "patch", "search_files", "todo", "clarify", "web_search", "web_extract"} {
		if _, ok := reg.Lookup(old); ok {
			t.Errorf("the old name %s is still registered", old)
		}
	}
	for _, n := range names {
		if tool, ok := reg.Lookup(n); !ok || tool.Schema().Function.Name != n {
			t.Errorf("Lookup(%q) failed", n)
		}
	}
	a, _ := json.Marshal(reg.Schemas())
	b, _ := json.Marshal(tools.NewRegistry(tools.Builtin()...).Schemas())
	if string(a) != string(b) {
		t.Fatal("schemas must serialize to the same bytes every time")
	}
}

// A model that calls a tool by its old name (a resumed session's history
// shows it those) is told the new name and its parameters, but only when the
// session has the tool that replaced it.
func TestOldToolNamesPointAtTheirReplacements(t *testing.T) {
	all := func(string) bool { return true }
	for old, want := range map[string]string{
		"read_file":    "read_file is now Read (file_path, offset, limit)",
		"write_file":   "write_file is now Write (file_path, content)",
		"patch":        "patch is now Edit (file_path, old_string, new_string, replace_all)",
		"search_files": "Glob (pattern, path)",
		"terminal":     "terminal is now Bash (command, timeout in milliseconds",
		"process":      "BashOutput (bash_id, filter)",
		"todo":         "todo is now TodoWrite",
		"clarify":      "clarify is now AskUserQuestion",
		"web_search":   "web_search is now WebSearch",
		"web_extract":  "web_extract is now WebFetch (url, prompt)",
		"task":         "task is now Task (description, prompt, subagent_type, model)",
	} {
		if got := tools.RenamedTool(old, all); !strings.Contains(got, want) {
			t.Errorf("%s: %q, want it to say %q", old, got, want)
		}
	}
	onlyRead := func(n string) bool { return n == "Read" }
	if got := tools.RenamedTool("terminal", onlyRead); got != "" {
		t.Errorf("a session without Bash is pointed at it: %q", got)
	}
	if got := tools.RenamedTool("delegate_task", all); got != "" {
		t.Errorf("a name that never was a builtin tool: %q", got)
	}
}

// ---- Read ----

func catN(n int, line string) string { return fmt.Sprintf("%6d\t%s", n, line) }

func TestReadShowsLinesTheWayCatNNumbersThem(t *testing.T) {
	f := setup(t, nil)
	f.write(t, "a.txt", "alpha\nbeta\ngamma\n")
	r := f.call(t, "Read", map[string]any{"file_path": "a.txt"})
	if str(r, "content") != "     1\talpha\n     2\tbeta\n     3\tgamma" || num(r, "total_lines") != 3 || r["truncated"] == true {
		t.Fatalf("read = %v", r)
	}
	if r := f.call(t, "Read", map[string]any{"path": "a.txt"}); !strings.Contains(str(r, "error"), "file_path, not path") {
		t.Fatalf("the old parameter name must be pointed at file_path: %v", r)
	}
}

func TestReadOffsetLimitAndContinuation(t *testing.T) {
	f := setup(t, nil)
	f.write(t, "a.txt", "l1\nl2\nl3\nl4\nl5\n")
	r := f.call(t, "Read", map[string]any{"file_path": "a.txt", "offset": 2, "limit": 2})
	if str(r, "content") != catN(2, "l2")+"\n"+catN(3, "l3") || r["truncated"] != true || num(r, "next_offset") != 4 {
		t.Fatalf("read = %v", r)
	}
	r = f.call(t, "Read", map[string]any{"file_path": "a.txt", "offset": 4})
	if str(r, "content") != catN(4, "l4")+"\n"+catN(5, "l5") || r["truncated"] == true {
		t.Fatalf("read to the end = %v", r)
	}
	r = f.call(t, "Read", map[string]any{"file_path": "a.txt", "offset": 9})
	if isErr(r) || str(r, "content") != "" || !strings.Contains(str(r, "note"), "past its end") {
		t.Fatalf("an offset past the end = %v", r)
	}
}

func TestReadDefaultLimitIs2000(t *testing.T) {
	f := setup(t, nil)
	var b strings.Builder
	for i := 1; i <= 2500; i++ {
		fmt.Fprintf(&b, "%d\n", i)
	}
	f.write(t, "big.txt", b.String())
	r := f.call(t, "Read", map[string]any{"file_path": "big.txt"})
	lines := strings.Split(str(r, "content"), "\n")
	if len(lines) != 2000 || num(r, "next_offset") != 2001 || num(r, "total_lines") != 2500 {
		t.Fatalf("got %d lines, next_offset %d, total %d", len(lines), num(r, "next_offset"), num(r, "total_lines"))
	}
	if r := f.call(t, "Read", map[string]any{"file_path": "big.txt", "limit": 2500}); len(strings.Split(str(r, "content"), "\n")) != 2500 {
		t.Fatalf("a limit over 2000 is honoured: %v", r["next_offset"])
	}
}

func TestReadCutsLongLinesAndHonorsTheCharacterBudget(t *testing.T) {
	f := setup(t, nil)
	f.write(t, "long.txt", strings.Repeat("x", 5000)+"\nshort\n")
	r := f.call(t, "Read", map[string]any{"file_path": "long.txt"})
	first := strings.SplitN(str(r, "content"), "\n", 2)[0]
	if !strings.HasSuffix(first, "... [truncated]") || len(first) > 2100 {
		t.Fatalf("long line came back as %d chars: %q...", len(first), first[:40])
	}

	var b strings.Builder
	for i := 0; i < 80; i++ {
		b.WriteString(strings.Repeat("y", 1900) + "\n")
	}
	f.write(t, "wide.txt", b.String())
	r = f.call(t, "Read", map[string]any{"file_path": "wide.txt"})
	content := str(r, "content")
	if len(content) > 100_000 || r["truncated"] != true || num(r, "next_offset") < 2 {
		t.Fatalf("budget: %d chars, truncated %v, next_offset %d", len(content), r["truncated"], num(r, "next_offset"))
	}
	if !strings.HasPrefix(content[strings.LastIndex(content, "\n")+1:], catN(num(r, "next_offset")-1, "")) {
		t.Fatal("a budget cut must stop at a whole line")
	}
}

func TestReadRefusals(t *testing.T) {
	f := setup(t, nil)
	f.write(t, "bin.dat", "abc\x00def")
	if r := f.call(t, "Read", map[string]any{"file_path": "bin.dat"}); !strings.Contains(strings.ToLower(str(r, "error")), "binary") {
		t.Errorf("binary file: %v", r)
	}
	if err := os.MkdirAll(filepath.Join(f.cwd, "dir"), 0o755); err != nil {
		t.Fatal(err)
	}
	if r := f.call(t, "Read", map[string]any{"file_path": "dir"}); !isErr(r) {
		t.Errorf("directory: %v", r)
	}
	f.write(t, "notes.txt", "x\n")
	r := f.call(t, "Read", map[string]any{"file_path": "note.txt"})
	similar, _ := json.Marshal(r["similar_files"])
	if !isErr(r) || !strings.Contains(string(similar), "notes.txt") {
		t.Errorf("missing file with a near name: %v", r)
	}
	f.write(t, ".env", "SECRET=hunter2\n")
	r = f.call(t, "Read", map[string]any{"file_path": ".env"})
	if !isErr(r) || strings.Contains(fmt.Sprint(r), "hunter2") {
		t.Errorf(".env must be refused without leaking it: %v", r)
	}
	f.write(t, "empty.txt", "")
	if r := f.call(t, "Read", map[string]any{"file_path": "empty.txt"}); isErr(r) || str(r, "content") != "" || num(r, "total_lines") != 0 ||
		!strings.Contains(str(r, "note"), "exists but is empty") {
		t.Errorf("empty file: %v", r)
	}
}

// The binary sniff reads a window of the file's start; a text file whose
// window ends inside a character is still text. On 2026-11-07 a notes file
// with an en-dash across byte 8192 read as binary.
func TestReadTextWhoseSniffWindowEndsInsideACharacter(t *testing.T) {
	f := setup(t, nil)
	// lead bytes of short lines, so Read's line cut keeps the tail.
	pad := func(lead int) string {
		return strings.Repeat(strings.Repeat("a", 99)+"\n", lead/100) + strings.Repeat("a", lead%100)
	}
	for _, lead := range []int{8189, 8190, 8191} {
		name := fmt.Sprintf("dash%d.md", lead)
		f.write(t, name, pad(lead)+"– and \U0001F431 after\nline two\n")
		r := f.call(t, "Read", map[string]any{"file_path": name})
		if isErr(r) || !strings.Contains(str(r, "content"), "– and \U0001F431 after") || num(r, "total_lines") != lead/100+2 {
			t.Errorf("%d bytes before the dash: %v", lead, r["error"])
		}
	}
	for _, lead := range []int{8189, 8190, 8191} {
		name := fmt.Sprintf("cat%d.md", lead)
		f.write(t, name, pad(lead)+"\U0001F431\n")
		if r := f.call(t, "Read", map[string]any{"file_path": name}); isErr(r) {
			t.Errorf("%d bytes before a 4-byte character: %v", lead, str(r, "error"))
		}
	}
	for name, tail := range map[string]string{"ff.dat": "\xff", "cont.dat": "\x80", "lead.dat": "\xc0"} {
		f.write(t, name, strings.Repeat("a", 8191)+tail+"rest\n")
		if r := f.call(t, "Read", map[string]any{"file_path": name}); !strings.Contains(strings.ToLower(str(r, "error")), "binary") {
			t.Errorf("%s: a byte that is not UTF-8 at the window's edge must still be binary: %v", name, r)
		}
	}
}

func TestReadResolvesRelativeToTheSessionAndTilde(t *testing.T) {
	f := setup(t, nil)
	if err := os.WriteFile(filepath.Join(f.home, "top.txt"), []byte("home file\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if r := f.call(t, "Read", map[string]any{"file_path": "~/top.txt"}); str(r, "content") != catN(1, "home file") {
		t.Fatalf("~ path: %v", r)
	}
	f.write(t, "sub/x.txt", "in sub\n")
	f.call(t, "Bash", map[string]any{"command": "cd sub"})
	if r := f.call(t, "Read", map[string]any{"file_path": "x.txt"}); str(r, "content") != catN(1, "in sub") {
		t.Fatalf("a relative path must follow the session's cwd after cd: %v", r)
	}
}

// ---- Write ----

func TestWriteCreatesANewFileWithoutARead(t *testing.T) {
	f := setup(t, nil)
	r := f.call(t, "Write", map[string]any{"file_path": "a/b/c.txt", "content": "hello\n"})
	p := filepath.Join(f.cwd, "a", "b", "c.txt")
	if isErr(r) || read(t, p) != "hello\n" || num(r, "bytes_written") != 6 || r["verified"] != true || r["dirs_created"] != true {
		t.Fatalf("write = %v", r)
	}
	if str(r, "resolved_path") != p {
		t.Fatalf("resolved_path = %q, want %q", str(r, "resolved_path"), p)
	}
	// The session wrote it, so it knows all of it: no read is needed to
	// write it again.
	r = f.call(t, "Write", map[string]any{"file_path": "a/b/c.txt", "content": "bye\n"})
	if isErr(r) || read(t, p) != "bye\n" || r["dirs_created"] == true {
		t.Fatalf("overwrite of its own file = %v", r)
	}
}

func TestWriteOverAFileNeedsAWholeReadFirst(t *testing.T) {
	f := setup(t, nil)
	p := f.write(t, "s.txt", "v1\n")
	r := f.call(t, "Write", map[string]any{"file_path": "s.txt", "content": "v2\n"})
	if !strings.Contains(str(r, "error"), "has not been read") || read(t, p) != "v1\n" {
		t.Fatalf("a write over an unread file = %v; file %q", r, read(t, p))
	}
	f.call(t, "Read", map[string]any{"file_path": "s.txt"})
	if r := f.call(t, "Write", map[string]any{"file_path": "s.txt", "content": "v2\n"}); isErr(r) || read(t, p) != "v2\n" {
		t.Fatalf("a write after a read = %v", r)
	}

	// A read of part of a file does not let Write replace all of it; reading
	// the rest, in any order, does.
	q := f.write(t, "long.txt", "1\n2\n3\n4\n5\n6\n")
	f.call(t, "Read", map[string]any{"file_path": "long.txt", "offset": 4})
	r = f.call(t, "Write", map[string]any{"file_path": "long.txt", "content": "new\n"})
	if !strings.Contains(str(r, "error"), "only in part (lines 4-6 of its 6 lines)") || read(t, q) != "1\n2\n3\n4\n5\n6\n" {
		t.Fatalf("a write after a partial read = %v", r)
	}
	f.call(t, "Read", map[string]any{"file_path": "long.txt", "limit": 3})
	if r := f.call(t, "Write", map[string]any{"file_path": "long.txt", "content": "new\n"}); isErr(r) || read(t, q) != "new\n" {
		t.Fatalf("a write after reading every part = %v", r)
	}

	// The head of a file is not all of it either.
	h := f.write(t, "head.txt", "1\n2\n3\n4\n")
	f.call(t, "Read", map[string]any{"file_path": "head.txt", "limit": 2})
	r = f.call(t, "Write", map[string]any{"file_path": "head.txt", "content": "new\n"})
	if !strings.Contains(str(r, "error"), "only in part (lines 1-2 of its 4 lines)") || read(t, h) != "1\n2\n3\n4\n" {
		t.Fatalf("a write after reading the head = %v", r)
	}
}

func TestWriteIsRefusedWhenTheFileChangedSinceTheRead(t *testing.T) {
	f := setup(t, nil)
	p := f.write(t, "s.txt", "v1\n")
	f.call(t, "Read", map[string]any{"file_path": "s.txt"})
	if err := os.WriteFile(p, []byte("changed elsewhere\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	r := f.call(t, "Write", map[string]any{"file_path": "s.txt", "content": "v2\n"})
	if !strings.Contains(str(r, "error"), "changed on disk since") || read(t, p) != "changed elsewhere\n" {
		t.Fatalf("a write over a changed file = %v; file %q", r, read(t, p))
	}
	f.call(t, "Read", map[string]any{"file_path": "s.txt"})
	if r := f.call(t, "Write", map[string]any{"file_path": "s.txt", "content": "v2\n"}); isErr(r) {
		t.Fatalf("a write after reading the change = %v", r)
	}

	// A file touched but holding the same bytes has not changed.
	if err := os.WriteFile(p, []byte("v2\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	later := time.Now().Add(2 * time.Second)
	_ = os.Chtimes(p, later, later)
	if r := f.call(t, "Write", map[string]any{"file_path": "s.txt", "content": "v3\n"}); isErr(r) {
		t.Fatalf("a touched but unchanged file = %v", r)
	}

	// A change of the same size, with the time set back, is still a change.
	fi, _ := os.Stat(p)
	if err := os.WriteFile(p, []byte("v9\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_ = os.Chtimes(p, fi.ModTime(), fi.ModTime())
	if r := f.call(t, "Write", map[string]any{"file_path": "s.txt", "content": "v4\n"}); !strings.Contains(str(r, "error"), "changed on disk since") {
		t.Fatalf("a same-size change = %v", r)
	}
}

func TestWriteKeepsAnExistingMode(t *testing.T) {
	f := setup(t, nil)
	p := f.write(t, "run.sh", "#!/bin/sh\n")
	if err := os.Chmod(p, 0o755); err != nil {
		t.Fatal(err)
	}
	f.call(t, "Read", map[string]any{"file_path": "run.sh"})
	if r := f.call(t, "Write", map[string]any{"file_path": "run.sh", "content": "#!/bin/sh\necho hi\n"}); isErr(r) {
		t.Fatal(r)
	}
	if fi, _ := os.Stat(p); fi.Mode().Perm() != 0o755 {
		t.Fatalf("mode = %v, want 0755 kept", fi.Mode().Perm())
	}
}

func TestWriteRefusesDeniedPaths(t *testing.T) {
	f := setup(t, approval.NewGate(approval.Yolo, nil, nil))
	target := filepath.Join(f.home, ".ssh", "config")
	r := f.call(t, "Write", map[string]any{"file_path": target, "content": "Host x\n"})
	if !isErr(r) {
		t.Fatalf("write to ~/.ssh/config even in yolo: %v", r)
	}
	if _, err := os.Stat(target); !os.IsNotExist(err) {
		t.Fatal("a denied write must not create the file")
	}
}

func TestWriteProtectedFilesNeedApproval(t *testing.T) {
	f := setup(t, approval.NewGate(approval.Yolo, nil, nil))
	if r := f.call(t, "Write", map[string]any{"file_path": "AGENTS.md", "content": "x"}); !isErr(r) {
		t.Fatalf("AGENTS.md with nobody to ask: %v", r)
	}
	if _, err := os.Stat(filepath.Join(f.cwd, "AGENTS.md")); !os.IsNotExist(err) {
		t.Fatal("a refused protected write must not create the file")
	}
	asked := 0
	f.env.Gate = approval.NewGate(approval.Yolo, func(context.Context, approval.Request) (approval.Choice, error) {
		asked++
		return approval.Once, nil
	}, nil)
	if r := f.call(t, "Write", map[string]any{"file_path": "AGENTS.md", "content": "rules\n"}); isErr(r) || asked != 1 {
		t.Fatalf("approved protected write: %v (asked %d)", r, asked)
	}
	// An unread file is refused before anyone is asked to approve the write.
	f.write(t, "CLAUDE.md", "old\n")
	if r := f.call(t, "Write", map[string]any{"file_path": "CLAUDE.md", "content": "new\n"}); !isErr(r) || asked != 1 {
		t.Fatalf("an unread protected file: %v (asked %d)", r, asked)
	}
}

// On 2026-11-10 and 11 a chat model wrote the read tool's display of a notes
// file back as the file, and the file took a layer of line numbers each time.
// Read's display is cat -n's now; the "N|" one it had before is still caught,
// since a resumed session's history shows it.
func TestWriteRefusesReadsOutputPastedBack(t *testing.T) {
	f := setup(t, nil)
	p := f.write(t, "notes.md", "# Notes\n\n- a\n")
	shown := f.call(t, "Read", map[string]any{"file_path": "notes.md"})
	result, _ := json.Marshal(shown)
	old, _ := json.Marshal(map[string]any{"content": "1|# Notes\n2|\n3|- a", "total_lines": 3})
	for name, content := range map[string]string{
		"Read's content":                str(shown, "content") + "\n",
		"Read's whole result":           string(result),
		"a stretch from an offset":      catN(7, "x") + "\n" + catN(8, "y") + "\n" + catN(9, "z"),
		"numbers stacked by repeats":    catN(1, catN(1, "# Notes")) + "\n" + catN(2, catN(2, "")),
		"a number wider than six":       catN(1234567, "a") + "\n" + catN(1234568, "b"),
		"the old display":               "1|# Notes\n2|\n3|- a\n",
		"the old whole result":          string(old),
		"old numbers stacked by repeat": "1|1|# Notes\n2|2|\n3|3|- a",
	} {
		if r := f.call(t, "Write", map[string]any{"file_path": "notes.md", "content": content}); !strings.Contains(str(r, "error"), "Read's display of a file") {
			t.Errorf("%s: write = %v", name, r)
		}
	}
	if read(t, p) != "# Notes\n\n- a\n" {
		t.Fatalf("the file changed: %q", read(t, p))
	}
	for i, content := range []string{
		"| a | b |\n|---|---|\n| 1 | 2 |\n", "1|only one line\n", "1|a\n3|b\n", "01|a\n02|b\n", "0|a\n1|b\n",
		"+1|a\n+2|b\n", `{"content": "1|a"}`,
		"1\talpha\n2\tbeta\n", "id\tname\n1\ta\n2\tb\n", catN(1, "only one line") + "\n", "  1\ta\n  2\tb\n", catN(1, "a") + "\n" + catN(3, "b"),
	} {
		if r := f.call(t, "Write", map[string]any{"file_path": fmt.Sprintf("ok%d.txt", i), "content": content}); isErr(r) {
			t.Errorf("%q was refused: %v", content, r)
		}
	}
}

// On 2026-11-12 the same model put its whole call, path and all, inside content.
func TestWriteSaysWhenTheArgumentsAreInsideContent(t *testing.T) {
	f := setup(t, nil)
	for _, key := range []string{"file_path", "path"} {
		nested, _ := json.Marshal(map[string]any{"content": "hello", key: "x.txt"})
		if r := f.call(t, "Write", map[string]any{"content": string(nested)}); !strings.Contains(str(r, "error"), "pass file_path and content as Write's own arguments") {
			t.Fatalf("%s inside content: write = %v", key, r)
		}
	}
	if r := f.call(t, "Write", map[string]any{"content": "hello"}); str(r, "error") != "file_path is required" {
		t.Fatalf("a plain missing file_path = %v", r)
	}
	if r := f.call(t, "Write", map[string]any{"path": "x.txt", "content": "hello"}); !strings.Contains(str(r, "error"), "file_path, not path") {
		t.Fatalf("the old parameter name = %v", r)
	}
}

func TestWriteRequiresContent(t *testing.T) {
	f := setup(t, nil)
	if r := f.call(t, "Write", map[string]any{"file_path": "x.txt"}); !isErr(r) {
		t.Fatalf("missing content: %v", r)
	}
}

// ---- Edit ----

// readThen reads rel in the session, as Edit and Write need.
func (f *fixture) readThen(t *testing.T, rel string) {
	t.Helper()
	if r := f.call(t, "Read", map[string]any{"file_path": rel}); isErr(r) {
		t.Fatalf("read %s: %v", rel, r)
	}
}

func TestEditReplacesAUniqueMatchAndReturnsADiff(t *testing.T) {
	f := setup(t, nil)
	p := f.write(t, "m.go", "package m\n\nconst A = 1\n")
	f.readThen(t, "m.go")
	r := f.call(t, "Edit", map[string]any{"file_path": "m.go", "old_string": "const A = 1", "new_string": "const A = 2"})
	if r["success"] != true || read(t, p) != "package m\n\nconst A = 2\n" {
		t.Fatalf("edit = %v; file %q", r, read(t, p))
	}
	if d := str(r, "diff"); !strings.Contains(d, "-const A = 1") || !strings.Contains(d, "+const A = 2") {
		t.Fatalf("diff = %q", d)
	}
	// Its own edit leaves the session knowing the file: a second needs no read.
	if r := f.call(t, "Edit", map[string]any{"file_path": "m.go", "old_string": "const A = 2", "new_string": "const A = 3"}); r["success"] != true {
		t.Fatalf("a second edit = %v", r)
	}
}

func TestEditNeedsAReadAndAnUnchangedFile(t *testing.T) {
	f := setup(t, nil)
	p := f.write(t, "m.txt", "one\ntwo\nthree\n")
	r := f.call(t, "Edit", map[string]any{"file_path": "m.txt", "old_string": "two", "new_string": "2"})
	if !strings.Contains(str(r, "error"), "has not been read") || read(t, p) != "one\ntwo\nthree\n" {
		t.Fatalf("an edit of an unread file = %v", r)
	}
	// Part of the file is enough for Edit.
	f.call(t, "Read", map[string]any{"file_path": "m.txt", "offset": 2, "limit": 1})
	if r := f.call(t, "Edit", map[string]any{"file_path": "m.txt", "old_string": "two", "new_string": "2"}); r["success"] != true {
		t.Fatalf("an edit after a partial read = %v", r)
	}
	if err := os.WriteFile(p, []byte("one\n2\nthree\nfour\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	r = f.call(t, "Edit", map[string]any{"file_path": "m.txt", "old_string": "one", "new_string": "1"})
	if !strings.Contains(str(r, "error"), "changed on disk since") || read(t, p) != "one\n2\nthree\nfour\n" {
		t.Fatalf("an edit of a file changed since the read = %v", r)
	}
}

func TestEditRefusesAmbiguityUnlessReplaceAll(t *testing.T) {
	f := setup(t, nil)
	p := f.write(t, "d.txt", "x = 1\ny = 2\nx = 1\n")
	f.readThen(t, "d.txt")
	r := f.call(t, "Edit", map[string]any{"file_path": "d.txt", "old_string": "x = 1", "new_string": "x = 9"})
	if !isErr(r) || !strings.Contains(str(r, "error"), "2") || read(t, p) != "x = 1\ny = 2\nx = 1\n" {
		t.Fatalf("ambiguous edit = %v", r)
	}
	r = f.call(t, "Edit", map[string]any{"file_path": "d.txt", "old_string": "x = 1", "new_string": "x = 9", "replace_all": true})
	if r["success"] != true || read(t, p) != "x = 9\ny = 2\nx = 9\n" {
		t.Fatalf("replace_all = %v; file %q", r, read(t, p))
	}
}

func TestEditToleratesWhitespaceAndReindents(t *testing.T) {
	f := setup(t, nil)
	p := f.write(t, "f.go", "func f() int {\n\treturn 1\n}\n")
	f.readThen(t, "f.go")
	r := f.call(t, "Edit", map[string]any{"file_path": "f.go", "old_string": "    return 1", "new_string": "    return 2"})
	if r["success"] != true || read(t, p) != "func f() int {\n\treturn 2\n}\n" {
		t.Fatalf("indentation-flexible edit = %v; file %q", r, read(t, p))
	}
	q := f.write(t, "g.py", "call(a, b)\n")
	f.readThen(t, "g.py")
	r = f.call(t, "Edit", map[string]any{"file_path": "g.py", "old_string": "call(a,   b)", "new_string": "call(a, c)"})
	if r["success"] != true || read(t, q) != "call(a, c)\n" {
		t.Fatalf("whitespace-normalized edit = %v; file %q", r, read(t, q))
	}
}

func TestEditEdgeCases(t *testing.T) {
	f := setup(t, nil)
	p := f.write(t, "e.txt", "hello world\n")
	f.readThen(t, "e.txt")
	if r := f.call(t, "Edit", map[string]any{"file_path": "e.txt", "old_string": "absent text", "new_string": "x"}); !isErr(r) {
		t.Errorf("no match: %v", r)
	}
	if r := f.call(t, "Edit", map[string]any{"file_path": "e.txt", "old_string": "", "new_string": "x"}); !strings.Contains(str(r, "error"), "already has content") {
		t.Errorf("empty old_string on a file with content: %v", r)
	}
	if r := f.call(t, "Edit", map[string]any{"file_path": "e.txt", "old_string": "hello", "new_string": "hello"}); !isErr(r) {
		t.Errorf("identical strings: %v", r)
	}
	f.call(t, "Edit", map[string]any{"file_path": "e.txt", "old_string": "hello world", "new_string": "goodbye world"})
	r := f.call(t, "Edit", map[string]any{"file_path": "e.txt", "old_string": "hello world", "new_string": "goodbye world"})
	if r["success"] != true || r["no_change"] != true || read(t, p) != "goodbye world\n" {
		t.Errorf("an already-applied edit = %v", r)
	}
	if r := f.call(t, "Edit", map[string]any{"file_path": filepath.Join(f.home, ".ssh", "known_hosts"), "old_string": "a", "new_string": "b"}); !isErr(r) {
		t.Errorf("edit of a denied path: %v", r)
	}
	if r := f.call(t, "Edit", map[string]any{"file_path": "absent.txt", "old_string": "a", "new_string": "b"}); !strings.Contains(str(r, "error"), "No such file") {
		t.Errorf("edit of a missing file: %v", r)
	}
}

func TestEditWithAnEmptyOldStringCreatesAFile(t *testing.T) {
	f := setup(t, nil)
	r := f.call(t, "Edit", map[string]any{"file_path": "new/n.txt", "old_string": "", "new_string": "first\n"})
	if r["success"] != true || read(t, filepath.Join(f.cwd, "new", "n.txt")) != "first\n" || !strings.Contains(str(r, "diff"), "+first") {
		t.Fatalf("create = %v", r)
	}
	q := f.write(t, "blank.txt", "")
	f.readThen(t, "blank.txt")
	if r := f.call(t, "Edit", map[string]any{"file_path": "blank.txt", "old_string": "", "new_string": "filled\n"}); r["success"] != true || read(t, q) != "filled\n" {
		t.Fatalf("filling an empty file = %v", r)
	}
}

// ---- Glob and Grep ----

func searchFixture(t *testing.T) *fixture {
	f := setup(t, nil)
	f.write(t, "a.go", "package a\nfunc Hello() {}\n")
	f.write(t, "b.txt", "hello world\n")
	f.write(t, "c.go", "package c\n")
	f.write(t, "many.txt", "hit 1\nmiss\nhit 2\nhit 3\nhit 4\nhit 5\n")
	f.write(t, "sub/deep/d.go", "package d\n// Hello again\n")
	f.write(t, "sub/e.ts", "let hello = 1\n")
	// Distinct times, oldest first in this order, so newest-first is testable.
	for i, rel := range []string{"many.txt", "b.txt", "c.go", "sub/e.ts", "sub/deep/d.go", "a.go"} {
		at := time.Now().Add(time.Duration(i-10) * time.Minute)
		if err := os.Chtimes(filepath.Join(f.cwd, rel), at, at); err != nil {
			t.Fatal(err)
		}
	}
	return f
}

func paths_(t *testing.T, r map[string]any, key string) []string {
	t.Helper()
	var out []string
	list, _ := r[key].([]any)
	for _, e := range list {
		switch v := e.(type) {
		case string:
			out = append(out, filepath.Base(v))
		case map[string]any:
			out = append(out, filepath.Base(str(v, "path")))
		}
	}
	return out
}

func TestGlobMatchesPathsNewestFirst(t *testing.T) {
	f := searchFixture(t)
	for pattern, want := range map[string]string{
		"*.go":            "a.go,d.go,c.go", // no slash: any depth, newest first
		"**/*.go":         "a.go,d.go,c.go",
		"sub/**/*.go":     "d.go",
		"sub/*.ts":        "e.ts",
		"/*.go":           "a.go,c.go", // a leading slash anchors at the root
		"*.{ts,txt}":      "e.ts,b.txt,many.txt",
		"sub/**":          "d.go,e.ts",
		"nothing-here.sh": "",
	} {
		r := f.call(t, "Glob", map[string]any{"pattern": pattern})
		if got := strings.Join(paths_(t, r, "files"), ","); isErr(r) || got != want {
			t.Errorf("Glob %q = %q, want %q (%v)", pattern, got, want, r)
		}
	}
	if r := f.call(t, "Glob", map[string]any{"pattern": "*.go", "path": "sub"}); strings.Join(paths_(t, r, "files"), ",") != "d.go" {
		t.Errorf("Glob in path: %v", r)
	}
	abs := filepath.Join(f.cwd, "sub") + "/**/*.go"
	if r := f.call(t, "Glob", map[string]any{"pattern": abs}); strings.Join(paths_(t, r, "files"), ",") != "d.go" {
		t.Errorf("an absolute pattern: %v", r)
	}
	if r := f.call(t, "Glob", map[string]any{"pattern": "*.txt", "path": "b.txt"}); strings.Join(paths_(t, r, "files"), ",") != "b.txt" {
		t.Errorf("a path naming a matching file: %v", r)
	}
	if r := f.call(t, "Glob", map[string]any{"pattern": "*.go", "path": "b.txt"}); isErr(r) || num(r, "total_count") != 0 {
		t.Errorf("a path naming a file the pattern misses: %v", r)
	}
	if r := f.call(t, "Glob", map[string]any{"pattern": "x", "path": "no/such/dir"}); !isErr(r) || !strings.Contains(str(r, "error"), "no/such/dir") {
		t.Errorf("missing path: %v", r)
	}
}

func TestGrepListsMatchingFilesNewestFirstByDefault(t *testing.T) {
	f := searchFixture(t)
	r := f.call(t, "Grep", map[string]any{"pattern": "Hello"})
	if got := strings.Join(paths_(t, r, "files"), ","); isErr(r) || got != "a.go,d.go" || num(r, "total_count") != 2 {
		t.Fatalf("files_with_matches = %v", r)
	}
	r = f.call(t, "Grep", map[string]any{"pattern": "hello", "-i": true})
	if got := strings.Join(paths_(t, r, "files"), ","); got != "a.go,d.go,e.ts,b.txt" {
		t.Fatalf("-i = %v", r)
	}
	r = f.call(t, "Grep", map[string]any{"pattern": "hello", "-i": true, "glob": "*.txt"})
	if got := strings.Join(paths_(t, r, "files"), ","); got != "b.txt" {
		t.Fatalf("glob = %v", r)
	}
	r = f.call(t, "Grep", map[string]any{"pattern": "hello", "-i": true, "glob": "sub/**/*.go"})
	if got := strings.Join(paths_(t, r, "files"), ","); got != "d.go" {
		t.Fatalf("a path glob = %v", r)
	}
	r = f.call(t, "Grep", map[string]any{"pattern": "hello", "-i": true, "glob": "!sub/**"})
	if got := strings.Join(paths_(t, r, "files"), ","); got != "a.go,b.txt" {
		t.Fatalf("a negated path glob = %v", r)
	}
	r = f.call(t, "Grep", map[string]any{"pattern": "hello", "-i": true, "glob": "!*.go"})
	if got := strings.Join(paths_(t, r, "files"), ","); got != "e.ts,b.txt" {
		t.Fatalf("a negated glob = %v", r)
	}
	r = f.call(t, "Grep", map[string]any{"pattern": "package", "type": "go"})
	if got := strings.Join(paths_(t, r, "files"), ","); got != "a.go,d.go,c.go" {
		t.Fatalf("type = %v", r)
	}
	r = f.call(t, "Grep", map[string]any{"pattern": "hello", "-i": true, "head_limit": 2})
	if got := paths_(t, r, "files"); len(got) != 2 || r["truncated"] != true || num(r, "total_count") != 4 {
		t.Fatalf("head_limit = %v", r)
	}
	if r := f.call(t, "Grep", map[string]any{"pattern": "nothing matches this"}); isErr(r) || num(r, "total_count") != 0 {
		t.Fatalf("no match = %v", r)
	}
	if r := f.call(t, "Grep", map[string]any{"pattern": "x", "output_mode": "files_only"}); !isErr(r) {
		t.Fatalf("an output_mode that is not one: %v", r)
	}
}

func TestGrepContentAndContext(t *testing.T) {
	f := searchFixture(t)
	mt := filepath.Join(f.cwd, "many.txt")
	r := f.call(t, "Grep", map[string]any{"pattern": "hit [12]", "output_mode": "content"})
	if c := str(r, "content"); isErr(r) || c != mt+":1:hit 1\n"+mt+":3:hit 2" || num(r, "matches") != 2 {
		t.Fatalf("content = %v", r)
	}
	r = f.call(t, "Grep", map[string]any{"pattern": "hit 1", "output_mode": "content", "path": "many.txt", "-A": 1})
	if c := str(r, "content"); c != "1:hit 1\n2-miss" {
		t.Fatalf("-A in one file = %q", c)
	}
	r = f.call(t, "Grep", map[string]any{"pattern": "hit [25]", "output_mode": "content", "path": "many.txt", "-B": 1, "-n": false})
	if c := str(r, "content"); c != "miss\nhit 2\n--\nhit 4\nhit 5" {
		t.Fatalf("-B without numbers = %q", c)
	}
	r = f.call(t, "Grep", map[string]any{"pattern": "miss", "output_mode": "content", "path": "many.txt", "-C": 1})
	if c := str(r, "content"); c != "1-hit 1\n2:miss\n3-hit 2" {
		t.Fatalf("-C = %q", c)
	}
	r = f.call(t, "Grep", map[string]any{"pattern": "hit", "output_mode": "content", "path": "many.txt", "head_limit": 2})
	if c := str(r, "content"); c != "1:hit 1\n3:hit 2" || r["truncated"] != true || num(r, "matches") != 5 {
		t.Fatalf("head_limit = %v", r)
	}
	r = f.call(t, "Grep", map[string]any{"pattern": `1\nmiss`, "output_mode": "content", "path": "many.txt", "multiline": true})
	if c := str(r, "content"); c != "1:hit 1\n2:miss" {
		t.Fatalf("multiline = %q (%v)", c, r)
	}
	r = f.call(t, "Grep", map[string]any{"pattern": "hit", "output_mode": "count"})
	counts, _ := r["counts"].([]any)
	if len(counts) != 1 || num(counts[0].(map[string]any), "count") != 5 || num(r, "total_matches") != 5 {
		t.Fatalf("count = %v", r)
	}
}

func sorted(s []string) []string {
	out := append([]string(nil), s...)
	for i := range out {
		for j := i + 1; j < len(out); j++ {
			if out[j] < out[i] {
				out[i], out[j] = out[j], out[i]
			}
		}
	}
	return out
}

// ---- Bash ----

func TestBashOutputAndExitCode(t *testing.T) {
	f := setup(t, nil)
	if r := f.call(t, "Bash", map[string]any{"command": "echo hi", "description": "Say hi"}); str(r, "output") != "hi" || num(r, "exit_code") != 0 {
		t.Fatalf("echo: %v", r)
	}
	if r := f.call(t, "Bash", map[string]any{"command": "exit 3"}); num(r, "exit_code") != 3 {
		t.Fatalf("exit 3: %v", r)
	}
	if r := f.call(t, "Bash", map[string]any{"command": "echo oops 1>&2"}); !strings.Contains(str(r, "output"), "oops") {
		t.Fatalf("stderr must be merged into output: %v", r)
	}
}

func TestBashCwdAndExportsPersist(t *testing.T) {
	f := setup(t, nil)
	if err := os.MkdirAll(filepath.Join(f.cwd, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	r := f.call(t, "Bash", map[string]any{"command": "cd sub"})
	sub := filepath.Join(f.cwd, "sub")
	if str(r, "cwd") != sub || f.env.State.Cwd() != sub {
		t.Fatalf("cd: result %v, state cwd %q", r, f.env.State.Cwd())
	}
	if r := f.call(t, "Bash", map[string]any{"command": "pwd"}); str(r, "output") != sub {
		t.Fatalf("pwd after cd: %v", r)
	}
	f.call(t, "Bash", map[string]any{"command": "export SOA_T=kept; NOT_EXPORTED=1"})
	if r := f.call(t, "Bash", map[string]any{"command": `echo "[$SOA_T][$NOT_EXPORTED]"`}); str(r, "output") != "[kept][]" {
		t.Fatalf("exports must persist, plain variables must not: %v", r)
	}
}

func TestBashTimeoutsAreMilliseconds(t *testing.T) {
	f := setup(t, nil)
	start := time.Now()
	r := f.call(t, "Bash", map[string]any{"command": "sleep 30", "timeout": 500})
	if num(r, "exit_code") != 124 || !strings.Contains(str(r, "output"), "timed out after 500ms") || time.Since(start) > 10*time.Second {
		t.Fatalf("timeout: %v after %v", r, time.Since(start))
	}
	if r := f.call(t, "Bash", map[string]any{"command": "sleep 0.2; echo done", "timeout": 5000}); str(r, "output") != "done" {
		t.Fatalf("a command within its timeout: %v", r)
	}
	if r := f.call(t, "Bash", map[string]any{"command": "true", "timeout": 0}); !isErr(r) {
		t.Fatalf("timeout 0: %v", r)
	}
	if r := f.call(t, "Bash", map[string]any{"command": "true", "timeout": 600_001}); !strings.Contains(str(r, "error"), "run_in_background") {
		t.Fatalf("a timeout over 600000 ms must point at run_in_background: %v", r)
	}
}

func TestBashInterrupt(t *testing.T) {
	f := setup(t, nil)
	c, cancel := context.WithCancel(ctx)
	time.AfterFunc(200*time.Millisecond, cancel)
	start := time.Now()
	r := f.callCtx(t, c, "Bash", map[string]any{"command": "sleep 30"})
	if num(r, "exit_code") != 130 || !strings.Contains(str(r, "output"), "interrupted") || time.Since(start) > 10*time.Second {
		t.Fatalf("interrupt: %v after %v", r, time.Since(start))
	}
}

func TestBashTruncatesHugeOutputKeepingHeadAndTail(t *testing.T) {
	f := setup(t, nil)
	r := f.call(t, "Bash", map[string]any{"command": `printf 'START'; head -c 200000 /dev/zero | tr '\0' a; printf 'END'`})
	out := str(r, "output")
	if len(out) > 52_000 || !strings.Contains(out, "output cut") || !strings.HasPrefix(out, "START") || !strings.HasSuffix(out, "END") {
		t.Fatalf("truncated output: %d chars, starts %q, ends %q", len(out), out[:min(10, len(out))], out[max(0, len(out)-10):])
	}
}

func TestBashStdinIsEmpty(t *testing.T) {
	f := setup(t, nil)
	start := time.Now()
	if r := f.call(t, "Bash", map[string]any{"command": `read x; echo "[$x]"`, "timeout": 10_000}); str(r, "output") != "[]" || time.Since(start) > 5*time.Second {
		t.Fatalf("stdin: %v", r)
	}
}

func TestBashRefusesBackgroundingInTheForeground(t *testing.T) {
	f := setup(t, nil)
	for _, cmd := range []string{"sleep 100 &", "nohup sleep 1", "setsid sleep 1"} {
		if r := f.call(t, "Bash", map[string]any{"command": cmd}); !strings.Contains(str(r, "error"), "run_in_background") {
			t.Errorf("%q: %v", cmd, r)
		}
	}
}

func TestBashGoesThroughTheGate(t *testing.T) {
	f := setup(t, nil) // default mode, nobody to ask
	victim := filepath.Join(f.cwd, "victim")
	if err := os.MkdirAll(filepath.Join(victim, "inner"), 0o755); err != nil {
		t.Fatal(err)
	}
	r := f.call(t, "Bash", map[string]any{"command": "rm -r victim"})
	if !strings.Contains(str(r, "error"), "BLOCKED") {
		t.Fatalf("a dangerous command with nobody to ask: %v", r)
	}
	if _, err := os.Stat(victim); err != nil {
		t.Fatal("a blocked command must not run")
	}
	var asked []approval.Request
	f.env.Gate = approval.NewGate(approval.Default, func(_ context.Context, req approval.Request) (approval.Choice, error) {
		asked = append(asked, req)
		return approval.Once, nil
	}, nil)
	f.call(t, "Bash", map[string]any{"command": "rm -r victim"})
	if len(asked) != 1 || asked[0].Subject != "rm -r victim" {
		t.Fatalf("approver asked %+v", asked)
	}
	if _, err := os.Stat(victim); !os.IsNotExist(err) {
		t.Fatal("an approved command must run")
	}
}

func TestBashDoesNotWaitForAGrandchildHoldingThePipe(t *testing.T) {
	f := setup(t, nil)
	start := time.Now()
	r := f.call(t, "Bash", map[string]any{"command": "bash -c 'sleep 30 &'; echo done", "timeout": 60_000})
	if !strings.Contains(str(r, "output"), "done") || time.Since(start) > 10*time.Second {
		t.Fatalf("a leftover child holding stdout must not hang the call: %v after %v", r, time.Since(start))
	}
}

func TestCommandsRunWithTheCommandHome(t *testing.T) {
	f := setup(t, approval.NewGate(approval.Yolo, nil, nil))
	if r := f.call(t, "Bash", map[string]any{"command": "echo \"home=$HOME\""}); !strings.Contains(str(r, "output"), "home="+os.Getenv("HOME")) {
		t.Fatalf("without CommandHome HOME is the daemon's: %v", r)
	}
	// A session's CommandHome is fixed for its life (its environment
	// snapshot keeps HOME from the first command), so a fresh one.
	g := setup(t, approval.NewGate(approval.Yolo, nil, nil))
	g.env.CommandHome = g.cwd
	if r := g.call(t, "Bash", map[string]any{"command": "echo \"home=$HOME\""}); !strings.Contains(str(r, "output"), "home="+g.cwd) {
		t.Fatalf("with CommandHome: %v", r)
	}
	if r := g.call(t, "Bash", map[string]any{"command": "cd /tmp && echo \"home=$HOME\""}); !strings.Contains(str(r, "output"), "home="+g.cwd) {
		t.Fatalf("HOME followed a cd (it stays the working directory): %v", r)
	}
}
