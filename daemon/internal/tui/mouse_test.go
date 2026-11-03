package tui_test

// Phase 9d: the mouse folds rows, copies a selection and scrolls
// (daemon/phases/09d-tui-subagents-mouse.md).

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/tui"
)

// rowOf is the screen row of the first line containing part, and the column
// part starts at; -1 when absent.
func rowOf(screen, part string) (int, int) {
	for i, l := range strings.Split(screen, "\n") {
		if j := strings.Index(l, part); j >= 0 {
			return i, len([]rune(l[:j]))
		}
	}
	return -1, -1
}

// waitRows waits until exactly n lines contain part.
func waitRows(t *testing.T, a *tui.App, part string, n int) string {
	t.Helper()
	deadline := time.Now().Add(waitFor)
	for {
		s := a.Screen()
		if countLines(s, part) == n {
			return s
		}
		if time.Now().After(deadline) {
			t.Fatalf("%d lines never held %q:\n%s", n, part, s)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func countLines(screen, part string) int {
	n := 0
	for _, l := range strings.Split(screen, "\n") {
		if strings.Contains(l, part) {
			n++
		}
	}
	return n
}

func TestClicksFoldAndUnfold(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Reasoning: "think one\nthink two", ToolCalls: []fakellm.ToolCall{terminal("c1", "echo "+strings.Repeat("y", 110))}},
		fakellm.Turn{Content: "done"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{Yolo: true})
	a.Resize(100, 40)
	send(a, "go")
	waitScreen(t, a, "done", "▶ reasoning · 2 lines")
	s := idle(t, a)

	r, c := rowOf(s, "▶ reasoning")
	a.Click(c+2, r)
	s = waitScreen(t, a, "▼ reasoning", "think one", "think two")
	r, c = rowOf(s, "▼ reasoning")
	a.Click(c+2, r)
	waitScreen(t, a, "▶ reasoning · 2 lines")
	waitGone(t, a, "think one")

	s = a.Screen()
	if n := countLines(s, "yyyy"); n != 1 || !strings.Contains(lineWith(s, "$ echo"), "…") {
		t.Fatalf("a long tool row is not folded to one line:\n%s", s)
	}
	r, c = rowOf(s, "$ echo")
	a.Click(c+4, r)
	s = waitRows(t, a, "yyyy", 2)
	var cont string // the continuation row: the 2-column inset, then the 2-column indent
	for _, l := range strings.Split(s, "\n") {
		if strings.Contains(l, "yyyy") && !strings.Contains(l, "$ echo") {
			cont = l
		}
	}
	if strings.Contains(lineWith(s, "$ echo"), "…") || !strings.HasPrefix(cont, "    y") {
		t.Fatalf("an unfolded row is still cut, or its continuation not indented:\n%s", s)
	}
	r, c = rowOf(s, "$ echo")
	a.Click(c+4, r)
	s = waitRows(t, a, "yyyy", 1)
	checkFrame(t, s, 100, 40)
}

func TestADragCopiesTheSelection(t *testing.T) {
	d := newDaemon(t, fakellm.New(t))
	a := open(t, d, tui.Options{})
	send(a, "!echo a line worth selecting")
	// The echo of the ! line holds the same words: wait for the output's own row.
	row, col := -1, 0
	for deadline := time.Now().Add(waitFor); row < 0; time.Sleep(20 * time.Millisecond) {
		for i, l := range strings.Split(a.Screen(), "\n") {
			if strings.TrimSpace(l) == "a line worth selecting" {
				row, col = i, len([]rune(l))-len([]rune(strings.TrimLeft(l, " ")))
			}
		}
		if row < 0 && time.Now().After(deadline) {
			t.Fatalf("no output row:\n%s", a.Screen())
		}
	}
	if a.Copied() != "" {
		t.Fatal("something was copied before any drag")
	}
	a.Drag(col, row, col+19, row)
	waitScreen(t, a, "copied 19 chars")
	if got := a.Copied(); got != "a line worth select" {
		t.Fatalf("copied %q", got)
	}
}

func TestTheWheelScrolls(t *testing.T) {
	var lines strings.Builder
	for i := range 120 {
		fmt.Fprintf(&lines, "line %03d\n", i)
	}
	fake := fakellm.New(t, fakellm.Turn{Content: lines.String()})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "count")
	waitScreen(t, a, "line 119")
	idle(t, a)
	a.Wheel(10, 5, -10)
	waitGone(t, a, "line 119")
	send(a, "/perm")
	if strings.Contains(a.Screen(), "permission mode:") {
		t.Fatal("output moved a view scrolled up with the wheel")
	}
	a.Wheel(10, 5, 100)
	waitScreen(t, a, "permission mode:")
	send(a, "/status")
	waitScreen(t, a, "session "+a.Session()+" ·")
}
