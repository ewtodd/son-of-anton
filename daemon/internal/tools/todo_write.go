package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

const (
	todoMaxItems   = 256
	todoMaxContent = 4000
	todoTruncated  = "... [truncated]"
)

// todoItem is one entry of the session's checklist. The list lives in the
// transcript: each TodoWrite result holds all of it, and the TUI's sidebar
// shows the latest one, a resumed session's included.
type todoItem struct {
	Content    string `json:"content"`
	Status     string `json:"status"`
	ActiveForm string `json:"activeForm"`
}

func capTodoContent(s string) string {
	if r := []rune(s); len(r) > todoMaxContent {
		return string(r[:todoMaxContent]) + todoTruncated
	}
	return s
}

type todoWriteTool struct{}

func (todoWriteTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "TodoWrite",
		Description: "Keep this session's checklist, which the user sees beside the conversation. " +
			"It pays for work with three or more steps, or a message asking for several things; a single quick task needs none. " +
			"Each call sends the whole list, which replaces the one before. " +
			"Give every entry its content (what to do, as an instruction), its activeForm (the same as what is happening, as in \"Running the tests\") " +
			"and its status: pending, in_progress or completed. Keep exactly one entry in_progress while you work, " +
			"mark an entry completed as soon as it is done and checked (not in a batch at the end), and keep one in_progress " +
			"if it failed or is blocked, adding an entry for what stands in the way. Remove entries that no longer apply.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "todos": {"type": "array", "description": "The complete, updated list.", "items": {
      "type": "object",
      "properties": {
        "content": {"type": "string", "minLength": 1, "description": "The step, as an instruction."},
        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
        "activeForm": {"type": "string", "minLength": 1, "description": "The step as it reads while it is being done."}
      },
      "required": ["content", "status", "activeForm"]
    }}
  },
  "required": ["todos"]
}`),
	}}
}

func (todoWriteTool) Run(_ context.Context, _ *Env, raw json.RawMessage) (string, error) {
	var a struct {
		Todos json.RawMessage `json:"todos"`
	}
	if err := json.Unmarshal(raw, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	if len(a.Todos) == 0 || string(a.Todos) == "null" {
		return errorResult("todos is required: the whole list, each entry {content, status, activeForm}"), nil
	}
	data := []byte(a.Todos)
	var asString string
	if json.Unmarshal(data, &asString) == nil {
		data = []byte(asString)
	}
	var list []map[string]any
	if err := json.Unmarshal(data, &list); err != nil {
		return errorResult("todos must be a list of {content, status, activeForm} entries"), nil
	}
	items := make([]todoItem, 0, len(list))
	for i, t := range list {
		it := todoItem{Content: capTodoContent(argStr(t, "content")), Status: strings.ToLower(argStr(t, "status")),
			ActiveForm: capTodoContent(argStr(t, "activeForm"))}
		if it.Content == "" {
			return errorResult(fmt.Sprintf("entry %d has no content", i+1)), nil
		}
		switch it.Status {
		case "pending", "in_progress", "completed":
		default:
			return errorResult(fmt.Sprintf("entry %d (%s) has status %q; a status is pending, in_progress or completed", i+1, it.Content, it.Status)), nil
		}
		if it.ActiveForm == "" {
			it.ActiveForm = it.Content
		}
		items = append(items, it)
	}
	if len(items) > todoMaxItems {
		items = items[:todoMaxItems]
	}
	sum := map[string]int{"total": len(items), "pending": 0, "in_progress": 0, "completed": 0}
	for _, it := range items {
		sum[it.Status]++
	}
	return jsonResult(map[string]any{"todos": items, "summary": sum})
}
