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

type todoItem struct {
	ID      string `json:"id"`
	Content string `json:"content"`
	Status  string `json:"status"`
}

func validTodoStatus(s string) bool {
	switch s {
	case "pending", "in_progress", "completed", "cancelled":
		return true
	}
	return false
}

func capTodoContent(s string) string {
	if r := []rune(s); len(r) > todoMaxContent {
		return string(r[:todoMaxContent]) + todoTruncated
	}
	return s
}

// rawTodo is one item as the model sent it; fields are read leniently.
type rawTodo map[string]any

func (r rawTodo) str(k string) string {
	v, ok := r[k]
	if !ok || v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return strings.TrimSpace(s)
	}
	return strings.TrimSpace(fmt.Sprint(v))
}

// dedupe keeps the last item per id, in that item's position.
func dedupeTodos(in []rawTodo) []rawTodo {
	last := map[string]int{}
	for i, t := range in {
		last[t.str("id")] = i
	}
	var out []rawTodo
	for i, t := range in {
		if t.str("id") == "" || last[t.str("id")] == i {
			out = append(out, t)
		}
	}
	return out
}

func newTodo(t rawTodo) todoItem {
	it := todoItem{ID: t.str("id"), Content: capTodoContent(t.str("content")), Status: strings.ToLower(t.str("status"))}
	if it.ID == "" {
		it.ID = "?"
	}
	if it.Content == "" {
		it.Content = "(no description)"
	}
	if !validTodoStatus(it.Status) {
		it.Status = "pending"
	}
	return it
}

// writeTodos replaces or merges the list and returns a copy.
func (s *State) writeTodos(in []rawTodo, merge bool) []todoItem {
	s.mu.Lock()
	defer s.mu.Unlock()
	in = dedupeTodos(in)
	if !merge {
		s.todos = nil
		for _, t := range in {
			s.todos = append(s.todos, newTodo(t))
		}
	} else {
		for _, t := range in {
			id := t.str("id")
			if id == "" {
				continue
			}
			idx := -1
			for i := range s.todos {
				if s.todos[i].ID == id {
					idx = i
				}
			}
			if idx < 0 {
				s.todos = append(s.todos, newTodo(t))
				continue
			}
			if c := t.str("content"); c != "" {
				s.todos[idx].Content = capTodoContent(c)
			}
			if st := strings.ToLower(t.str("status")); validTodoStatus(st) {
				s.todos[idx].Status = st
			}
		}
	}
	if len(s.todos) > todoMaxItems {
		s.todos = s.todos[:todoMaxItems]
	}
	return append([]todoItem{}, s.todos...)
}

// RestoreTodos loads the list from a stored todo tool result.
func (s *State) RestoreTodos(result string) error {
	var r struct {
		Todos []rawTodo `json:"todos"`
	}
	if err := json.Unmarshal([]byte(result), &r); err != nil {
		return err
	}
	if r.Todos == nil {
		return fmt.Errorf("not a todo result")
	}
	s.writeTodos(r.Todos, false)
	return nil
}

type todoTool struct{}

func (todoTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "todo",
		Description: "Keeps a checklist for the current session, for work that has about three or more steps or several separate requests. " +
			"With no arguments it just returns the list. To change it, pass todos: each entry is {id, content, status}, " +
			"where status is pending, in_progress, completed or cancelled. By default the new todos replace the whole list; " +
			"with merge set to true they are applied by id, updating entries that exist and appending the rest. " +
			"Order is priority. Have a single entry in_progress at a time, and mark one completed only once you have checked the work. " +
			"Every call returns the complete list.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "todos": {"type": "array", "description": "The entries to write. Leave out to only read the list.", "items": {
      "type": "object",
      "properties": {
        "id": {"type": "string"},
        "content": {"type": "string"},
        "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]}
      },
      "required": ["id", "content", "status"]
    }},
    "merge": {"type": "boolean", "description": "true applies the entries by id (update or append). false, the default, replaces the list."}
  }
}`),
	}}
}

func (todoTool) Run(_ context.Context, env *Env, raw json.RawMessage) (string, error) {
	var a struct {
		Todos json.RawMessage `json:"todos"`
		Merge bool            `json:"merge"`
	}
	if err := json.Unmarshal(raw, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	st := env.State
	var items []todoItem
	if len(a.Todos) == 0 || string(a.Todos) == "null" {
		st.mu.Lock()
		items = append([]todoItem{}, st.todos...)
		st.mu.Unlock()
	} else {
		data := []byte(a.Todos)
		var asString string
		if json.Unmarshal(data, &asString) == nil {
			data = []byte(asString)
		}
		var list []rawTodo
		if err := json.Unmarshal(data, &list); err != nil {
			return errorResult("todos must be a list of {id, content, status} items"), nil
		}
		items = st.writeTodos(list, a.Merge)
	}
	if items == nil {
		items = []todoItem{}
	}
	sum := map[string]int{"total": len(items), "pending": 0, "in_progress": 0, "completed": 0, "cancelled": 0}
	for _, it := range items {
		sum[it.Status]++
	}
	return jsonResult(map[string]any{"todos": items, "summary": sum})
}
