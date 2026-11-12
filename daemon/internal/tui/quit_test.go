package tui_test

import (
	"bytes"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/tui"
)

const quitAsk = "A turn is still running"

func TestQuitAsksWhileATurnRuns(t *testing.T) {
	rel := make(chan struct{})
	t.Cleanup(func() { close(rel) })
	fake := fakellm.New(t, fakellm.Turn{Content: "slow answer", ChunkRunes: 5, Release: rel})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "slow")
	waitScreen(t, a, "ctrl+c interrupt")
	id := a.Session()

	send(a, ":q")
	waitScreen(t, a, quitAsk, "1. Stay", "2. Stop the turn and quit", "3. Keep it running and quit")
	a.Press("esc")
	waitGone(t, a, quitAsk)
	send(a, "/quit")
	waitScreen(t, a, quitAsk)
	a.Press("enter") // the choice already highlighted stays
	waitGone(t, a, quitAsk)
	a.Press("ctrl+q")
	waitScreen(t, a, quitAsk)
	a.Press("2")
	waitExit(t, a)

	deadline := time.Now().Add(waitFor)
	for {
		sess, _ := peek(t, d, id)
		if sess["status"] == "idle" {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("stopping did not stop the turn: %v", sess)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func TestQuitAsksInFrontOfAnOpenApproval(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{{ID: "c1", Name: "Bash", Arguments: `{"command":"rm -rf ./scratch"}`}}},
		fakellm.Turn{Content: "denied, then"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "clean up")
	const approval = "The agent asks to run a command"
	waitScreen(t, a, approval)
	a.Press("ctrl+q")
	waitScreen(t, a, quitAsk)
	a.Press("ctrl+q") // asked already: one question is enough
	a.Press("1")      // stay: the 1 goes to this question, not the approval
	waitScreen(t, a, approval)
	a.Press("esc")
	waitScreen(t, a, "denied, then")
}

func TestLosingTheDriverClosesTheTUIAndSaysWhy(t *testing.T) {
	rel := make(chan struct{})
	fake := fakellm.New(t, fakellm.Turn{Content: "slow answer", ChunkRunes: 5, Release: rel})
	d := newDaemon(t, fake)
	var said bytes.Buffer
	a := open(t, d, tui.Options{Stderr: &said})
	send(a, "slow")
	waitScreen(t, a, "ctrl+c interrupt")
	id := a.Session()

	b := open(t, d, tui.Options{})
	send(b, "/resume "+id)
	waitExit(t, a) // no question: the turn is not this terminal's to decide about
	got := said.String()
	if !strings.Contains(got, "Another terminal took over session "+id) || !strings.Contains(got, "son-of-anton -r "+id) {
		t.Fatalf("the terminal says %q", got)
	}
	close(rel)
	waitScreen(t, b, "slow answer")
}

func TestQuitDoesNotAskWhenNothingRuns(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "the work is done"})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "work")
	// The reply first: the idle hint is on screen before the turn starts
	// too, and a :q in that gap either quit without asking and cut the
	// turn's request mid-send, or met the question and never exited.
	waitScreen(t, a, "the work is done")
	idle(t, a)
	send(a, ":q")
	waitExit(t, a)
}
