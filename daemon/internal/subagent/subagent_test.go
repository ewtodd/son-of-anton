package subagent_test

// Phase 4a: the task tool's text, types and result format, ported from
// opencode v1.19.5 (packages/opencode/src/tool/task.ts, task.txt,
// src/agent/agent.ts; reference clone ~/.cache/claude-ref/opencode-v1.19.5).

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
	if !ok || !strings.HasPrefix(explore.Description, "Fast agent specialized for exploring codebases.") ||
		!strings.HasPrefix(explore.Prompt, "You are a file search specialist.") {
		t.Fatalf("explore = %+v", explore)
	}
	if strings.Join(explore.Tools, ",") != "terminal,read_file,search_files" {
		t.Fatalf("explore tools = %v", explore.Tools)
	}
	general, ok := subagent.Lookup(types, "general")
	if !ok || !strings.HasPrefix(general.Description, "General-purpose agent for researching complex questions") || general.Prompt == "" || general.Tools != nil {
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

func TestTheDescriptionIsOpencodes(t *testing.T) {
	d := subagent.Description(subagent.Builtin(), false)
	for _, want := range []string{
		"Launch a new agent to handle complex, multistep tasks autonomously.",
		"When using the Task tool, you must specify a subagent_type parameter to select which agent type to use.",
		"If you are searching for a specific class definition like \"class Foo\", use search_files instead, to find the match more quickly",
		"1. Launch multiple agents concurrently whenever possible, to maximize performance; to do that, use a single message with multiple tool uses",
		"The output includes a task_id you can reuse later to continue the same subagent session.",
		"\nAvailable agent types and the tools they have access to:\n- explore: Fast agent specialized for exploring codebases.",
	} {
		if !strings.Contains(d, want) {
			t.Errorf("description lacks %q", want)
		}
	}
	if strings.Index(d, "- explore:") > strings.Index(d, "- general:") {
		t.Error("agents are listed by name")
	}
	for _, old := range []string{"Glob tool", "Grep tool", "Read tool"} {
		if strings.Contains(d, old) {
			t.Errorf("an opencode tool name survived: %q", old)
		}
	}
	if strings.Contains(d, "Background mode:") {
		t.Error("the background paragraph appears only when background is offered")
	}
	if !strings.Contains(subagent.Description(subagent.Builtin(), true), "Background mode: background=true launches the subagent asynchronously and returns immediately.") {
		t.Error("the background paragraph, verbatim")
	}
}

func TestResultsRenderLikeOpencode(t *testing.T) {
	got := subagent.Render("20261102_100000_aaaaaa", "completed", "", "found it")
	want := "<task id=\"20261102_100000_aaaaaa\" state=\"completed\">\n<task_result>\nfound it\n</task_result>\n</task>"
	if got != want {
		t.Fatalf("Render = %q", got)
	}
	got = subagent.Render("x", "error", "Background task failed: look", "boom")
	want = "<task id=\"x\" state=\"error\">\n<summary>Background task failed: look</summary>\n<task_error>\nboom\n</task_error>\n</task>"
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
	if !strings.Contains(out, "Unknown agent type: nope is not a valid agent type") {
		t.Fatalf("unknown type: %q", out)
	}
}
