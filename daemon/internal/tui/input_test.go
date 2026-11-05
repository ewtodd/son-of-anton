package tui_test

// Phase 9c: pickers, completion, suggestions, the palette and the editor
// (daemon/phases/09c-tui-input.md).

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/tui"
)

// waitDock waits until the prompt and status rows contain want.
func waitDock(t *testing.T, a *tui.App, want string) {
	t.Helper()
	deadline := time.Now().Add(waitFor)
	for !strings.Contains(dock(a.Screen()), want) {
		if time.Now().After(deadline) {
			t.Fatalf("the prompt never showed %q:\n%s", want, dock(a.Screen()))
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// clearPrompt empties the prompt (ctrl+c while idle with text).
func clearPrompt(t *testing.T, a *tui.App) {
	t.Helper()
	a.Press("ctrl+c")
	waitDock(t, a, "Message Son of Anton…")
}

func TestTheModelAndReasoningPickers(t *testing.T) {
	d := newDaemon(t, fakellm.New(t))
	a := open(t, d, tui.Options{})
	send(a, "/model")
	waitScreen(t, a, "Switch model  ·  now test-model", "test-model  ●", "other-model", "reasoning: low, high", "type to filter")
	a.Type("oth")
	waitGone(t, a, "test-model  ●")
	a.Press("enter")
	waitScreen(t, a, "model: other-model (from the next turn)")
	if sess, _ := peek(t, d, a.Session()); sess["model"] != "other-model" {
		t.Fatalf("model %v", sess["model"])
	}
	send(a, "/reasoning")
	waitScreen(t, a, "other-model has no configured reasoning levels")

	send(a, "/model test-model")
	waitScreen(t, a, "Reasoning  ·  now default", "the configured default", "1-9 pick")
	a.Press("3")
	waitScreen(t, a, "model: test-model (from the next turn)", "reasoning: high (from the next turn)")
	waitGone(t, a, "Reasoning  ·  now")

	send(a, "/model")
	waitScreen(t, a, "Switch model  ·  now test-model")
	a.Press("esc")
	waitGone(t, a, "Switch model")
	if sess, _ := peek(t, d, a.Session()); sess["model"] != "test-model" || sess["reasoning_effort"] != "high" {
		t.Fatalf("after esc: %v %v", sess["model"], sess["reasoning_effort"])
	}
}

func TestTheResumePicker(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "the alpha answer"}, fakellm.Turn{Content: "the beta answer"})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "alpha planning session")
	waitScreen(t, a, "the alpha answer")
	idle(t, a)
	alpha := a.Session()
	send(a, "/new")
	waitGone(t, a, "the alpha answer")
	send(a, "beta planning session")
	waitScreen(t, a, "the beta answer")
	idle(t, a)
	send(a, "/new") // a fresh feed: only the picker shows the titles
	waitGone(t, a, "the beta answer")

	send(a, "/resume")
	waitScreen(t, a, "Resume session", "alpha planning session", "beta planning session", "untitled")
	a.Type("ALPHA")
	waitGone(t, a, "beta planning session")
	a.Press("enter")
	waitScreen(t, a, "resumed session "+alpha, "the alpha answer")
	if a.Session() != alpha {
		t.Fatalf("resumed %s, want %s", a.Session(), alpha)
	}
	send(a, "/pickup")
	waitScreen(t, a, "no workers to pick up")
}

func TestTheCancelPicker(t *testing.T) {
	rel := make(chan struct{})
	fake := fakellm.New(t,
		fakellm.Turn{Content: "busy now", ChunkRunes: 5, Release: rel},
		fakellm.Turn{Content: "after the rest"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "/cancel")
	waitScreen(t, a, "nothing queued")
	send(a, "one")
	waitScreen(t, a, "ctrl+c interrupt")
	for _, q := range []string{"q-first", "q-second", "q-third"} {
		send(a, q)
	}
	waitScreen(t, a, "3 queued")
	send(a, "/cancel")
	waitScreen(t, a, "Drop queued messages", "[ ] q-first", "[ ] q-second", "[ ] q-third", "space toggle")
	a.Press("space", "down", "space")
	waitScreen(t, a, "[x] q-first", "[x] q-second", "[ ] q-third")
	a.Press("enter")
	waitScreen(t, a, "dropped 2 queued message(s)", "1 queued", "↳ q-third")
	close(rel)
	waitScreen(t, a, "after the rest")
	idle(t, a)
	if m := lastMessage(fake, 1); !strings.Contains(m, "q-third") || strings.Contains(m, "q-first") {
		t.Fatalf("what ran after the turn: %s", m)
	}
}

func TestCompletion(t *testing.T) {
	d := newDaemon(t, fakellm.New(t))
	if err := os.WriteFile(filepath.Join(d.cwd, "notes.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(d.cwd, "docs"), 0o755); err != nil {
		t.Fatal(err)
	}
	a := open(t, d, tui.Options{})
	a.Resize(100, 40) // room for all of /help

	a.Type("/reasoning hi") // test-model's levels: before the switch to other-model, which has none
	a.Press("tab")
	waitDock(t, a, "/reasoning high ")
	clearPrompt(t, a)

	a.Type("/mo")
	waitScreen(t, a, "/model", "Change the model for this session")
	a.Press("tab")
	waitDock(t, a, "/model ")
	waitGone(t, a, "Change the model for this session")
	a.Type("oth")
	waitScreen(t, a, "other-model")
	a.Press("tab")
	waitDock(t, a, "/model other-model ")
	a.Press("enter")
	waitScreen(t, a, "model: other-model (from the next turn)")

	a.Type("/perm y")
	a.Press("tab")
	waitDock(t, a, "/perm yolo ")
	clearPrompt(t, a)

	a.Type("see ./no")
	if s := a.Screen(); strings.Contains(s, "notes.txt") {
		t.Fatalf("a path list opened while typing:\n%s", s)
	}
	a.Press("tab")
	waitDock(t, a, "see ./notes.txt ")
	clearPrompt(t, a)

	a.Type("./do")
	a.Press("tab")
	waitDock(t, a, "./docs/")
	a.Type("x") // the cursor's cell reads as a space: what follows shows there is none
	waitDock(t, a, "./docs/x")
	clearPrompt(t, a)

	a.Type("/hel")
	waitScreen(t, a, "List the commands")
	a.Press("enter") // accepts, does not send
	waitDock(t, a, "/help ")
	if strings.Contains(a.Screen(), "commands:") {
		t.Fatal("enter on an open list sent the command")
	}
	a.Press("enter")
	waitScreen(t, a, "commands:", "/compact [focus topic | --preview] — Condense older turns into a summary",
		"/new — Open a new session", "ctrl+p opens the palette")
}

func TestHistorySuggestions(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "first"}, fakellm.Turn{Content: "second"})
	d := newDaemon(t, fake)
	if err := tui.AppendHistory(filepath.Join(d.home, ".son_of_anton_history"), "explain the relay worker"); err != nil {
		t.Fatal(err)
	}
	a := open(t, d, tui.Options{})
	a.Type("expl")
	waitDock(t, a, "explain the relay worker")
	a.Press("right", "enter")
	waitScreen(t, a, "▎  explain the relay worker", "first")
	idle(t, a)
	a.Type("expl")
	waitDock(t, a, "explain the relay worker")
	a.Press("enter")
	s := waitScreen(t, a, "second")
	if !hasLine(s, "▎  expl") {
		t.Fatalf("enter sent the suggestion rather than the draft:\n%s", s)
	}
}

// Tab and End take the suggestion too.
func TestTabAndEndTakeTheSuggestion(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "first"}, fakellm.Turn{Content: "second"})
	d := newDaemon(t, fake)
	if err := tui.AppendHistory(filepath.Join(d.home, ".son_of_anton_history"), "explain the relay worker"); err != nil {
		t.Fatal(err)
	}
	a := open(t, d, tui.Options{})
	for i, key := range []string{"tab", "end"} {
		a.Type("expl")
		waitDock(t, a, "explain the relay worker")
		a.Press(key, "enter")
		waitScreen(t, a, []string{"first", "second"}[i])
		idle(t, a)
		if n := len(fake.Messages(i)); n == 0 || !strings.Contains(string(fake.Messages(i)[n-1]), "explain the relay worker") {
			t.Fatalf("%s did not take the suggestion: %s", key, fake.Messages(i)[n-1])
		}
	}
}

func TestThePalette(t *testing.T) {
	fake := fakellm.New(t)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	a.Resize(100, 40)
	a.Press("ctrl+p")
	waitScreen(t, a, "Commands", "/new", "Session · Open a new session", "Configuration · Change the model for this session")
	a.Type("comp")
	waitGone(t, a, "/new")
	a.Press("enter")
	waitGone(t, a, "Session · ")
	waitDock(t, a, "/compact ")
	a.Press("ctrl+p")
	waitScreen(t, a, "Commands")
	a.Press("esc")
	waitGone(t, a, "Session · ")
	waitDock(t, a, "/compact ")
	if fake.Count() != 0 || strings.Contains(a.Screen(), "compacting") {
		t.Fatal("picking from the palette ran the command")
	}
}

func TestTheEditor(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "got it"})
	d := newDaemon(t, fake)
	dir := t.TempDir()
	send1 := filepath.Join(dir, "append.sh")
	if err := os.WriteFile(send1, []byte("#!/bin/sh\nprintf 'from the editor\\n' >> \"$1\"\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	empty := filepath.Join(dir, "empty.sh")
	ran := filepath.Join(dir, "ran")
	if err := os.WriteFile(empty, []byte("#!/bin/sh\n: > \"$1\"\ntouch "+ran+"\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("EDITOR", "")
	t.Setenv("VISUAL", send1)
	a := open(t, d, tui.Options{})
	a.Type("draft one")
	a.Press("ctrl+g")
	waitScreen(t, a, "▎  draft one", "from the editor", "got it")
	if m := lastMessage(fake, 0); !strings.Contains(m, `draft one\nfrom the editor`) || strings.Contains(m, "#!") {
		t.Fatalf("what the editor sent: %s", m)
	}
	idle(t, a)

	t.Setenv("VISUAL", empty)
	a.Type("draft two")
	a.Press("ctrl+g")
	for deadline := time.Now().Add(waitFor); ; time.Sleep(20 * time.Millisecond) {
		if _, err := os.Stat(ran); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the editor never ran")
		}
	}
	waitDock(t, a, "draft two")
	time.Sleep(300 * time.Millisecond)
	if fake.Count() != 1 {
		t.Fatal("an emptied file was sent")
	}
}
