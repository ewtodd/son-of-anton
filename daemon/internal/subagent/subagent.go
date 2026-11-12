// Package subagent is the Task tool: it runs a subagent as a child session
// and returns the child's final message. The server supplies the Spawner
// that makes and runs the child session.
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

// BackgroundDescription is added to the Task tool's description when the
// run_in_background parameter is offered. BackgroundStarted and BackgroundUpdated
// are the results of a background launch and of a message sent to a
// background task that is still running.
const (
	BackgroundDescription = "Background option: set run_in_background to true and the Task call returns at once while the subagent keeps working. " +
		"The result arrives later as a message of its own. Leave it false (the default) when your next step needs the answer. " +
		"Use it only for work that is independent of what you do meanwhile."
	BackgroundStarted = "The subagent is running in the background and its result will be delivered here when it is done.\n" +
		"Do not wait for it, poll it, ask it for progress, or do its work yourself. Stay off the files and questions it covers.\n" +
		"Carry on with unrelated work, or tell the user briefly what you started and finish your reply."
	BackgroundUpdated = "Your message was passed to the subagent, which is still running in the background.\n" +
		"Its result will be delivered here when it is done. Do not wait for it, poll it, or do its work yourself.\n" +
		"Carry on with unrelated work, or tell the user briefly what you sent and finish your reply."
)

// Type is one kind of subagent. Tools nil means every tool the child may have.
type Type struct {
	Name, Description, Prompt string
	Tools                     []string
}

// Builtin is the agent types: Explore and general-purpose.
func Builtin() []Type {
	return []Type{
		{
			Name: "Explore",
			Description: "Read-only searcher for finding things in a codebase: files by name pattern, code by keyword, or an answer to a question about how something works. " +
				"In the prompt, give the depth you want: quick, medium or very thorough.",
			Prompt: strings.TrimSpace(exploreText),
			Tools:  []string{"Bash", "Read", "Glob", "Grep"},
		},
		{
			Name:        "general-purpose",
			Description: "Full-capability agent for any job that needs several steps: research, investigation, or changes to make. Use it to run separate jobs side by side.",
			Prompt: "Another agent has handed you one job. Carry it out completely without asking anything back; " +
				"nobody will answer. Finish with a single message holding what the job asked for. " +
				"The calling agent sees only that message.",
		},
	}
}

// Lookup finds a type by name, in any case. A child session stored before the
// types took Claude Code's names says "general" for general-purpose.
func Lookup(types []Type, name string) (Type, bool) {
	if name == "general" {
		name = "general-purpose"
	}
	for _, t := range types {
		if strings.EqualFold(t.Name, name) {
			return t, true
		}
	}
	return Type{}, false
}

// ChildMay says whether a subagent may have the tool: never Task (depth is
// 1), TodoWrite, AskUserQuestion (nobody answers) or memory.
func ChildMay(tool string) bool {
	switch tool {
	case "Task", "memory", "AskUserQuestion", "TodoWrite":
		return false
	}
	return true
}

// Description is the Task tool's description: the text in task.txt, the
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
	return d + "\nSubagent types:\n" + strings.Join(lines, "\n")
}

// Render is the task result as the model reads it. The first line,
// <task id=... state=...>, is read by the server and the TUI; the rest is for
// the model: an optional one-line summary, then the text under a label.
func Render(id, state, summary, text string) string {
	label := "Result:"
	if state == "error" {
		label = "Error:"
	}
	parts := []string{`<task id="` + id + `" state="` + state + `">`}
	if summary != "" {
		parts = append(parts, summary)
	}
	return strings.Join(append(parts, label, text, "</task>"), "\n")
}

// Request is one Task call.
type Request struct {
	Description, Prompt, TaskID string
	// Model pins the child's model; "" inherits the parent's (or, for a call
	// that names none, the instance's delegation model).
	Model      string
	Type       Type
	Background bool
	CallID     string
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

// Tool is the Task tool. background offers the run_in_background parameter.
func Tool(types []Type, background bool, spawn Spawner) tools.Tool {
	return taskTool{types, background, spawn}
}

// Concurrent says Task calls of one message run together.
func (taskTool) Concurrent() bool { return true }

func (t taskTool) Schema() provider.Tool {
	props := map[string]any{
		"description":   map[string]string{"type": "string", "description": "Three to five words naming the job (used as the subagent session's title)"},
		"prompt":        map[string]string{"type": "string", "description": "The full brief for the subagent: goal, context, and what its final message must contain"},
		"subagent_type": map[string]string{"type": "string", "description": "Which subagent to use, by name from the list in this tool's description"},
		"task_id":       map[string]string{"type": "string", "description": "A task_id from an earlier result. Give it to send this prompt to that same subagent, which remembers its earlier work; omit it to start a new one."},
		"model":         map[string]string{"type": "string", "description": "The model the subagent runs on, pinned for this task. Omit to inherit the parent's model (or the instance's delegation model)."},
	}
	if t.background {
		props["run_in_background"] = map[string]string{"type": "boolean", "description": "true to return immediately and receive the result later as a message; do not wait for or poll it"}
	}
	params, _ := json.Marshal(map[string]any{
		"type": "object", "properties": props, "required": []string{"description", "prompt", "subagent_type"},
	})
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "Task", Description: Description(t.types, t.background), Parameters: params,
	}}
}

func (t taskTool) Run(ctx context.Context, env *tools.Env, args json.RawMessage) (string, error) {
	var p struct {
		Description  string `json:"description"`
		Prompt       string `json:"prompt"`
		SubagentType string `json:"subagent_type"`
		TaskID       string `json:"task_id"`
		Model        string `json:"model"`
		Background   bool   `json:"run_in_background"`
	}
	if err := json.Unmarshal(args, &p); err != nil {
		return "invalid arguments: " + err.Error(), nil
	}
	if p.Description == "" || p.Prompt == "" || p.SubagentType == "" {
		return "description, prompt and subagent_type must all be given", nil
	}
	typ, ok := Lookup(t.types, p.SubagentType)
	if !ok {
		return "There is no subagent type called " + p.SubagentType + "; choose one from the list in this tool's description", nil
	}
	res, err := t.spawn(ctx, env, Request{Description: p.Description, Prompt: p.Prompt, TaskID: p.TaskID, Model: p.Model, Type: typ,
		Background: t.background && p.Background, CallID: env.CallID})
	if err != nil {
		return Render(res.ID, "error", "", err.Error()), nil
	}
	return Render(res.ID, res.State, res.Summary, res.Text), nil
}
