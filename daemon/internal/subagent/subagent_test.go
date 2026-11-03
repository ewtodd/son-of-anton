package subagent_test

// Phase 4a: the task tool's text, types and result format.

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/subagent"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

func TestTheBuiltinTypes(t *testing.T) {
	types := subagent.Builtin()
	explore, ok := subagent.Lookup(types, "explore")
	if !ok || !strings.HasPrefix(explore.Description, "Read-only searcher for finding things in a codebase") ||
		!strings.HasPrefix(explore.Prompt, "You are a read-only code explorer.") {
		t.Fatalf("explore = %+v", explore)
	}
	if strings.Join(explore.Tools, ",") != "terminal,read_file,search_files" {
		t.Fatalf("explore tools = %v", explore.Tools)
	}
	general, ok := subagent.Lookup(types, "general")
	if !ok || !strings.HasPrefix(general.Description, "Full-capability agent for any job that needs several steps") || general.Prompt == "" || general.Tools != nil {
		t.Fatalf("general = %+v; every tool its parent may give it", general)
	}
	if _, ok := subagent.Lookup(types, "nope"); ok {
		t.Fatal("unknown type")
	}
	for _, name := range []string{"task", "memory", "clarify", "todo"} {
		if subagent.ChildMay(name) {
			t.Errorf("a child may not use %s", name)
		}
	}
	if !subagent.ChildMay("terminal") {
		t.Error("a child may use terminal")
	}
}

func TestTheDescriptionCoversTheTool(t *testing.T) {
	d := subagent.Description(subagent.Builtin(), false)
	for _, want := range []string{
		"Hand a self-contained job to a subagent",
		"Pick the subagent by name with subagent_type.",
		"Do the job yourself when it is small",
		"Several task calls in one message run at the same time.",
		"The result carries a task_id.",
		"\nSubagent types:\n- explore: Read-only searcher for finding things in a codebase",
	} {
		if !strings.Contains(d, want) {
			t.Errorf("description lacks %q", want)
		}
	}
	if strings.Index(d, "- explore:") > strings.Index(d, "- general:") {
		t.Error("agents are listed by name")
	}
	if strings.Contains(d, "Background option:") {
		t.Error("the background paragraph appears only when background is offered")
	}
	if !strings.Contains(subagent.Description(subagent.Builtin(), true), "Background option: set background to true") {
		t.Error("the background paragraph")
	}
}

func TestResultsRender(t *testing.T) {
	got := subagent.Render("20261102_100000_aaaaaa", "completed", "", "found it")
	want := "<task id=\"20261102_100000_aaaaaa\" state=\"completed\">\nResult:\nfound it\n</task>"
	if got != want {
		t.Fatalf("Render = %q", got)
	}
	got = subagent.Render("x", "error", "Failed in the background: look", "boom")
	want = "<task id=\"x\" state=\"error\">\nFailed in the background: look\nError:\nboom\n</task>"
	if got != want {
		t.Fatalf("Render error = %q", got)
	}
}

func TestTheToolCallsTheSpawner(t *testing.T) {
	var got subagent.Request
	tool := subagent.Tool(subagent.Builtin(), true, func(_ context.Context, _ *tools.Env, req subagent.Request) (subagent.Result, error) {
		got = req
		return subagent.Result{ID: "child-1", State: "completed", Text: "the answer"}, nil
	})
	s := tool.Schema()
	var params struct {
		Properties map[string]any `json:"properties"`
		Required   []string       `json:"required"`
	}
	if err := json.Unmarshal(s.Function.Parameters, &params); err != nil {
		t.Fatal(err)
	}
	if s.Function.Name != "task" || strings.Join(params.Required, ",") != "description,prompt,subagent_type" ||
		params.Properties["task_id"] == nil || params.Properties["background"] == nil {
		t.Fatalf("schema = %s", s.Function.Parameters)
	}
	if c, ok := tool.(interface{ Concurrent() bool }); !ok || !c.Concurrent() {
		t.Fatal("task calls in one message run concurrently")
	}
	out, err := tool.Run(context.Background(), &tools.Env{}, json.RawMessage(`{"description":"Find it","prompt":"look for it","subagent_type":"explore"}`))
	if err != nil || out != subagent.Render("child-1", "completed", "", "the answer") {
		t.Fatalf("Run = %q, %v", out, err)
	}
	if got.Description != "Find it" || got.Prompt != "look for it" || got.Type.Name != "explore" || got.Background || got.TaskID != "" {
		t.Fatalf("request = %+v", got)
	}
	out, _ = tool.Run(context.Background(), &tools.Env{}, json.RawMessage(`{"description":"x","prompt":"y","subagent_type":"nope"}`))
	if !strings.Contains(out, "There is no subagent type called nope") {
		t.Fatalf("unknown type: %q", out)
	}
}

func TestTheTaskToolAndItsParametersAreDescribed(t *testing.T) {
	s := subagent.Tool(subagent.Builtin(), true, nil).Schema()
	if strings.TrimSpace(s.Function.Description) == "" {
		t.Fatal("the task tool has no description")
	}
	var params struct {
		Properties map[string]struct {
			Description string `json:"description"`
		} `json:"properties"`
	}
	if err := json.Unmarshal(s.Function.Parameters, &params); err != nil {
		t.Fatal(err)
	}
	for name, p := range params.Properties {
		if strings.TrimSpace(p.Description) == "" {
			t.Errorf("task parameter %s has no description", name)
		}
	}
}
