package tui_test

// The /model picker lists only models the gateway serves, and a session whose
// model is down is warned. Picking a model opens its reasoning picker next,
// tool rows expand to their output, and the todo tool's checklist stays in the
// sidebar (phase 9a and 10a).

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/tui"
)

func setStatus(fake *fakellm.Server, serving, unavailable []string) {
	ids := append(append([]string(nil), serving...), unavailable...)
	fake.SetModels(ids...)
	for _, id := range serving {
		fake.SetModelMeta(id, map[string]any{"status": "serving"})
	}
	for _, id := range unavailable {
		fake.SetModelMeta(id, map[string]any{"status": "unavailable"})
	}
}

func TestThePickerListsOnlyServedModels(t *testing.T) {
	fake := fakellm.New(t)
	setStatus(fake, []string{"other-model"}, []string{"test-model", "down-model"})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})

	waitScreen(t, a, "test-model is not being served; pick another model")

	send(a, "/model")
	s := waitScreen(t, a, "Switch model", "other-model", "test-model is not being served; pick another model")
	if strings.Contains(s, "test-model  ●") {
		t.Fatalf("the down current model is in the picker:\n%s", s)
	}
	if strings.Contains(s, "down-model") {
		t.Fatalf("a down model is in the picker:\n%s", s)
	}
	a.Press("enter")
	waitScreen(t, a, "model: other-model (from the next turn)")
	if sess, _ := peek(t, d, a.Session()); sess["model"] != "other-model" {
		t.Fatalf("session model = %v", sess["model"])
	}
}

// Choosing a model with reasoning levels opens the reasoning picker right
// after, without a second command.
func TestPickingAModelOpensItsReasoningPicker(t *testing.T) {
	fake := fakellm.New(t)
	setStatus(fake, []string{"test-model", "other-model"}, nil)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})

	send(a, "/model")
	waitScreen(t, a, "Switch model", "test-model  ●", "other-model", "reasoning: low, high")
	a.Press("enter") // test-model, the current one, has low/high
	waitScreen(t, a, "Reasoning  ·  now default", "the configured default", "low", "high")
	if sess, _ := peek(t, d, a.Session()); sess["model"] != "test-model" {
		t.Fatalf("after /model the session model = %v", sess["model"])
	}
	a.Press("3")
	waitScreen(t, a, "reasoning: high (from the next turn)")
	if sess, _ := peek(t, d, a.Session()); sess["reasoning_effort"] != "high" {
		t.Fatalf("reasoning effort = %v", sess["reasoning_effort"])
	}
}

func toolCall(id, name, args string) fakellm.ToolCall {
	return fakellm.ToolCall{ID: id, Name: name, Arguments: args}
}

// A tool row folds to one line until expanded; its output is then visible.
func TestAToolRowExpandsToItsOutput(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{toolCall("c1", "read_file", `{"path":"note.txt"}`)}},
		fakellm.Turn{Content: "done"},
	)
	d := newDaemon(t, fake)
	if err := os.WriteFile(filepath.Join(d.cwd, "note.txt"), []byte("visible-content"), 0o600); err != nil {
		t.Fatal(err)
	}
	a := open(t, d, tui.Options{})
	send(a, "read the note")
	s := waitScreen(t, a, "note.txt", "done")
	if strings.Contains(s, "visible-content") {
		t.Fatalf("tool output shown before expanding:\n%s", s)
	}
	a.Press("ctrl+t")
	waitScreen(t, a, "visible-content")
}

// The todo tool's complete list stays in the sidebar.
func TestTodosStayInTheSidebar(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{toolCall("c1", "todo", `{"todos":[{"id":"1","content":"run the tests","status":"completed"},{"id":"2","content":"write docs","status":"pending"}]}`)}},
		fakellm.Turn{Content: "done"},
	)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	a.Resize(160, 40)
	send(a, "plan the work")
	waitScreen(t, a, "todo", "✓ run the tests", "· write docs")
}
