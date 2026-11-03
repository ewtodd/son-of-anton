package tools_test

// Contract tests for the five phase 1a tools. Every test runs in temp
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

func TestBuiltinToolsAndSchemas(t *testing.T) {
	reg := tools.NewRegistry(tools.Builtin()...)
	var names []string
	for _, s := range reg.Schemas() {
		if s.Type != "function" {
			t.Errorf("%s: type %q", s.Function.Name, s.Type)
		}
		var params map[string]any
		if err := json.Unmarshal(s.Function.Parameters, &params); err != nil || params["type"] != "object" {
			t.Errorf("%s: parameters must be a JSON object schema: %s", s.Function.Name, s.Function.Parameters)
		}
		if s.Function.Description == "" {
			t.Errorf("%s: no description", s.Function.Name)
		}
		props, _ := params["properties"].(map[string]any)
		for _, dead := range []string{"cross_profile", "pty", "watch_patterns"} {
			if _, present := props[dead]; present {
				t.Errorf("%s still offers the dropped %q parameter", s.Function.Name, dead)
			}
		}
		names = append(names, s.Function.Name)
	}
	// The five phase 1a tools, in this relative order (later phases add more).
	last := -1
	for _, want := range []string{"terminal", "read_file", "write_file", "patch", "search_files"} {
		i := strings.Index(","+strings.Join(names, ",")+",", ","+want+",")
		if i <= last {
			t.Fatalf("builtin tools = %v; missing or out of order: %s", names, want)
		}
		last = i
	}
	for _, n := range names {
		if tool, ok := reg.Lookup(n); !ok || tool.Schema().Function.Name != n {
			t.Errorf("Lookup(%q) failed", n)
		}
	}
	if _, ok := reg.Lookup("delegate_task"); ok {
		t.Error("Lookup of an unknown tool must fail")
	}
	a, _ := json.Marshal(reg.Schemas())
	b, _ := json.Marshal(tools.NewRegistry(tools.Builtin()...).Schemas())
	if string(a) != string(b) {
		t.Fatal("schemas must serialize to the same bytes every time")
	}
}

// ---- read_file ----

func TestReadFileFormat(t *testing.T) {
	f := setup(t, nil)
	f.write(t, "a.txt", "alpha\nbeta\ngamma\n")
	r := f.call(t, "read_file", map[string]any{"path": "a.txt"})
	if str(r, "content") != "1|alpha\n2|beta\n3|gamma" || num(r, "total_lines") != 3 || r["truncated"] == true {
		t.Fatalf("read = %v", r)
	}
}

func TestReadFileOffsetLimitAndContinuation(t *testing.T) {
	f := setup(t, nil)
	f.write(t, "a.txt", "l1\nl2\nl3\nl4\nl5\n")
	r := f.call(t, "read_file", map[string]any{"path": "a.txt", "offset": 2, "limit": 2})
	if str(r, "content") != "2|l2\n3|l3" || r["truncated"] != true || num(r, "next_offset") != 4 {
		t.Fatalf("read = %v", r)
	}
	r = f.call(t, "read_file", map[string]any{"path": "a.txt", "offset": 4})
	if str(r, "content") != "4|l4\n5|l5" || r["truncated"] == true {
		t.Fatalf("read to the end = %v", r)
	}
}

func TestReadFileDefaultLimitIs2000(t *testing.T) {
	f := setup(t, nil)
	var b strings.Builder
	for i := 1; i <= 2500; i++ {
		fmt.Fprintf(&b, "%d\n", i)
	}
	f.write(t, "big.txt", b.String())
	r := f.call(t, "read_file", map[string]any{"path": "big.txt"})
	lines := strings.Split(str(r, "content"), "\n")
	if len(lines) != 2000 || num(r, "next_offset") != 2001 || num(r, "total_lines") != 2500 {
		t.Fatalf("got %d lines, next_offset %d, total %d", len(lines), num(r, "next_offset"), num(r, "total_lines"))
	}
}

func TestReadFileCutsLongLinesAndHonorsTheCharacterBudget(t *testing.T) {
	f := setup(t, nil)
	f.write(t, "long.txt", strings.Repeat("x", 5000)+"\nshort\n")
	r := f.call(t, "read_file", map[string]any{"path": "long.txt"})
	first := strings.SplitN(str(r, "content"), "\n", 2)[0]
	if !strings.HasSuffix(first, "... [truncated]") || len(first) > 2100 {
		t.Fatalf("long line came back as %d chars: %q...", len(first), first[:40])
	}

	var b strings.Builder
	for i := 0; i < 80; i++ {
		b.WriteString(strings.Repeat("y", 1900) + "\n")
	}
	f.write(t, "wide.txt", b.String())
	r = f.call(t, "read_file", map[string]any{"path": "wide.txt"})
	content := str(r, "content")
	if len(content) > 100_000 || r["truncated"] != true || num(r, "next_offset") < 2 {
		t.Fatalf("budget: %d chars, truncated %v, next_offset %d", len(content), r["truncated"], num(r, "next_offset"))
	}
	if !strings.HasPrefix(content[strings.LastIndex(content, "\n")+1:], fmt.Sprintf("%d|", num(r, "next_offset")-1)) {
		t.Fatal("a budget cut must stop at a whole line")
	}
}

func TestReadFileRefusals(t *testing.T) {
	f := setup(t, nil)
	f.write(t, "bin.dat", "abc\x00def")
	if r := f.call(t, "read_file", map[string]any{"path": "bin.dat"}); !strings.Contains(strings.ToLower(str(r, "error")), "binary") {
		t.Errorf("binary file: %v", r)
	}
	if err := os.MkdirAll(filepath.Join(f.cwd, "dir"), 0o755); err != nil {
		t.Fatal(err)
	}
	if r := f.call(t, "read_file", map[string]any{"path": "dir"}); !isErr(r) {
		t.Errorf("directory: %v", r)
	}
	f.write(t, "notes.txt", "x\n")
	r := f.call(t, "read_file", map[string]any{"path": "note.txt"})
	similar, _ := json.Marshal(r["similar_files"])
	if !isErr(r) || !strings.Contains(string(similar), "notes.txt") {
		t.Errorf("missing file with a near name: %v", r)
	}
	f.write(t, ".env", "SECRET=hunter2\n")
	r = f.call(t, "read_file", map[string]any{"path": ".env"})
	if !isErr(r) || strings.Contains(fmt.Sprint(r), "hunter2") {
		t.Errorf(".env must be refused without leaking it: %v", r)
	}
	f.write(t, "empty.txt", "")
	if r := f.call(t, "read_file", map[string]any{"path": "empty.txt"}); isErr(r) || str(r, "content") != "" || num(r, "total_lines") != 0 {
		t.Errorf("empty file: %v", r)
	}
}

func TestReadFileResolvesRelativeToTheSessionAndTilde(t *testing.T) {
	f := setup(t, nil)
	if err := os.WriteFile(filepath.Join(f.home, "top.txt"), []byte("home file\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if r := f.call(t, "read_file", map[string]any{"path": "~/top.txt"}); str(r, "content") != "1|home file" {
		t.Fatalf("~ path: %v", r)
	}
	f.write(t, "sub/x.txt", "in sub\n")
	f.call(t, "terminal", map[string]any{"command": "cd sub"})
	if r := f.call(t, "read_file", map[string]any{"path": "x.txt"}); str(r, "content") != "1|in sub" {
		t.Fatalf("a relative path must follow the session's cwd after cd: %v", r)
	}
}

// ---- write_file ----

func TestWriteFileCreatesParentsAndVerifies(t *testing.T) {
	f := setup(t, nil)
	r := f.call(t, "write_file", map[string]any{"path": "a/b/c.txt", "content": "hello\n"})
	p := filepath.Join(f.cwd, "a", "b", "c.txt")
	if isErr(r) || read(t, p) != "hello\n" || num(r, "bytes_written") != 6 || r["verified"] != true || r["dirs_created"] != true {
		t.Fatalf("write = %v", r)
	}
	if str(r, "resolved_path") != p {
		t.Fatalf("resolved_path = %q, want %q", str(r, "resolved_path"), p)
	}
	r = f.call(t, "write_file", map[string]any{"path": "a/b/c.txt", "content": "bye\n"})
	if read(t, p) != "bye\n" || r["dirs_created"] == true {
		t.Fatalf("overwrite = %v", r)
	}
}

func TestWriteFileKeepsAnExistingMode(t *testing.T) {
	f := setup(t, nil)
	p := f.write(t, "run.sh", "#!/bin/sh\n")
	if err := os.Chmod(p, 0o755); err != nil {
		t.Fatal(err)
	}
	f.call(t, "write_file", map[string]any{"path": "run.sh", "content": "#!/bin/sh\necho hi\n"})
	if fi, _ := os.Stat(p); fi.Mode().Perm() != 0o755 {
		t.Fatalf("mode = %v, want 0755 kept", fi.Mode().Perm())
	}
}

func TestWriteFileRefusesDeniedPaths(t *testing.T) {
	f := setup(t, approval.NewGate(approval.Yolo, nil, nil))
	target := filepath.Join(f.home, ".ssh", "config")
	r := f.call(t, "write_file", map[string]any{"path": target, "content": "Host x\n"})
	if !isErr(r) {
		t.Fatalf("write to ~/.ssh/config even in yolo: %v", r)
	}
	if _, err := os.Stat(target); !os.IsNotExist(err) {
		t.Fatal("a denied write must not create the file")
	}
}

func TestWriteFileProtectedFilesNeedApproval(t *testing.T) {
	f := setup(t, approval.NewGate(approval.Yolo, nil, nil))
	if r := f.call(t, "write_file", map[string]any{"path": "AGENTS.md", "content": "x"}); !isErr(r) {
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
	if r := f.call(t, "write_file", map[string]any{"path": "AGENTS.md", "content": "rules\n"}); isErr(r) || asked != 1 {
		t.Fatalf("approved protected write: %v (asked %d)", r, asked)
	}
}

func TestWriteFileWarnsWhenTheFileChangedSinceItWasRead(t *testing.T) {
	f := setup(t, nil)
	p := f.write(t, "s.txt", "v1\n")
	f.call(t, "read_file", map[string]any{"path": "s.txt"})
	if err := os.WriteFile(p, []byte("changed elsewhere\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	later := time.Now().Add(2 * time.Second)
	_ = os.Chtimes(p, later, later)
	r := f.call(t, "write_file", map[string]any{"path": "s.txt", "content": "v2\n"})
	if isErr(r) || str(r, "_warning") == "" {
		t.Fatalf("stale write = %v, want a _warning (writes are not blocked)", r)
	}
	if read(t, p) != "v2\n" {
		t.Fatal("a stale write still writes")
	}
}

func TestWriteFileRequiresContent(t *testing.T) {
	f := setup(t, nil)
	if r := f.call(t, "write_file", map[string]any{"path": "x.txt"}); !isErr(r) {
		t.Fatalf("missing content: %v", r)
	}
}

// ---- patch ----

func TestPatchReplacesAUniqueMatchAndReturnsADiff(t *testing.T) {
	f := setup(t, nil)
	p := f.write(t, "m.go", "package m\n\nconst A = 1\n")
	r := f.call(t, "patch", map[string]any{"path": "m.go", "old_string": "const A = 1", "new_string": "const A = 2"})
	if r["success"] != true || read(t, p) != "package m\n\nconst A = 2\n" {
		t.Fatalf("patch = %v; file %q", r, read(t, p))
	}
	if d := str(r, "diff"); !strings.Contains(d, "-const A = 1") || !strings.Contains(d, "+const A = 2") {
		t.Fatalf("diff = %q", d)
	}
}

func TestPatchRefusesAmbiguityUnlessReplaceAll(t *testing.T) {
	f := setup(t, nil)
	p := f.write(t, "d.txt", "x = 1\ny = 2\nx = 1\n")
	r := f.call(t, "patch", map[string]any{"path": "d.txt", "old_string": "x = 1", "new_string": "x = 9"})
	if !isErr(r) || !strings.Contains(str(r, "error"), "2") || read(t, p) != "x = 1\ny = 2\nx = 1\n" {
		t.Fatalf("ambiguous patch = %v", r)
	}
	r = f.call(t, "patch", map[string]any{"path": "d.txt", "old_string": "x = 1", "new_string": "x = 9", "replace_all": true})
	if r["success"] != true || read(t, p) != "x = 9\ny = 2\nx = 9\n" {
		t.Fatalf("replace_all = %v; file %q", r, read(t, p))
	}
}

func TestPatchToleratesWhitespaceAndReindents(t *testing.T) {
	f := setup(t, nil)
	p := f.write(t, "f.go", "func f() int {\n\treturn 1\n}\n")
	r := f.call(t, "patch", map[string]any{"path": "f.go", "old_string": "    return 1", "new_string": "    return 2"})
	if r["success"] != true || read(t, p) != "func f() int {\n\treturn 2\n}\n" {
		t.Fatalf("indentation-flexible patch = %v; file %q", r, read(t, p))
	}
	q := f.write(t, "g.py", "call(a, b)\n")
	r = f.call(t, "patch", map[string]any{"path": "g.py", "old_string": "call(a,   b)", "new_string": "call(a, c)"})
	if r["success"] != true || read(t, q) != "call(a, c)\n" {
		t.Fatalf("whitespace-normalized patch = %v; file %q", r, read(t, q))
	}
}

func TestPatchEdgeCases(t *testing.T) {
	f := setup(t, nil)
	p := f.write(t, "e.txt", "hello world\n")
	if r := f.call(t, "patch", map[string]any{"path": "e.txt", "old_string": "absent text", "new_string": "x"}); !isErr(r) {
		t.Errorf("no match: %v", r)
	}
	if r := f.call(t, "patch", map[string]any{"path": "e.txt", "old_string": "", "new_string": "x"}); !isErr(r) {
		t.Errorf("empty old_string: %v", r)
	}
	if r := f.call(t, "patch", map[string]any{"path": "e.txt", "old_string": "hello", "new_string": "hello"}); !isErr(r) {
		t.Errorf("identical strings: %v", r)
	}
	f.call(t, "patch", map[string]any{"path": "e.txt", "old_string": "hello world", "new_string": "goodbye world"})
	r := f.call(t, "patch", map[string]any{"path": "e.txt", "old_string": "hello world", "new_string": "goodbye world"})
	if r["success"] != true || r["no_change"] != true || read(t, p) != "goodbye world\n" {
		t.Errorf("an already-applied patch = %v", r)
	}
	if r := f.call(t, "patch", map[string]any{"path": filepath.Join(f.home, ".ssh", "known_hosts"), "old_string": "a", "new_string": "b"}); !isErr(r) {
		t.Errorf("patch of a denied path: %v", r)
	}
	if r := f.call(t, "patch", map[string]any{"path": "e.txt", "old_string": "a", "new_string": "b", "mode": "patch"}); !isErr(r) {
		t.Errorf("V4A patch mode is not supported and must say so: %v", r)
	}
}

// ---- search_files ----

func searchFixture(t *testing.T) *fixture {
	f := setup(t, nil)
	f.write(t, "a.go", "package a\nfunc Hello() {}\n")
	f.write(t, "b.txt", "hello world\n")
	f.write(t, "c.go", "package c\n")
	f.write(t, "many.txt", "hit 1\nmiss\nhit 2\nhit 3\nhit 4\nhit 5\n")
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

func TestSearchContent(t *testing.T) {
	f := searchFixture(t)
	r := f.call(t, "search_files", map[string]any{"pattern": "Hello"})
	ms, _ := r["matches"].([]any)
	if isErr(r) || len(ms) != 1 || num(r, "total_count") != 1 {
		t.Fatalf("search = %v", r)
	}
	m := ms[0].(map[string]any)
	if filepath.Base(str(m, "path")) != "a.go" || num(m, "line") != 2 || !strings.Contains(str(m, "content"), "func Hello") {
		t.Fatalf("match = %v", m)
	}
}

func TestSearchGlobAndOutputModes(t *testing.T) {
	f := searchFixture(t)
	r := f.call(t, "search_files", map[string]any{"pattern": "(?i)hello", "file_glob": "*.txt"})
	if got := paths_(t, r, "matches"); len(got) != 1 || got[0] != "b.txt" {
		t.Fatalf("file_glob: %v", r)
	}
	r = f.call(t, "search_files", map[string]any{"pattern": "(?i)hello", "output_mode": "files_only"})
	if got := strings.Join(sorted(paths_(t, r, "files")), ","); got != "a.go,b.txt" {
		t.Fatalf("files_only: %v", r)
	}
	r = f.call(t, "search_files", map[string]any{"pattern": "hit", "output_mode": "count"})
	counts, _ := r["counts"].([]any)
	if len(counts) != 1 || num(counts[0].(map[string]any), "count") != 5 {
		t.Fatalf("count: %v", r)
	}
}

func TestSearchContextLimitAndOffset(t *testing.T) {
	f := searchFixture(t)
	r := f.call(t, "search_files", map[string]any{"pattern": "hit 1", "context": 1})
	ms, _ := r["matches"].([]any)
	foundContext := false
	for _, e := range ms {
		m := e.(map[string]any)
		if m["context"] == true && strings.Contains(str(m, "content"), "miss") {
			foundContext = true
		}
	}
	if !foundContext {
		t.Fatalf("context line missing: %v", r)
	}
	r = f.call(t, "search_files", map[string]any{"pattern": "hit", "limit": 2})
	if ms, _ := r["matches"].([]any); len(ms) != 2 || r["truncated"] != true {
		t.Fatalf("limit: %v", r)
	}
	r = f.call(t, "search_files", map[string]any{"pattern": "hit", "limit": 2, "offset": 2})
	ms, _ = r["matches"].([]any)
	if len(ms) != 2 || !strings.Contains(str(ms[0].(map[string]any), "content"), "hit 3") {
		t.Fatalf("offset: %v", r)
	}
}

func TestSearchFiles(t *testing.T) {
	f := searchFixture(t)
	r := f.call(t, "search_files", map[string]any{"pattern": "*.go", "target": "files"})
	if got := strings.Join(sorted(paths_(t, r, "files")), ","); got != "a.go,c.go" {
		t.Fatalf("files target: %v", r)
	}
	if r := f.call(t, "search_files", map[string]any{"pattern": "x", "path": "no/such/dir"}); !isErr(r) {
		t.Fatalf("missing path: %v", r)
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

// ---- terminal ----

func TestTerminalOutputAndExitCode(t *testing.T) {
	f := setup(t, nil)
	if r := f.call(t, "terminal", map[string]any{"command": "echo hi"}); str(r, "output") != "hi" || num(r, "exit_code") != 0 {
		t.Fatalf("echo: %v", r)
	}
	if r := f.call(t, "terminal", map[string]any{"command": "exit 3"}); num(r, "exit_code") != 3 {
		t.Fatalf("exit 3: %v", r)
	}
	if r := f.call(t, "terminal", map[string]any{"command": "echo oops 1>&2"}); !strings.Contains(str(r, "output"), "oops") {
		t.Fatalf("stderr must be merged into output: %v", r)
	}
}

func TestTerminalCwdAndExportsPersist(t *testing.T) {
	f := setup(t, nil)
	if err := os.MkdirAll(filepath.Join(f.cwd, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	r := f.call(t, "terminal", map[string]any{"command": "cd sub"})
	sub := filepath.Join(f.cwd, "sub")
	if str(r, "cwd") != sub || f.env.State.Cwd() != sub {
		t.Fatalf("cd: result %v, state cwd %q", r, f.env.State.Cwd())
	}
	if r := f.call(t, "terminal", map[string]any{"command": "pwd"}); str(r, "output") != sub {
		t.Fatalf("pwd after cd: %v", r)
	}
	f.call(t, "terminal", map[string]any{"command": "export SOA_T=kept; NOT_EXPORTED=1"})
	if r := f.call(t, "terminal", map[string]any{"command": `echo "[$SOA_T][$NOT_EXPORTED]"`}); str(r, "output") != "[kept][]" {
		t.Fatalf("exports must persist, plain variables must not: %v", r)
	}
}

func TestTerminalWorkdirIsOneOff(t *testing.T) {
	f := setup(t, nil)
	other := t.TempDir()
	if r := f.call(t, "terminal", map[string]any{"command": "pwd", "workdir": other}); str(r, "output") != other {
		t.Fatalf("workdir: %v", r)
	}
	if f.env.State.Cwd() != f.cwd {
		t.Fatalf("workdir must not move the session: cwd %q", f.env.State.Cwd())
	}
	if r := f.call(t, "terminal", map[string]any{"command": "pwd", "workdir": other + "; rm x"}); !isErr(r) {
		t.Fatalf("a workdir with shell metacharacters must be refused: %v", r)
	}
}

func TestTerminalTimeouts(t *testing.T) {
	f := setup(t, nil)
	start := time.Now()
	r := f.call(t, "terminal", map[string]any{"command": "sleep 30", "timeout": 1})
	if num(r, "exit_code") != 124 || !strings.Contains(str(r, "output"), "timed out") || time.Since(start) > 10*time.Second {
		t.Fatalf("timeout: %v after %v", r, time.Since(start))
	}
	if r := f.call(t, "terminal", map[string]any{"command": "true", "timeout": 0}); !isErr(r) {
		t.Fatalf("timeout 0: %v", r)
	}
	if r := f.call(t, "terminal", map[string]any{"command": "true", "timeout": 700}); !strings.Contains(str(r, "error"), "background") {
		t.Fatalf("a foreground timeout over 600 must point at background: %v", r)
	}
}

func TestTerminalInterrupt(t *testing.T) {
	f := setup(t, nil)
	c, cancel := context.WithCancel(ctx)
	time.AfterFunc(200*time.Millisecond, cancel)
	start := time.Now()
	r := f.callCtx(t, c, "terminal", map[string]any{"command": "sleep 30"})
	if num(r, "exit_code") != 130 || !strings.Contains(str(r, "output"), "interrupted") || time.Since(start) > 10*time.Second {
		t.Fatalf("interrupt: %v after %v", r, time.Since(start))
	}
}

func TestTerminalTruncatesHugeOutputKeepingHeadAndTail(t *testing.T) {
	f := setup(t, nil)
	r := f.call(t, "terminal", map[string]any{"command": `printf 'START'; head -c 200000 /dev/zero | tr '\0' a; printf 'END'`})
	out := str(r, "output")
	if len(out) > 52_000 || !strings.Contains(out, "OUTPUT TRUNCATED") || !strings.HasPrefix(out, "START") || !strings.HasSuffix(out, "END") {
		t.Fatalf("truncated output: %d chars, starts %q, ends %q", len(out), out[:min(10, len(out))], out[max(0, len(out)-10):])
	}
}

func TestTerminalStdinIsEmpty(t *testing.T) {
	f := setup(t, nil)
	start := time.Now()
	if r := f.call(t, "terminal", map[string]any{"command": `read x; echo "[$x]"`, "timeout": 10}); str(r, "output") != "[]" || time.Since(start) > 5*time.Second {
		t.Fatalf("stdin: %v", r)
	}
}

func TestTerminalRefusesBackgroundingInTheForeground(t *testing.T) {
	f := setup(t, nil)
	for _, cmd := range []string{"sleep 100 &", "nohup sleep 1", "setsid sleep 1"} {
		if r := f.call(t, "terminal", map[string]any{"command": cmd}); !strings.Contains(str(r, "error"), "background") {
			t.Errorf("%q: %v", cmd, r)
		}
	}
}

func TestTerminalGoesThroughTheGate(t *testing.T) {
	f := setup(t, nil) // default mode, nobody to ask
	victim := filepath.Join(f.cwd, "victim")
	if err := os.MkdirAll(filepath.Join(victim, "inner"), 0o755); err != nil {
		t.Fatal(err)
	}
	r := f.call(t, "terminal", map[string]any{"command": "rm -r victim"})
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
	f.call(t, "terminal", map[string]any{"command": "rm -r victim"})
	if len(asked) != 1 || asked[0].Subject != "rm -r victim" {
		t.Fatalf("approver asked %+v", asked)
	}
	if _, err := os.Stat(victim); !os.IsNotExist(err) {
		t.Fatal("an approved command must run")
	}
}

func TestTerminalDoesNotWaitForAGrandchildHoldingThePipe(t *testing.T) {
	f := setup(t, nil)
	start := time.Now()
	r := f.call(t, "terminal", map[string]any{"command": "bash -c 'sleep 30 &'; echo done", "timeout": 60})
	if !strings.Contains(str(r, "output"), "done") || time.Since(start) > 10*time.Second {
		t.Fatalf("a leftover child holding stdout must not hang the call: %v after %v", r, time.Since(start))
	}
}

func TestCommandsRunWithTheCommandHome(t *testing.T) {
	f := setup(t, approval.NewGate(approval.Yolo, nil, nil))
	if r := f.call(t, "terminal", map[string]any{"command": "echo \"home=$HOME\""}); !strings.Contains(str(r, "output"), "home="+os.Getenv("HOME")) {
		t.Fatalf("without CommandHome HOME is the daemon's: %v", r)
	}
	// A session's CommandHome is fixed for its life (its environment
	// snapshot keeps HOME from the first command), so a fresh one.
	g := setup(t, approval.NewGate(approval.Yolo, nil, nil))
	g.env.CommandHome = g.cwd
	if r := g.call(t, "terminal", map[string]any{"command": "echo \"home=$HOME\""}); !strings.Contains(str(r, "output"), "home="+g.cwd) {
		t.Fatalf("with CommandHome: %v", r)
	}
	if r := g.call(t, "terminal", map[string]any{"command": "cd /tmp && echo \"home=$HOME\""}); !strings.Contains(str(r, "output"), "home="+g.cwd) {
		t.Fatalf("HOME followed a cd (it stays the working directory): %v", r)
	}
}
