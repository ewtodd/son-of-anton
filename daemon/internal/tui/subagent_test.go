package tui_test

// Phase 9d: task rows and the subagent view (daemon/phases/09d-tui-subagents-mouse.md).

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/tui"
)

// explorePrompt is in an explore child's system prompt, never the parent's.
const explorePrompt = "You are a read-only code explorer."

func taskCall(id, args string) fakellm.ToolCall {
	return fakellm.ToolCall{ID: id, Name: "task", Arguments: args}
}

var doneDetail = regexp.MustCompile(`↳ 1 toolcall · \d+(ms|\.\ds)`)

func TestATaskRowAndItsChildsView(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{taskCall("t1", `{"description":"Find the thing","prompt":"look for the thing","subagent_type":"explore"}`)}},
		fakellm.Turn{When: explorePrompt, ToolCalls: []fakellm.ToolCall{terminal("a1", "echo child-ran")}},
		fakellm.Turn{When: explorePrompt, Content: "found it in the child", PromptTokens: 1200, CompletionTokens: 34},
		fakellm.Turn{Content: "the parent is done"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "find the thing")
	s := waitScreen(t, a, "the parent is done", "Explore Task — Find the thing", "ctrl+x down view subagents")
	if !doneDetail.MatchString(s) {
		t.Fatalf("the task row's detail:\n%s", s)
	}
	if strings.Contains(s, "echo child-ran") || strings.Contains(s, "found it in the child") {
		t.Fatalf("the child's own rows reached the parent's feed:\n%s", s)
	}
	idle(t, a)

	a.Press("ctrl+x", "down")
	s = waitScreen(t, a, "Explore", "(1 of 1)", "1.2K", "Parent ↑", "Prev ←", "Next →", "found it in the child", "$ echo child-ran")
	checkFrame(t, s, 100, 30)
	if strings.Contains(s, "Message Son of Anton…") || strings.Contains(s, "the parent is done") {
		t.Fatalf("the child view shows the prompt or the parent's feed:\n%s", s)
	}
	a.Press("esc")
	if !strings.Contains(a.Screen(), "Parent ↑") {
		t.Fatal("esc left the child view")
	}
	a.Press("up")
	waitScreen(t, a, "Message Son of Anton…", "the parent is done")
	waitGone(t, a, "Parent ↑")

	a.Press("ctrl+x")
	a.Type("x") // ends the leader: an ordinary key
	a.Press("down")
	if strings.Contains(a.Screen(), "Parent ↑") {
		t.Fatal("down after the leader ended opened a child")
	}
}

func TestSteppingBetweenSiblings(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{
			taskCall("t1", `{"description":"First look","prompt":"look one","subagent_type":"explore"}`),
			taskCall("t2", `{"description":"Second look","prompt":"look two","subagent_type":"explore"}`),
		}},
		fakellm.Turn{When: "look one", Content: "answer one"},
		fakellm.Turn{When: "look two", Content: "answer two"},
		fakellm.Turn{Content: "both done"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "look twice")
	waitScreen(t, a, "both done")
	idle(t, a)
	// The two calls run at once, so which child the daemon made first is a
	// race: the view opens on the newest, whichever that is, and steps
	// between the two, wrapping.
	a.Press("ctrl+x", "down")
	newest, older := "", ""
	for deadline := time.Now().Add(waitFor); newest == ""; time.Sleep(20 * time.Millisecond) {
		switch s := a.Screen(); {
		case !strings.Contains(s, "(2 of 2)"):
		case strings.Contains(s, "answer one"):
			newest, older = "answer one", "answer two"
		case strings.Contains(s, "answer two"):
			newest, older = "answer two", "answer one"
		}
		if newest == "" && time.Now().After(deadline) {
			t.Fatalf("the newest child's view never showed its answer:\n%s", a.Screen())
		}
	}
	waitScreen(t, a, "(2 of 2)", newest)
	a.Press("right")
	waitScreen(t, a, "(1 of 2)", older)
	a.Press("right")
	waitScreen(t, a, "(2 of 2)", newest)
	a.Press("left")
	waitScreen(t, a, "(1 of 2)", older)
	a.Press("up")
	waitScreen(t, a, "Message Son of Anton…")
}

func TestARunningChildStreamsIntoItsView(t *testing.T) {
	rel := make(chan struct{})
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{taskCall("t1", `{"description":"Slow look","prompt":"look slowly","subagent_type":"explore"}`)}},
		fakellm.Turn{When: explorePrompt, Content: "partial then the rest", ChunkRunes: 8, Release: rel},
		fakellm.Turn{Content: "parent wrapped up"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "look slowly")
	waitScreen(t, a, "Explore Task — Slow look", "ctrl+x down view subagents  ·  ")
	a.Press("ctrl+x", "down")
	waitScreen(t, a, "(1 of 1)", "partial")
	close(rel)
	waitScreen(t, a, "partial then the rest")
	a.Press("up")
	s := waitScreen(t, a, "parent wrapped up")
	idle(t, a)
	if strings.Contains(lineWith(s, "/help commands"), "view subagents") {
		t.Fatal("the status still offers subagents with none running")
	}
}

func TestAChildsApprovalNamesTheTask(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{taskCall("t1", `{"description":"Open it up","prompt":"open up f.txt","subagent_type":"general"}`)}},
		fakellm.Turn{When: "open up f.txt", ToolCalls: []fakellm.ToolCall{terminal("c1", "chmod 777 f.txt")}},
		fakellm.Turn{When: "open up f.txt", Content: "opened"},
		fakellm.Turn{Content: "all set"},
	)
	d := newDaemon(t, fake)
	if err := os.WriteFile(filepath.Join(d.cwd, "f.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	a := open(t, d, tui.Options{})
	send(a, "fix the perms")
	waitScreen(t, a, "General Task — Open it up: The agent asks to run a command", "chmod 777 f.txt")
	a.Press("1")
	waitScreen(t, a, "all set")
}

func TestAResumedSessionShowsItsTasks(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{taskCall("t1", `{"description":"Find the thing","prompt":"look for the thing","subagent_type":"explore"}`)}},
		fakellm.Turn{When: explorePrompt, ToolCalls: []fakellm.ToolCall{terminal("a1", "echo child-ran")}},
		fakellm.Turn{When: explorePrompt, Content: "found it in the child"},
		fakellm.Turn{Content: "the parent is done"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "find the thing")
	waitScreen(t, a, "the parent is done")
	idle(t, a)
	id := a.Session()
	a.Press("ctrl+q")
	waitExit(t, a)

	b := open(t, d, tui.Options{Resume: id})
	s := waitScreen(t, b, "Explore Task — Find the thing", "ctrl+x down view subagents", "the parent is done")
	if !doneDetail.MatchString(s) {
		t.Fatalf("a resumed task row's detail:\n%s", s)
	}
	b.Press("ctrl+x", "down")
	waitScreen(t, b, "(1 of 1)", "found it in the child")
}
