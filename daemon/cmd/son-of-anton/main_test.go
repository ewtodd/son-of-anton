package main

// Phase 10a: the son-of-anton command (daemon/phases/10a-son-of-anton-command.md).
// With SON_OF_ANTON_TEST_AS_MAIN=1 this test binary is the command, so it runs
// as a real process and its exec into soa-d (a stand-in that records its
// arguments) happens for real.

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/buildinfo"
	"github.com/ewtodd/son-of-anton/daemon/internal/client"
	"github.com/ewtodd/son-of-anton/daemon/internal/server"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func TestMain(m *testing.M) {
	// The stand-in for soa-d (or the physics python): record the arguments.
	if rec := os.Getenv("SON_OF_ANTON_TEST_RECORD"); rec != "" {
		b, _ := json.Marshal(os.Args[1:])
		_ = os.WriteFile(rec, b, 0o600)
		os.Exit(0)
	}
	if os.Getenv("SON_OF_ANTON_TEST_AS_MAIN") == "1" {
		main()
		os.Exit(0)
	}
	os.Exit(m.Run())
}

// home is a short home (a unix socket path must fit in 107 bytes) with a
// config.toml; extra is appended to it.
func home(t *testing.T, extra string) string {
	t.Helper()
	h, err := os.MkdirTemp("", "soacli")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(h) })
	cfg := "[model]\ndefault = \"test-model\"\nprovider = \"custom\"\n\n[custom_providers.custom]\nbase_url = \"http://127.0.0.1:9/v1\"\n\n" + extra
	if err := os.WriteFile(filepath.Join(h, "config.toml"), []byte(cfg), 0o600); err != nil {
		t.Fatal(err)
	}
	return h
}

// stub is a stand-in program: this test binary, recording its arguments as a
// JSON list in the file it returns.
func stub(t *testing.T) (bin, record string) {
	t.Helper()
	dir := t.TempDir()
	record = filepath.Join(dir, "argv.json")
	bin = filepath.Join(dir, "stand-in")
	script := "#!/bin/sh\nSON_OF_ANTON_TEST_AS_MAIN= SON_OF_ANTON_TEST_RECORD=" + record + " exec " + os.Args[0] + " \"$@\"\n"
	if err := os.WriteFile(bin, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return bin, record
}

type result struct {
	code        int
	out, errOut string
}

// cli runs the command with args, in dir, with env added.
func cli(t *testing.T, dir, stdin string, env []string, args ...string) result {
	t.Helper()
	cmd := exec.Command(os.Args[0], args...)
	cmd.Dir = dir
	cmd.Env = append(append(os.Environ(), "SON_OF_ANTON_TEST_AS_MAIN=1", "SON_OF_ANTON_HOME="), env...)
	cmd.Stdin = strings.NewReader(stdin)
	var out, errOut bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errOut
	err := cmd.Run()
	code := 0
	if ee, ok := err.(*exec.ExitError); ok {
		code = ee.ExitCode()
	} else if err != nil {
		t.Fatal(err)
	}
	return result{code, out.String(), errOut.String()}
}

func argv(t *testing.T, record string) []string {
	t.Helper()
	b, err := os.ReadFile(record)
	if err != nil {
		t.Fatalf("the stand-in was not run: %v", err)
	}
	var got []string
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatalf("record %q: %v", b, err)
	}
	return got
}

func TestItRunsSoaDWithTheRightArguments(t *testing.T) {
	h := home(t, "")
	bin, record := stub(t)
	work := t.TempDir()
	qfile := filepath.Join(t.TempDir(), "q.txt")
	if err := os.WriteFile(qfile, []byte("from a file"), 0o600); err != nil {
		t.Fatal(err)
	}
	env := []string{"SOA_D_BIN=" + bin}
	for _, c := range []struct {
		args []string
		want []string
	}{
		{[]string{"--home", h}, []string{"tui", "--home", h, "--in", work}},
		{[]string{"--home", h, "-c", "-m", "test-model", "--reasoning", "low", "--yolo", "--in", work},
			[]string{"tui", "--home", h, "--model", "test-model", "--reasoning", "low", "--yolo", "--in", work, "-c"}},
		{[]string{"--home=" + h, "--resume", "latest"}, []string{"tui", "--home", h, "--in", work, "--resume", "latest"}},
		{[]string{"--home", h, "chat", "-r", "20261103_010203_abcdef"}, []string{"tui", "--home", h, "--in", work, "--resume", "20261103_010203_abcdef"}},
		{[]string{"--home", h, "what", "is", "up"}, []string{"query", "--home", h, "what is up"}},
		{[]string{"--home", h, "-q", "ask", "--yolo"}, []string{"query", "--home", h, "--yolo", "ask"}},
		{[]string{"--home", h, "--query-file", qfile}, []string{"query", "--home", h, "from a file"}},
		{[]string{"--home", h, "-z", "do it", "-m", "other", "--usage-file", "/tmp/u.json"},
			[]string{"oneshot", "--home", h, "--model", "other", "--usage-file", "/tmp/u.json", "do it"}},
		{[]string{"--home", h, "sessions", "import", "--from-opencode", "/x/opencode.db"},
			[]string{"import", "--from-opencode", "/x/opencode.db", "--home", h}},
	} {
		_ = os.Remove(record)
		if r := cli(t, work, "", env, c.args...); r.code != 0 {
			t.Fatalf("%v: exit %d, %s", c.args, r.code, r.errOut)
		}
		if got := argv(t, record); strings.Join(got, "\x00") != strings.Join(c.want, "\x00") {
			t.Errorf("%v ran soa-d %q, want %q", c.args, got, c.want)
		}
	}
	// stdin for --query-file -
	_ = os.Remove(record)
	if r := cli(t, work, "piped question", env, "--home", h, "--query-file", "-"); r.code != 0 {
		t.Fatalf("--query-file -: %s", r.errOut)
	}
	if got := argv(t, record); got[len(got)-1] != "piped question" {
		t.Fatalf("--query-file - ran %q", got)
	}
}

func TestUsageErrors(t *testing.T) {
	h := home(t, "")
	bin, record := stub(t)
	empty := filepath.Join(t.TempDir(), "empty.txt")
	if err := os.WriteFile(empty, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{
		{"--bogus"},
		{"-m"},
		{"-c", "my session"},
		{"-q", "x", "--in", "/tmp"},
		{"-z", "x", "--resume", "latest"},
		{"-q", "x", "-c"},
		{"--usage-file", "/tmp/u.json"},
		{"-q", "x", "-z", "y"},
		{"--query-file", empty},
		{"frobnicate-subcommand-that-is-a-flag", "--nope"},
	} {
		_ = os.Remove(record)
		r := cli(t, t.TempDir(), "", []string{"SOA_D_BIN=" + bin}, append([]string{"--home", h}, args...)...)
		if r.code != 2 || r.errOut == "" {
			t.Errorf("%v: exit %d, stderr %q", args, r.code, r.errOut)
		}
		if _, err := os.Stat(record); err == nil {
			t.Errorf("%v ran soa-d", args)
		}
	}
	if r := cli(t, t.TempDir(), "", []string{"SOA_D_BIN=" + bin}, "--home", h, "--in", "/no/such/dir"); r.code != 1 || !strings.Contains(r.errOut, "--in directory not found") {
		t.Errorf("--in missing: %d %q", r.code, r.errOut)
	}
	if r := cli(t, t.TempDir(), "", []string{"SOA_D_BIN=", "PATH=/nonexistent"}, "--home", h); r.code != 1 || !strings.Contains(r.errOut, "soa-d is not installed") {
		t.Errorf("no soa-d: %d %q", r.code, r.errOut)
	}
}

func TestVersion(t *testing.T) {
	r := cli(t, t.TempDir(), "", nil, "--version")
	if r.code != 0 || r.out != "son-of-anton "+buildinfo.Version+"\n" {
		t.Fatalf("--version: %d %q", r.code, r.out)
	}
}

// giveTurn writes a user row to the named sessions through the store: only
// sessions with a turn list.
func giveTurn(t *testing.T, h string, ids ...string) {
	t.Helper()
	st, err := store.Open(filepath.Join(h, "soa-d.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	for _, id := range ids {
		if _, err := st.AppendMessages(context.Background(), id, store.Message{Role: "user", Content: "hello"}); err != nil {
			t.Fatal(err)
		}
	}
}

// serve runs a daemon on h in this process until the test ends.
func serve(t *testing.T, h string) {
	t.Helper()
	ln, err := server.Listen(h)
	if err != nil {
		t.Fatal(err)
	}
	srv, err := server.New(server.Options{Home: h, Version: "test"})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- srv.Serve(ctx, ln) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(15 * time.Second):
		}
	})
}

func TestStatus(t *testing.T) {
	h := home(t, "[gateway]\nmodel = \"fiona\"\n\n[daemon]\nautostart = false\ninstance = \"play\"\nsignal = false\ncron = true\n")
	r := cli(t, t.TempDir(), "", nil, "--home", h, "status")
	for _, want := range []string{"home: " + h, "daemon: not running", "served by: the son-of-anton-play unit",
		"signal: off", "cron: on", "models: test-model, chats fiona"} {
		if r.code != 0 || !strings.Contains(r.out, want) {
			t.Fatalf("status lacks %q (exit %d):\n%s%s", want, r.code, r.out, r.errOut)
		}
	}
	if _, err := os.Stat(filepath.Join(h, "run", "soa-d.sock")); err == nil {
		t.Fatal("status started a daemon")
	}

	h2 := home(t, "")
	serve(t, h2)
	c, err := client.Dial(h2, "cli")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	ctx := context.Background()
	ids := [2]string{}
	for i := range ids {
		var cr struct {
			Session struct {
				ID string `json:"id"`
			} `json:"session"`
		}
		if err := c.Call(ctx, "session.create", map[string]any{"cwd": t.TempDir()}, &cr); err != nil {
			t.Fatal(err)
		}
		ids[i] = cr.Session.ID
	}
	// A session that never got a turn does not count: both get one.
	giveTurn(t, h2, ids[0], ids[1])
	r = cli(t, t.TempDir(), "", nil, "--home", h2, "status")
	for _, want := range []string{"daemon: running (pid ", "soa-d test)", "served by: started on demand by the terminal UI", "sessions: 2 interactive"} {
		if r.code != 0 || !strings.Contains(r.out, want) {
			t.Fatalf("status lacks %q:\n%s%s", want, r.out, r.errOut)
		}
	}
}

func TestSessions(t *testing.T) {
	h := home(t, "")
	serve(t, h)
	c, err := client.Dial(h, "cli")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	ctx := context.Background()
	var cr struct {
		Session struct {
			ID string `json:"id"`
		} `json:"session"`
	}
	if err := c.Call(ctx, "session.create", map[string]any{"cwd": t.TempDir()}, &cr); err != nil {
		t.Fatal(err)
	}
	id := cr.Session.ID
	// A session that never got a turn does not count as a session.
	giveTurn(t, h, id)

	if r := cli(t, t.TempDir(), "", nil, "--home", h, "sessions"); r.code != 0 || !strings.Contains(r.out, id+"  interactive  ") || !strings.Contains(r.out, "(untitled)") {
		t.Fatalf("sessions list: %d %q %q", r.code, r.out, r.errOut)
	}
	if r := cli(t, t.TempDir(), "", nil, "--home", h, "sessions", "rename", id, "the", "parser", "fix"); r.code != 0 || r.out != "renamed "+id+": the parser fix\n" {
		t.Fatalf("rename: %d %q %q", r.code, r.out, r.errOut)
	}
	if r := cli(t, t.TempDir(), "", nil, "--home", h, "sessions", "list"); !strings.Contains(r.out, "the parser fix") {
		t.Fatalf("after rename: %q", r.out)
	}
	if r := cli(t, t.TempDir(), "n\n", nil, "--home", h, "sessions", "delete", id); r.code != 1 || !strings.Contains(r.out, "delete "+id+" (the parser fix)? [y/N] ") || !strings.Contains(r.out, "not deleted") {
		t.Fatalf("delete answered n: %d %q %q", r.code, r.out, r.errOut)
	}
	if r := cli(t, t.TempDir(), "y\n", nil, "--home", h, "sessions", "delete", id); r.code != 0 || !strings.Contains(r.out, "deleted "+id) {
		t.Fatalf("delete answered y: %d %q %q", r.code, r.out, r.errOut)
	}
	if r := cli(t, t.TempDir(), "", nil, "--home", h, "sessions", "list"); r.out != "no sessions\n" {
		t.Fatalf("after delete: %q", r.out)
	}
	if r := cli(t, t.TempDir(), "", nil, "--home", h, "sessions", "rename", "nope", "x"); r.code != 1 || !strings.HasPrefix(r.errOut, "son-of-anton: ") {
		t.Fatalf("rename of an unknown session: %d %q", r.code, r.errOut)
	}
}
