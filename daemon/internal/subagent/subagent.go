// Package subagent is the task tool: it runs a subagent as a child session,
// opencode v1.19.5's model (packages/opencode/src/tool/task.ts). The texts
// are opencode's with the tool names mapped to this daemon's; the server
// supplies the Spawner that makes and runs the child session.
package subagent

import (
	"context"
	_ "embed"
	"encoding/json"
	"sort"
	"strings"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

//go:embed task.txt
var taskText string

//go:embed explore.txt
var exploreText string

// BackgroundDescription, BackgroundStarted and BackgroundUpdated are
// opencode's (tool/task.ts:25-41).
const (
	BackgroundDescription = "Background mode: background=true launches the subagent asynchronously and returns immediately. " +
		"Foreground is the default; use it when you need the result before continuing. " +
		"Use background only for independent work that can run while you continue elsewhere. " +
		"You will be notified automatically when it finishes."
	BackgroundStarted = "The task is working in the background. You will be notified automatically when it finishes.\n" +
		"DO NOT sleep, poll for progress, ask the task for status, or duplicate this task's work — avoid working with the same files or topics it is using.\n" +
		"Work on non-overlapping tasks, or briefly tell the user what you launched and end your response."
	BackgroundUpdated = "Additional context sent to the running background task.\n" +
		"The task is still working in the background. You will be notified automatically when it finishes.\n" +
		"DO NOT sleep, poll for progress, ask the task for status, or duplicate this task's work — avoid working with the same files or topics it is using.\n" +
		"Work on non-overlapping tasks, or briefly tell the user what you sent and end your response."
)

// Type is one kind of subagent. Tools nil means every tool the child may have.
type Type struct {
	Name, Description, Prompt string
	Tools                     []string
}

// Builtin is the agent types: explore and general.
func Builtin() []Type {
	return []Type{
		{
			Name: "explore",
			Description: `Fast agent specialized for exploring codebases. Use this when you need to quickly find files by patterns (eg. "src/components/**/*.tsx"), ` +
				`search code for keywords (eg. "API endpoints"), or answer questions about the codebase (eg. "how do API endpoints work?"). ` +
				`When calling this agent, specify the desired thoroughness level: "quick" for basic searches, "medium" for moderate exploration, ` +
				`or "very thorough" for comprehensive analysis across multiple locations and naming conventions.`,
			Prompt: strings.TrimSpace(exploreText),
			Tools:  []string{"terminal", "read_file", "search_files"},
		},
		{
			Name:        "general",
			Description: "General-purpose agent for researching complex questions and executing multi-step tasks. Use this agent to execute multiple units of work in parallel.",
			Prompt: "You are a subagent: another agent gave you one task. Do it fully and on your own, " +
				"then reply with one final message that contains exactly what the task asked for; " +
				"that message is all the agent that called you will see.",
		},
	}
}

// Lookup finds a type by name.
func Lookup(types []Type, name string) (Type, bool) {
	for _, t := range types {
		if t.Name == name {
			return t, true
		}
	}
	return Type{}, false
}

// ChildMay says whether a subagent may have the tool: never task (depth is
// 1), todo, clarify (nobody answers) or memory.
func ChildMay(tool string) bool {
	switch tool {
	case "task", "memory", "clarify", "todo":
		return false
	}
	return true
}

// Description is the task tool's description: opencode's task.txt, the
// background paragraph when offered, then the available types by name.
func Description(types []Type, background bool) string {
	d := taskText
	if background {
		d += "\n\n" + BackgroundDescription
	}
	sorted := append([]Type(nil), types...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Name < sorted[j].Name })
	lines := make([]string, len(sorted))
	for i, t := range sorted {
		lines[i] = "- " + t.Name + ": " + t.Description
	}
	return d + "\nAvailable agent types and the tools they have access to:\n" + strings.Join(lines, "\n")
}

// Render is opencode's renderOutput (tool/task.ts:64-79).
func Render(id, state, summary, text string) string {
	tag := "task_result"
	if state == "error" {
		tag = "task_error"
	}
	parts := []string{`<task id="` + id + `" state="` + state + `">`}
	if summary != "" {
		parts = append(parts, "<summary>"+summary+"</summary>")
	}
	return strings.Join(append(parts, "<"+tag+">", text, "</"+tag+">", "</task>"), "\n")
}

// Request is one task call.
type Request struct {
	Description, Prompt, TaskID string
	Type                        Type
	Background                  bool
	CallID                      string
}

// Result is a spawner's answer. State is completed, running or error.
type Result struct{ ID, State, Summary, Text string }

// Spawner runs a request as a child session of the calling session.
type Spawner func(ctx context.Context, env *tools.Env, req Request) (Result, error)

type taskTool struct {
	types      []Type
	background bool
	spawn      Spawner
}

// Tool is the task tool. background offers the background parameter.
func Tool(types []Type, background bool, spawn Spawner) tools.Tool {
	return taskTool{types, background, spawn}
}

// Concurrent says task calls of one message run together.
func (taskTool) Concurrent() bool { return true }

func (t taskTool) Schema() provider.Tool {
	props := map[string]any{
		"description":   map[string]string{"type": "string", "description": "A short (3-5 words) description of the task"},
		"prompt":        map[string]string{"type": "string", "description": "The task for the agent to perform"},
		"subagent_type": map[string]string{"type": "string", "description": "The type of specialized agent to use for this task"},
		"task_id": map[string]string{"type": "string", "description": "This should only be set if you mean to resume a previous task " +
			"(you can pass a prior task_id and the task will continue the same subagent session as before instead of creating a fresh one)"},
	}
	if t.background {
		props["background"] = map[string]string{"type": "boolean", "description": "Run the agent in the background. You will be notified when it completes. " +
			"DO NOT sleep, poll, or proactively check on its progress"}
	}
	params, _ := json.Marshal(map[string]any{
		"type": "object", "properties": props, "required": []string{"description", "prompt", "subagent_type"},
	})
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "task", Description: Description(t.types, t.background), Parameters: params,
	}}
}

func (t taskTool) Run(ctx context.Context, env *tools.Env, args json.RawMessage) (string, error) {
	var p struct {
		Description  string `json:"description"`
		Prompt       string `json:"prompt"`
		SubagentType string `json:"subagent_type"`
		TaskID       string `json:"task_id"`
		Background   bool   `json:"background"`
	}
	if err := json.Unmarshal(args, &p); err != nil {
		return "invalid arguments: " + err.Error(), nil
	}
	if p.Description == "" || p.Prompt == "" || p.SubagentType == "" {
		return "invalid arguments: description, prompt and subagent_type are required", nil
	}
	typ, ok := Lookup(t.types, p.SubagentType)
	if !ok {
		return "Unknown agent type: " + p.SubagentType + " is not a valid agent type", nil
	}
	res, err := t.spawn(ctx, env, Request{Description: p.Description, Prompt: p.Prompt, TaskID: p.TaskID, Type: typ,
		Background: t.background && p.Background, CallID: env.CallID})
	if err != nil {
		return Render(res.ID, "error", "", err.Error()), nil
	}
	return Render(res.ID, res.State, res.Summary, res.Text), nil
}
