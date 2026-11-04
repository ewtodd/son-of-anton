package tui_test

// Phase 9a: the Go TUI, driven like a person at the keyboard against a real
// daemon on a real socket and internal/fakellm (daemon/phases/09a-tui-core.md).

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/ewtodd/son-of-anton/daemon/internal/client"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/server"
	"github.com/ewtodd/son-of-anton/daemon/internal/tui"
)

const waitFor = 10 * time.Second

type daemon struct {
	t      *testing.T
	home   string
	cwd    string
	cancel context.CancelFunc
	done   chan error
}

// newDaemon starts a server in a fresh short home (a unix socket path must fit
// in 107 bytes), with two model routes, and a working directory for sessions.
func newDaemon(t *testing.T, fake *fakellm.Server) *daemon {
	t.Helper()
	home, err := os.MkdirTemp("", "soat")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(home) })
	t.Setenv("SOA_D_TEST_KEY", "")
	t.Setenv("SON_OF_ANTON_HOME", "")
	cfg := "[model]\ndefault = \"test-model\"\nprovider = \"custom\"\n\n" +
		"[custom_providers.custom]\nbase_url = \"" + fake.URL + "\"\nkey_env = \"SOA_D_TEST_KEY\"\n\n" +
		"[custom_providers.custom.models.\"test-model\"]\nreasoning_efforts = [\"low\", \"high\"]\n\n" +
		"[custom_providers.custom.models.\"other-model\"]\n"
	if err := os.WriteFile(filepath.Join(home, "config.toml"), []byte(cfg), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(home, ".env"), []byte("SOA_D_TEST_KEY=s3cret\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	d := &daemon{t: t, home: home, cwd: t.TempDir()}
	d.start()
	t.Cleanup(d.stop)
	return d
}

func (d *daemon) start() {
	d.t.Helper()
	ln, err := server.Listen(d.home)
	if err != nil {
		d.t.Fatal(err)
	}
	srv, err := server.New(server.Options{Home: d.home, Version: "test"})
	if err != nil {
		d.t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	d.cancel, d.done = cancel, make(chan error, 1)
	go func() { d.done <- srv.Serve(ctx, ln) }()
}

func (d *daemon) stop() {
	if d.cancel == nil {
		return
	}
	d.cancel()
	d.cancel = nil
	select {
	case <-d.done:
	case <-time.After(15 * time.Second):
		d.t.Error("Serve did not return")
	}
}

// open starts the TUI on a fresh connection, at 100x30.
func open(t *testing.T, d *daemon, o tui.Options) *tui.App {
	t.Helper()
	c, err := client.Dial(d.home, "tui")
	if err != nil {
		t.Fatal(err)
	}
	o.Home = d.home
	if o.Cwd == "" {
		o.Cwd = d.cwd
	}
	o.Version = "test"
	a, err := tui.Start(c, o, nil, io.Discard)
	if err != nil {
		c.Close()
		t.Fatal(err)
	}
	a.Resize(100, 30)
	t.Cleanup(func() {
		a.Press("ctrl+q")
		waitExit(t, a)
		c.Close()
	})
	return a
}

func waitExit(t *testing.T, a *tui.App) {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- a.Wait() }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the TUI did not exit")
	}
}

func send(a *tui.App, text string) {
	a.Type(text)
	a.Press("enter")
}

// waitScreen waits until every want is on the screen and returns the screen.
func waitScreen(t *testing.T, a *tui.App, wants ...string) string {
	t.Helper()
	deadline := time.Now().Add(waitFor)
	for {
		s := a.Screen()
		missing := ""
		for _, w := range wants {
			if !strings.Contains(s, w) {
				missing = w
				break
			}
		}
		if missing == "" {
			return s
		}
		if time.Now().After(deadline) {
			t.Fatalf("%q never appeared; the screen:\n%s", missing, s)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func waitGone(t *testing.T, a *tui.App, gone string) string {
	t.Helper()
	deadline := time.Now().Add(waitFor)
	for {
		s := a.Screen()
		if !strings.Contains(s, gone) {
			return s
		}
		if time.Now().After(deadline) {
			t.Fatalf("%q never went; the screen:\n%s", gone, s)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// idle waits for the idle status line.
func idle(t *testing.T, a *tui.App) string { t.Helper(); return waitScreen(t, a, "/help commands") }

// hasLine reports whether some line, trimmed, is exactly want.
func hasLine(screen, want string) bool {
	for _, l := range strings.Split(screen, "\n") {
		if strings.TrimSpace(l) == want {
			return true
		}
	}
	return false
}

func lineWith(screen, part string) string {
	for _, l := range strings.Split(screen, "\n") {
		if strings.Contains(l, part) {
			return l
		}
	}
	return ""
}

// dock is the bottom of the screen: the prompt frame, the status line and the
// blank rows around them.
func dock(screen string) string {
	lines := strings.Split(screen, "\n")
	return strings.Join(lines[max(0, len(lines)-8):], "\n")
}

func checkFrame(t *testing.T, screen string, w, h int) {
	t.Helper()
	lines := strings.Split(screen, "\n")
	if len(lines) != h {
		t.Fatalf("%d lines at %dx%d:\n%s", len(lines), w, h, screen)
	}
	for i, l := range lines {
		if n := utf8.RuneCountInString(l); n > w {
			t.Fatalf("line %d is %d wide at %dx%d: %q", i, n, w, h, l)
		}
	}
}

// peek reads the session as a second client does.
func peek(t *testing.T, d *daemon, id string) (map[string]any, []map[string]any) {
	t.Helper()
	c, err := client.Dial(d.home, "cli")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	ctx, cancel := context.WithTimeout(context.Background(), waitFor)
	defer cancel()
	var res struct {
		Session  map[string]any   `json:"session"`
		Messages []map[string]any `json:"messages"`
	}
	if err := c.Call(ctx, "session.attach", map[string]any{"session": id, "as": "viewer"}, &res); err != nil {
		t.Fatal(err)
	}
	return res.Session, res.Messages
}

func terminal(id, cmd string) fakellm.ToolCall {
	b, _ := json.Marshal(map[string]string{"command": cmd})
	return fakellm.ToolCall{ID: id, Name: "terminal", Arguments: string(b)}
}

// lastMessage is the last message of request i, as raw JSON text.
func lastMessage(fake *fakellm.Server, i int) string {
	m := fake.Messages(i)
	return string(m[len(m)-1])
}

func TestATurnStreamsIntoTheFeed(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Reasoning: "let me think\nabout it", Content: "hello there, friend", ChunkRunes: 3})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	s := waitScreen(t, a, "Message Son of Anton…", "session "+a.Session(), "vtest", "/help for commands · :q to quit")
	if meta := lineWith(dock(s), "test-model"); !strings.Contains(meta, "default") || !strings.Contains(meta, "reasoning default") {
		t.Fatalf("the meta row = %q", meta)
	}
	if !strings.Contains(lineWith(s, "/help commands"), d.cwd) {
		t.Fatalf("the idle status shows the cwd:\n%s", s)
	}
	checkFrame(t, s, 100, 30)

	send(a, "hi")
	s = waitScreen(t, a, "hello there, friend", "▶ reasoning · 2 lines")
	if !strings.Contains(s, "▎  hi") {
		t.Fatalf("the user block:\n%s", s)
	}
	s = idle(t, a)
	checkFrame(t, s, 100, 30)
	if fake.Count() != 1 {
		t.Fatalf("%d requests", fake.Count())
	}
}

func TestAnApprovalIsAnsweredFromTheModal(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("c1", "chmod 777 f.txt")}},
		fakellm.Turn{Content: "changed it"},
	)
	d := newDaemon(t, fake)
	f := filepath.Join(d.cwd, "f.txt")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	a := open(t, d, tui.Options{})
	send(a, "make it writable")
	s := waitScreen(t, a, "The agent asks to run a command", "chmod 777 f.txt", "Allow once", "Allow for this session", "Always allow", "Deny")
	checkFrame(t, s, 100, 30)
	a.Press("1")
	s = waitScreen(t, a, "changed it")
	if strings.Contains(s, "Allow once") || !strings.Contains(s, "$ chmod 777 f.txt") {
		t.Fatalf("after answering:\n%s", s)
	}
	if fi, _ := os.Stat(f); fi.Mode().Perm() != 0o777 {
		t.Fatalf("mode %v: the command did not run", fi.Mode().Perm())
	}
}

func TestEscDeniesAnApproval(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("c1", "chmod 777 f.txt")}},
		fakellm.Turn{Content: "left it alone"},
	)
	d := newDaemon(t, fake)
	f := filepath.Join(d.cwd, "f.txt")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	a := open(t, d, tui.Options{})
	send(a, "make it writable")
	waitScreen(t, a, "The agent asks to run a command")
	a.Press("esc")
	waitScreen(t, a, "left it alone")
	if fi, _ := os.Stat(f); fi.Mode().Perm() != 0o644 {
		t.Fatalf("mode %v: a denied command ran", fi.Mode().Perm())
	}
}

func TestAQuestionIsAnsweredFromTheModal(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{{ID: "q1", Name: "clarify", Arguments: `{"question":"Which flavour?","choices":["vanilla","chocolate"]}`}}},
		fakellm.Turn{Content: "chocolate it is"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{{ID: "q2", Name: "clarify", Arguments: `{"question":"Your name?"}`}}},
		fakellm.Turn{Content: "nice to meet you"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "ice cream")
	waitScreen(t, a, "Son of Anton has a question", "Which flavour?", "vanilla", "chocolate", "Other…")
	a.Press("down", "enter")
	waitScreen(t, a, "chocolate it is")
	if m := lastMessage(fake, 1); !strings.Contains(m, "chocolate") {
		t.Fatalf("the clarify result = %s", m)
	}

	send(a, "hello")
	waitScreen(t, a, "Your name?")
	send(a, "Ada")
	waitScreen(t, a, "nice to meet you")
	if m := lastMessage(fake, 3); !strings.Contains(m, "Ada") {
		t.Fatalf("the typed answer = %s", m)
	}
}

func TestAMessageSentWhileBusyQueues(t *testing.T) {
	rel := make(chan struct{})
	fake := fakellm.New(t,
		fakellm.Turn{Content: "first answer", ChunkRunes: 6, Release: rel},
		fakellm.Turn{Content: "second answer"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "one")
	waitScreen(t, a, "first", "ctrl+c interrupt")
	send(a, "two")
	s := waitScreen(t, a, "↳ two", "1 queued", "/sendnow sends this now")
	checkFrame(t, s, 100, 30)
	close(rel)
	waitScreen(t, a, "second answer")
	waitGone(t, a, "↳ two")
	idle(t, a)
	if fake.Count() != 2 {
		t.Fatalf("%d requests", fake.Count())
	}
}

func TestCtrlCInterruptsThenQuits(t *testing.T) {
	rel := make(chan struct{})
	t.Cleanup(func() { close(rel) })
	fake := fakellm.New(t,
		fakellm.Turn{Content: "slow answer", ChunkRunes: 5, Release: rel},
		fakellm.Turn{Content: "after that"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "slow")
	waitScreen(t, a, "ctrl+c interrupt")
	a.Press("ctrl+c")
	waitScreen(t, a, "interrupting… (ctrl+c again to force quit)")
	s := idle(t, a)
	if !hasLine(s, "interrupted") {
		t.Fatalf("no interrupted note:\n%s", s)
	}
	send(a, "again")
	waitScreen(t, a, "after that")
	idle(t, a)

	a.Type("a draft")
	waitScreen(t, a, "a draft")
	a.Press("ctrl+c")
	waitGone(t, a, "a draft")
	a.Press("ctrl+c")
	waitExit(t, a)
}

func TestEscInterrupts(t *testing.T) {
	rel := make(chan struct{})
	t.Cleanup(func() { close(rel) })
	fake := fakellm.New(t, fakellm.Turn{Content: "slow answer", ChunkRunes: 5, Release: rel})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "slow")
	waitScreen(t, a, "ctrl+c interrupt")
	a.Press("esc")
	waitScreen(t, a, "interrupting…")
	if s := idle(t, a); !hasLine(s, "interrupted") {
		t.Fatalf("no interrupted note:\n%s", s)
	}
}

func TestDoubleCtrlCForceQuits(t *testing.T) {
	rel := make(chan struct{})
	t.Cleanup(func() { close(rel) })
	fake := fakellm.New(t, fakellm.Turn{Content: "slow answer", ChunkRunes: 5, Release: rel})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "slow")
	waitScreen(t, a, "ctrl+c interrupt")
	a.Press("ctrl+c", "ctrl+c")
	waitExit(t, a)
}

func TestQuittingLeavesTheTurnRunning(t *testing.T) {
	rel := make(chan struct{})
	fake := fakellm.New(t, fakellm.Turn{Content: "finished anyway", ChunkRunes: 9, Release: rel})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "work")
	waitScreen(t, a, "ctrl+c interrupt")
	id := a.Session()
	send(a, ":q")
	waitExit(t, a)
	close(rel)
	deadline := time.Now().Add(waitFor)
	for {
		sess, msgs := peek(t, d, id)
		if sess["status"] == "idle" && len(msgs) > 0 {
			if last := msgs[len(msgs)-1]; last["role"] != "assistant" || last["content"] != "finished anyway" {
				t.Fatalf("the turn after :q ended with %v", last)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the turn never finished: %v", sess)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func TestCommandsReachTheDaemon(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "ok"})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "/model other-model")
	waitScreen(t, a, "⚙ /model other-model", "model: other-model (from the next turn)")
	send(a, "/reasoning low") // after the model: a model change resets the effort
	waitScreen(t, a, "reasoning: low (from the next turn)", "reasoning low")
	send(a, "/perm yolo")
	waitScreen(t, a, "permission mode: yolo")
	if meta := lineWith(dock(a.Screen()), "other-model"); !strings.HasPrefix(strings.TrimLeft(meta, " ▎"), "yolo") {
		t.Fatalf("the meta row = %q", meta)
	}
	send(a, "hi")
	waitScreen(t, a, "ok")
	idle(t, a)
	if r := fake.Request(0); r["model"] != "other-model" || r["reasoning_effort"] != "low" {
		t.Fatalf("the request used model %v, effort %v", r["model"], r["reasoning_effort"])
	}
	if sess, _ := peek(t, d, a.Session()); sess["mode"] != "yolo" {
		t.Fatalf("mode = %v", sess["mode"])
	}
	a.Press("shift+tab")
	deadline := time.Now().Add(waitFor)
	for sess, _ := peek(t, d, a.Session()); sess["mode"] != "default"; sess, _ = peek(t, d, a.Session()) {
		if time.Now().After(deadline) {
			t.Fatalf("shift+tab from yolo: mode %v", sess["mode"])
		}
		time.Sleep(50 * time.Millisecond)
	}

	send(a, "/goal")
	waitScreen(t, a, "unknown command /goal (/help lists them)")
	a.Resize(100, 40) // room for all of /help
	send(a, "/help")
	waitScreen(t, a, "commands:", "/model", "/sendnow", "!cmd runs a shell command here · ctrl+p opens the palette · :q quits")
	send(a, "/status")
	waitScreen(t, a, "session "+a.Session()+" · other-model · mode default")
	if fake.Count() != 1 {
		t.Fatalf("commands made %d model requests", fake.Count()-1)
	}
}

func TestBangRunsAShellCommandHere(t *testing.T) {
	fake := fakellm.New(t)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "!echo bang-$((6*7)) && pwd")
	waitScreen(t, a, "bang-42", d.cwd)
	send(a, "!exit 3")
	waitScreen(t, a, "! exited 3")
	send(a, "!")
	waitScreen(t, a, "Type ! followed by a shell command")
	if fake.Count() != 0 {
		t.Fatalf("%d model requests", fake.Count())
	}
}

func TestNewAndResume(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "the first session's answer"})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "hi")
	waitScreen(t, a, "the first session's answer")
	idle(t, a)
	old := a.Session()

	send(a, "/new")
	deadline := time.Now().Add(waitFor)
	for a.Session() == old {
		if time.Now().After(deadline) {
			t.Fatal("/new kept the session")
		}
		time.Sleep(20 * time.Millisecond)
	}
	waitScreen(t, a, "session "+a.Session())
	waitGone(t, a, "the first session's answer")

	send(a, "/resume "+old)
	waitScreen(t, a, "resumed session "+old, "the first session's answer")
	if a.Session() != old {
		t.Fatalf("session %s after /resume %s", a.Session(), old)
	}
	send(a, "/resume nope")
	waitScreen(t, a, "session nope is not in soa-d")
}

func TestStartChoosesTheSession(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "remember me"})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "hi")
	waitScreen(t, a, "remember me")
	idle(t, a)
	first := a.Session()
	a.Press("ctrl+q")
	waitExit(t, a)

	b := open(t, d, tui.Options{Continue: true})
	if b.Session() != first {
		t.Fatalf("-c attached %s, want %s", b.Session(), first)
	}
	waitScreen(t, b, "resumed session "+first, "▎  hi", "remember me")
	b.Press("ctrl+q")
	waitExit(t, b)

	if c := open(t, d, tui.Options{Resume: "latest"}); c.Session() != first {
		t.Fatalf("--resume latest attached %s, want %s", c.Session(), first)
	}

	other := t.TempDir()
	var errOut bytes.Buffer
	e := open(t, d, tui.Options{Continue: true, Cwd: other, Stderr: &errOut})
	if e.Session() == first || errOut.String() != fmt.Sprintf("soa-d: no session to continue in %s; starting a new one\n", other) {
		t.Fatalf("-c with nothing to continue: session %s, stderr %q", e.Session(), errOut.String())
	}

	c, err := client.Dial(d.home, "tui")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if _, err := tui.Start(c, tui.Options{Home: d.home, Cwd: d.cwd, Resume: "nope"}, nil, io.Discard); err == nil || err.Error() != "session nope is not in soa-d" {
		t.Fatalf("--resume nope: %v", err)
	}

	f := open(t, d, tui.Options{Model: "other-model", Yolo: true})
	if sess, _ := peek(t, d, f.Session()); sess["model"] != "other-model" || sess["mode"] != "yolo" {
		t.Fatalf("-m other-model --yolo: %v %v", sess["model"], sess["mode"])
	}
	waitScreen(t, f, "yolo", "other-model")
}

func TestItReconnectsAfterADaemonRestart(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "before"}, fakellm.Turn{Content: "after the restart"})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "one")
	waitScreen(t, a, "before")
	idle(t, a)
	d.stop()
	waitScreen(t, a, "lost the connection to soa-d; reconnecting…")
	d.start()
	waitScreen(t, a, "reconnected to soa-d")
	send(a, "two")
	waitScreen(t, a, "after the restart", "before")
}

func TestTheFrameFitsEverySize(t *testing.T) {
	long := strings.Repeat("words that wrap ", 40)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("c1", "echo "+strings.Repeat("x", 300))}},
		fakellm.Turn{Content: long + "\n\nTHE END"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{Yolo: true})
	send(a, "a long user message "+strings.Repeat("that wraps ", 30))
	waitScreen(t, a, "THE END")
	idle(t, a)
	a.Type(strings.Repeat("a long draft ", 20))
	for _, sz := range [][2]int{{40, 12}, {80, 24}, {160, 40}, {60, 16}, {100, 30}} {
		a.Resize(sz[0], sz[1])
		s := a.Screen()
		checkFrame(t, s, sz[0], sz[1])
		if !strings.Contains(s, "THE END") {
			t.Fatalf("at %dx%d the newest output is not in view:\n%s", sz[0], sz[1], s)
		}
		// On a short screen the answer pushes the tool row out of view; where
		// it shows, it is one line, cut. At 100x30 it must show.
		row := lineWith(s, "$ echo")
		if sz == [2]int{100, 30} && row == "" {
			t.Fatalf("at 100x30 the tool row is not in view:\n%s", s)
		}
		if sz[0] <= 100 && row != "" && !strings.Contains(row, "…") {
			t.Fatalf("at %dx%d the tool row is not cut to one line:\n%s", sz[0], sz[1], s)
		}
	}
}

func TestScrollingNeverYanksTheView(t *testing.T) {
	var lines strings.Builder
	for i := range 120 {
		fmt.Fprintf(&lines, "line %03d\n", i)
	}
	body := lines.String()
	rel := make(chan struct{})
	fake := fakellm.New(t, fakellm.Turn{Content: body + "END-MARK", ChunkRunes: utf8.RuneCountInString(body), Release: rel})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "count")
	waitScreen(t, a, "line 119")
	a.Press("pgup")
	waitGone(t, a, "line 119")
	close(rel)
	if s := idle(t, a); strings.Contains(s, "END-MARK") {
		t.Fatalf("new output moved a scrolled-up view:\n%s", s)
	}
	for range 10 {
		a.Press("pgdown")
	}
	waitScreen(t, a, "END-MARK", "line 119")
}

func TestHistoryRecall(t *testing.T) {
	fake := fakellm.New(t)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "/perm")
	waitScreen(t, a, "permission mode: default")
	send(a, "/status")
	waitScreen(t, a, "session "+a.Session())

	a.Type("abc")
	a.Press("up")
	if s := waitGone(t, a, "abc"); !strings.Contains(dock(s), "/status") {
		t.Fatalf("↑ did not recall /status:\n%s", dock(s))
	}
	a.Press("up")
	waitScreen(t, a, "/perm")
	a.Press("up") // the oldest entry stays
	if s := a.Screen(); !strings.Contains(dock(s), "/perm") {
		t.Fatalf("↑ past the oldest:\n%s", dock(s))
	}
	a.Press("down")
	if s := waitScreen(t, a, "/status"); strings.Contains(dock(s), "/perm") {
		t.Fatalf("↓:\n%s", dock(s))
	}
	a.Press("down")
	waitScreen(t, a, "abc")

	got := tui.ReadHistory(filepath.Join(d.home, ".son_of_anton_history"))
	if len(got) != 2 || got[0] != "/perm" || got[1] != "/status" {
		t.Fatalf("the history file holds %q", got)
	}
}

func TestResumingARunningSessionShowsItsTurn(t *testing.T) {
	rel := make(chan struct{})
	fake := fakellm.New(t, fakellm.Turn{Content: "streaming along nicely", ChunkRunes: 10, Release: rel})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "go")
	waitScreen(t, a, "streaming")
	running := a.Session()
	send(a, ":q")
	waitExit(t, a)

	// The live tail follows the attach reply at once, before the switch is
	// applied: it must not be lost.
	b := open(t, d, tui.Options{})
	send(b, "/resume "+running)
	waitScreen(t, b, "▎  go", "streaming", "ctrl+c interrupt")
	close(rel)
	waitScreen(t, b, "streaming along nicely")
	idle(t, b)
}

func TestAutoShowsTheModelThatAnswered(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "hi", ServedModel: "radiance/qwen3.8-27b"})
	d := newDaemon(t, fake)
	restartWith(t, d, "\n[custom_providers.custom.models.\"auto\"]\n")
	a := open(t, d, tui.Options{Model: "auto"})
	s := waitScreen(t, a, "Message Son of Anton…")
	if strings.Contains(s, "auto →") || !strings.Contains(lineWith(dock(s), "reasoning"), "auto") {
		t.Fatalf("before any turn the model is plain auto:\n%s", dock(s))
	}
	send(a, "hello")
	waitScreen(t, a, "auto → qwen3.8-27b")
}

// restartWith appends extra to the daemon's config.toml and restarts it.
func restartWith(t *testing.T, d *daemon, extra string) {
	t.Helper()
	d.stop()
	f, err := os.OpenFile(filepath.Join(d.home, "config.toml"), os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString(extra); err != nil {
		t.Fatal(err)
	}
	f.Close()
	d.start()
}

// A reasoning fragment that arrives after the answer has begun (a server
// releasing its reasoning's tail late) joins the response's reasoning block:
// the answer stays one block and no reasoning row appears inside it.
func TestLateReasoningDoesNotSplitTheAnswer(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Reasoning: "let me think\nabout it", Content: "Let me look at the recent commits.", ChunkRunes: 4, LateReasoning: " diff."})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "hi")
	s := waitScreen(t, a, "Let me look at the recent commits.", "▶ reasoning · 2 lines")
	s = idle(t, a)
	if n := strings.Count(s, "▶ reasoning") + strings.Count(s, "▼ reasoning"); n != 1 {
		t.Fatalf("one reasoning row, got %d:\n%s", n, s)
	}
	if strings.Contains(s, "diff.") {
		t.Fatalf("the late fragment is folded into the reasoning block, not shown in the answer:\n%s", s)
	}
}

func TestEnterDuringAToolGoesInOnceItEnds(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("c1", "sleep 0.5; echo finished")}},
		fakellm.Turn{Content: "took it in"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "one")
	waitScreen(t, a, "sleep 0.5")
	send(a, "two")
	waitScreen(t, a, "took it in")
	waitGone(t, a, "↳ two")
	idle(t, a)
	if fake.Count() != 2 {
		t.Fatalf("%d requests; the tool's result and the message go in one request", fake.Count())
	}
	if last := lastMessage(fake, 1); !strings.Contains(last, `"two"`) {
		t.Fatalf("the request after the tool ends %s", last)
	}
}
