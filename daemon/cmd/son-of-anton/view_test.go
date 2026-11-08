package main

// Phase 12: son-of-anton view and -r ID --view (daemon/phases/12-view-socket.md).

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/server"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// serveView runs a daemon on h in this process, with a view socket, until
// the test ends, and returns the view socket's path.
func serveView(t *testing.T, h string) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "soav")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	sock := filepath.Join(dir, "view.sock")
	ln, err := server.Listen(h)
	if err != nil {
		t.Fatal(err)
	}
	vln, err := server.ListenView(sock, os.Getgid())
	if err != nil {
		t.Fatal(err)
	}
	srv, err := server.New(server.Options{Home: h, Version: "test"})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- srv.Serve(ctx, ln, vln) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(15 * time.Second):
		}
	})
	return sock
}

// session stores a session of kind with a title and messages, through the
// store (the daemon reads what is there).
func session(t *testing.T, h string, s store.Session, msgs ...store.Message) string {
	t.Helper()
	st, err := store.Open(filepath.Join(h, "soa-d.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	ctx := context.Background()
	s, err = st.CreateSession(ctx, s)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.AppendMessages(ctx, s.ID, msgs...); err != nil {
		t.Fatal(err)
	}
	return s.ID
}

func TestViewListsAndShows(t *testing.T) {
	h := home(t, "")
	sock := serveView(t, h)
	at := time.Date(2026, 11, 8, 9, 30, 0, 0, time.UTC)
	stamp := func(d time.Duration) string { return at.Add(d).Local().Format("2006-01-02 15:04:05") }
	calls, _ := json.Marshal([]map[string]any{{"id": "c1", "type": "function",
		"function": map[string]any{"name": "terminal", "arguments": `{"command":"ls"}`}}})
	worker := session(t, h, store.Session{Kind: "interactive", Title: "list the files", Cwd: "/srv/shop"},
		store.Message{Role: "user", Content: "list the files", TS: at},
		store.Message{Role: "assistant", Content: "I'll look.", Reasoning: "private thoughts", ToolCalls: calls, TS: at.Add(time.Second)},
		store.Message{Role: "tool", Content: "a.txt\nb.txt", ToolCallID: "c1", ToolName: "terminal", TS: at.Add(2 * time.Second)},
		store.Message{Role: "assistant", Content: "Two files.", TS: at.Add(3 * time.Second)},
	)
	job := session(t, h, store.Session{Kind: "cron", Title: "the nightly job", Cwd: "/srv/shop"},
		store.Message{Role: "user", Content: "run the job", TS: at})
	chat := session(t, h, store.Session{Kind: "chat", Title: "a private chat", Scope: "gateway", Chat: "signal:group.x"},
		store.Message{Role: "user", Content: "a private message", TS: at})

	r := cli(t, t.TempDir(), "", nil, "view", "--socket", sock)
	if r.code != 0 || !strings.Contains(r.out, worker+"  interactive  idle  ") || !strings.Contains(r.out, "list the files") ||
		strings.Contains(r.out, job) || strings.Contains(r.out, chat) {
		t.Fatalf("view lists workers: %d %q %q", r.code, r.out, r.errOut)
	}
	if r2 := cli(t, t.TempDir(), "", nil, "view", "--socket", sock, "list"); r2.out != r.out {
		t.Fatalf("view list = %q, want %q", r2.out, r.out)
	}
	r = cli(t, t.TempDir(), "", nil, "view", "--socket", sock, "--all")
	if r.code != 0 || !strings.Contains(r.out, worker) || !strings.Contains(r.out, job+"  cron  ") || strings.Contains(r.out, chat) ||
		strings.Contains(r.out, "a private chat") {
		t.Fatalf("view --all: %d %q %q", r.code, r.out, r.errOut)
	}

	r = cli(t, t.TempDir(), "", nil, "view", "--socket", sock, "show", worker)
	want := "[user " + stamp(0) + "]\nlist the files\n\n" +
		"[assistant " + stamp(time.Second) + "]\nI'll look.\n-> terminal {\"command\":\"ls\"}\n\n" +
		"[tool terminal]\na.txt\nb.txt\n\n" +
		"[assistant " + stamp(3*time.Second) + "]\nTwo files.\n"
	if r.code != 0 || r.out != want {
		t.Fatalf("view show: %d %q\nwant %q\n%s", r.code, r.out, want, r.errOut)
	}
	if r := cli(t, t.TempDir(), "", nil, "view", "--socket", sock, "show", chat); r.code != 1 || r.out != "" {
		t.Fatalf("view show of a chat: %d %q %q", r.code, r.out, r.errOut)
	}
}

func TestViewShowPagesToTheStart(t *testing.T) {
	h := home(t, "")
	sock := serveView(t, h)
	var msgs []store.Message
	for i := range 1300 { // more than session.history gives at once
		role := "user"
		if i%2 == 1 {
			role = "assistant"
		}
		msgs = append(msgs, store.Message{Role: role, Content: "line " + strings.Repeat("x", i%7) + "#" + itoa(i)})
	}
	id := session(t, h, store.Session{Kind: "interactive", Cwd: "/tmp"}, msgs...)
	r := cli(t, t.TempDir(), "", nil, "view", "--socket", sock, "show", id)
	if r.code != 0 || strings.Count(r.out, "\n[") != 1299 || !strings.HasPrefix(r.out, "[user ") ||
		!strings.Contains(r.out, "#0\n") || !strings.HasSuffix(r.out, "#1299\n") ||
		strings.Index(r.out, "#600\n") > strings.Index(r.out, "#601\n") {
		t.Fatalf("view show of 1300 messages: %d, %d headers, %q", r.code, strings.Count(r.out, "\n["), r.errOut)
	}
}

func itoa(i int) string { b, _ := json.Marshal(i); return string(b) }

func TestViewOpensTheTUIReadOnly(t *testing.T) {
	h := home(t, "")
	bin, record := stub(t)
	work := t.TempDir()
	env := []string{"SOA_D_BIN=" + bin}
	const id = "20261108_093000_abcdef"
	for _, c := range []struct {
		args []string
		want []string
	}{
		{[]string{"view", "--socket", "/run/x/view.sock", id}, []string{"tui", "--socket", "/run/x/view.sock", "--view", "--resume", id}},
		{[]string{"view", "shop", id}, []string{"tui", "--socket", "/run/son-of-anton-shop/view.sock", "--view", "--resume", id}},
		{[]string{"view", "--socket=/run/x/view.sock", id}, []string{"tui", "--socket", "/run/x/view.sock", "--view", "--resume", id}},
		{[]string{"--home", h, "-r", id, "--view"}, []string{"tui", "--home", h, "--in", work, "--resume", id, "--view"}},
		{[]string{"--home", h, "--view", "--resume", "latest"}, []string{"tui", "--home", h, "--in", work, "--resume", "latest", "--view"}},
	} {
		_ = os.Remove(record)
		if r := cli(t, work, "", env, c.args...); r.code != 0 {
			t.Fatalf("%v: exit %d, %s", c.args, r.code, r.errOut)
		}
		if got := argv(t, record); strings.Join(got, "\x00") != strings.Join(c.want, "\x00") {
			t.Errorf("%v ran soa-d %q, want %q", c.args, got, c.want)
		}
	}
}

// view reaches the daemon through the socket alone: it never starts one, so
// a missing socket is an error, and nothing is written to the home.
func TestViewNeverStartsADaemon(t *testing.T) {
	h := home(t, "") // autostart is on: the terminal UI would start a daemon here
	bin, record := stub(t)
	missing := filepath.Join(t.TempDir(), "view.sock")
	env := []string{"SOA_D_BIN=" + bin, "SON_OF_ANTON_HOME=" + h}
	for _, args := range [][]string{
		{"view", "--socket", missing},
		{"view", "--socket", missing, "--all"},
		{"view", "--socket", missing, "show", "20261108_093000_abcdef"},
		{"view", "no-such-instance-here"},
	} {
		_ = os.Remove(record)
		r := cli(t, t.TempDir(), "", env, args...)
		if r.code != 1 || !strings.Contains(r.errOut, "cannot reach the view socket") {
			t.Errorf("%v: exit %d, stderr %q", args, r.code, r.errOut)
		}
		if _, err := os.Stat(record); err == nil {
			t.Errorf("%v ran soa-d", args)
		}
	}
	if r := cli(t, t.TempDir(), "", env, "view", "no-such-instance-here"); !strings.Contains(r.errOut, "/run/son-of-anton-no-such-instance-here/view.sock") {
		t.Errorf("an instance's socket is named: %q", r.errOut)
	}
	for _, d := range []string{"run", "logs"} {
		if _, err := os.Stat(filepath.Join(h, d)); err == nil {
			t.Errorf("view made %s in the home", d)
		}
	}
}

// A viewer cannot read the instance's home, so view never does: a home whose
// config.toml does not parse changes nothing.
func TestViewNeverReadsTheHome(t *testing.T) {
	h := home(t, "")
	sock := serveView(t, h)
	id := session(t, h, store.Session{Kind: "interactive", Title: "watched", Cwd: "/tmp"},
		store.Message{Role: "user", Content: "hello there"})
	broken := t.TempDir()
	if err := os.WriteFile(filepath.Join(broken, "config.toml"), []byte("this is [not toml"), 0o600); err != nil {
		t.Fatal(err)
	}
	env := []string{"SON_OF_ANTON_HOME=" + broken}
	if r := cli(t, t.TempDir(), "", env, "status"); r.code == 0 {
		t.Fatal("the broken home is not broken")
	}
	if r := cli(t, t.TempDir(), "", env, "view", "--socket", sock); r.code != 0 || !strings.Contains(r.out, id) {
		t.Fatalf("view with a broken home: %d %q %q", r.code, r.out, r.errOut)
	}
	if r := cli(t, t.TempDir(), "", env, "view", "--socket", sock, "show", id); r.code != 0 || !strings.Contains(r.out, "hello there") {
		t.Fatalf("view show with a broken home: %d %q %q", r.code, r.out, r.errOut)
	}
}

func TestViewUsageErrors(t *testing.T) {
	h := home(t, "")
	bin, record := stub(t)
	for _, args := range [][]string{
		{"view"},
		{"view", "--socket"},
		{"view", "shop", "show"},
		{"view", "shop", "show", "a", "b"},
		{"view", "shop", "a", "b"},
		{"view", "../etc", "x"},
		{"view", ".hidden"},
		{"view", "shop/x"},
		{"view", "shop", "--all", "20261108_093000_abcdef"},
		{"view", "shop", "--limit", "0"},
		{"view", "shop", "--bogus"},
		{"view", "--socket", "/run/x/view.sock", "shop", "20261108_093000_abcdef"},
		{"--home", h, "--view"},
		{"--home", h, "-c", "--view"},
		{"--home", h, "-r", "x", "--view", "-m", "other"},
		{"--home", h, "-r", "x", "--view", "--reasoning", "low"},
		{"--home", h, "-r", "x", "--view", "--yolo"},
		{"--home", h, "-q", "x", "--view"},
		{"--home", h, "-z", "x", "--view"},
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
