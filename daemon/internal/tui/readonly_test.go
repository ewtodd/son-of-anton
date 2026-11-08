package tui_test

// Phase 12: the read-only TUI (daemon/phases/12-view-socket.md), over the view
// socket and, for the owner, over the normal one.

import (
	"context"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/client"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/tui"
)

const readOnlyLine = "read-only view: you can watch this session, not send to it or answer it"

// openView starts the read-only TUI over the daemon's view socket, at 100x30.
func openView(t *testing.T, d *daemon, o tui.Options) *tui.App {
	t.Helper()
	c, err := client.DialSocket(d.view, "tui")
	if err != nil {
		t.Fatal(err)
	}
	o.View, o.Socket, o.Version = true, d.view, "test"
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

func TestTheViewTUIWatches(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "first answer"},
		fakellm.Turn{Content: "second answer"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "first question")
	waitScreen(t, a, "first answer")
	idle(t, a)
	id := a.Session()

	v := openView(t, d, tui.Options{Resume: id})
	s := waitScreen(t, v, "first question", "first answer", readOnlyLine, "read-only view · ctrl+p for commands · ctrl+q to quit", "ctrl+p commands")
	if strings.Contains(s, "Message Son of Anton…") || strings.Contains(s, "/help commands") {
		t.Fatalf("the view has a prompt:\n%s", s)
	}
	checkFrame(t, s, 100, 30)
	v.Type("sneaky")
	v.Press("enter")
	v.Type("!touch pwned")
	v.Press("enter")
	time.Sleep(300 * time.Millisecond)
	if s := v.Screen(); strings.Contains(s, "sneaky") || strings.Contains(s, "pwned") || fake.Count() != 1 {
		t.Fatalf("typing reached somewhere (%d requests):\n%s", fake.Count(), s)
	}
	if _, err := os.Stat(filepath.Join(d.cwd, "pwned")); err == nil {
		t.Fatal("!cmd ran in the view")
	}

	// The owner drives on; the view follows live.
	send(a, "second question")
	waitScreen(t, v, "second question", "second answer")
	if s := idle(t, a); !strings.Contains(s, "Message Son of Anton…") {
		t.Fatalf("the owner lost the prompt:\n%s", s)
	}
}

func TestTheViewTUIShowsApprovalsReadOnly(t *testing.T) {
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
	v := openView(t, d, tui.Options{Resume: a.Session()})
	waitScreen(t, v, readOnlyLine)
	send(a, "make it writable")
	s := waitScreen(t, v, "The agent asks to run a command", "chmod 777 f.txt", "Allow once", "read-only: the session's driver or its chat answers this")
	checkFrame(t, s, 100, 30)
	v.Press("1")
	v.Press("enter")
	v.Press("down", "enter")
	time.Sleep(300 * time.Millisecond)
	if fi, _ := os.Stat(f); fi.Mode().Perm() != 0o644 {
		t.Fatal("the view answered the approval")
	}
	waitScreen(t, a, "The agent asks to run a command", "Allow once")
	v.Press("esc") // hides it here only
	waitGone(t, v, "The agent asks to run a command")
	waitScreen(t, a, "The agent asks to run a command")
	a.Press("1")
	waitScreen(t, v, "changed it")
	if fi, _ := os.Stat(f); fi.Mode().Perm() != 0o777 {
		t.Fatal("the owner's approval ran the command")
	}
}

func TestTheViewTUIOffersOnlyReading(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "one answer"},
		fakellm.Turn{Content: "other answer"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "one question")
	waitScreen(t, a, "one answer")
	one := a.Session()
	b := open(t, d, tui.Options{})
	send(b, "other question")
	waitScreen(t, b, "other answer")
	other := b.Session()

	v := openView(t, d, tui.Options{Resume: one})
	waitScreen(t, v, "one answer", readOnlyLine)
	v.Press("ctrl+p")
	s := waitScreen(t, v, "/resume", "/status", "/help", "/quit")
	for _, gone := range []string{"/new", "/model", "/reasoning", "/perm", "/commit", "/compact", "/retitle", "/handoff", "/sendnow", "/sendall", "/cancel", "/stop", "/clear"} {
		if strings.Contains(s, gone) {
			t.Errorf("the view's palette offers %s:\n%s", gone, s)
		}
	}
	v.Type("status")
	v.Press("enter")
	waitScreen(t, v, "session "+one+" · ", "· mode default ·")

	v.Press("shift+tab", "ctrl+g")
	time.Sleep(200 * time.Millisecond)
	if sess, _ := peek(t, d, one); str(sess, "mode") != "default" {
		t.Fatalf("shift+tab changed the mode: %v", sess)
	}

	// /resume switches, still as a viewer: the other session's TUI keeps it.
	v.Press("ctrl+p")
	waitScreen(t, v, "/resume")
	v.Type("resume")
	v.Press("enter")
	waitScreen(t, v, "Resume session", "other question")
	v.Press("enter")
	waitScreen(t, v, "other answer", readOnlyLine)
	if v.Session() != other {
		t.Fatalf("the view is on %s, want %s", v.Session(), other)
	}
	time.Sleep(200 * time.Millisecond)
	if s := idle(t, b); !strings.Contains(s, "Message Son of Anton…") {
		t.Fatalf("the other session's driver lost it:\n%s", s)
	}
}

func str(m map[string]any, k string) string { s, _ := m[k].(string); return s }

// A viewer has no turn to decide about: ctrl+c leaves at once and stops
// nothing.
func TestTheViewTUIQuitsWithoutStoppingTheTurn(t *testing.T) {
	rel := make(chan struct{})
	fake := fakellm.New(t, fakellm.Turn{Content: "a long answer", ChunkRunes: 2, Release: rel})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	v := openView(t, d, tui.Options{Resume: a.Session()})
	send(a, "go")
	waitScreen(t, v, "ctrl+q quit")
	v.Press("ctrl+c")
	waitExit(t, v)
	close(rel)
	s := waitScreen(t, a, "a long answer")
	if strings.Contains(s, "interrupted") {
		t.Fatalf("the viewer's ctrl+c interrupted the turn:\n%s", s)
	}
}

// The owner can look at a session over the normal socket without taking it:
// the TUI that drives it keeps driving.
func TestTheOwnerViewsWithoutTakingOver(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "first answer"},
		fakellm.Turn{Content: "second answer"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "first question")
	waitScreen(t, a, "first answer")
	id := a.Session()

	c, err := client.Dial(d.home, "tui")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	o := tui.Options{Home: d.home, Cwd: d.cwd, View: true, Resume: id, Version: "test"}
	b, err := tui.Start(c, o, nil, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	b.Resize(100, 30)
	waitScreen(t, b, "first answer", readOnlyLine)
	send(a, "second question")
	waitScreen(t, b, "second answer")
	if s := idle(t, a); !strings.Contains(s, "Message Son of Anton…") {
		t.Fatalf("the driver lost the session to a viewer:\n%s", s)
	}
	b.Press("ctrl+q")
	waitExit(t, b)
	idle(t, a)
}

func TestTheViewTUIRefusesChanges(t *testing.T) {
	d := newDaemon(t, fakellm.New(t))
	c, err := client.DialSocket(d.view, "tui")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	for _, o := range []tui.Options{
		{View: true},
		{View: true, Continue: true},
		{View: true, Resume: "x", Model: "other-model"},
		{View: true, Resume: "x", Reasoning: "low"},
		{View: true, Resume: "x", Yolo: true},
	} {
		o.Socket = d.view
		if a, err := tui.Start(c, o, nil, io.Discard); err == nil {
			a.Press("ctrl+q")
			t.Errorf("Start(%+v) took", o)
		}
	}
	// Nothing was made.
	ctx, cancel := context.WithTimeout(context.Background(), waitFor)
	defer cancel()
	var res struct {
		Sessions []map[string]any `json:"sessions"`
	}
	if err := c.Call(ctx, "session.list", map[string]any{}, &res); err != nil || len(res.Sessions) != 0 {
		t.Fatalf("sessions: %v, %v", res.Sessions, err)
	}
}
