package tools_test

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

type todoItem struct {
	Content, Status, ActiveForm string
}

func todos(t *testing.T, r map[string]any) []todoItem {
	t.Helper()
	list, _ := r["todos"].([]any)
	var out []todoItem
	for _, e := range list {
		m := e.(map[string]any)
		out = append(out, todoItem{str(m, "content"), str(m, "status"), str(m, "activeForm")})
	}
	return out
}

func TestTodoWriteReplacesTheWholeListEachCall(t *testing.T) {
	f := setup(t, nil)
	r := f.call(t, "TodoWrite", map[string]any{"todos": []map[string]any{
		{"content": "Write tests", "status": "completed", "activeForm": "Writing tests"},
		{"content": "Implement", "status": "in_progress", "activeForm": "Implementing"},
		{"content": "Review", "status": "pending", "activeForm": "Reviewing"},
	}})
	if got := todos(t, r); len(got) != 3 || got[1] != (todoItem{"Implement", "in_progress", "Implementing"}) {
		t.Fatalf("todos = %+v", got)
	}
	s, _ := r["summary"].(map[string]any)
	if num(s, "total") != 3 || num(s, "completed") != 1 || num(s, "in_progress") != 1 || num(s, "pending") != 1 {
		t.Fatalf("summary = %v", s)
	}
	r = f.call(t, "TodoWrite", map[string]any{"todos": []map[string]any{{"content": "Only", "status": "pending", "activeForm": "Doing only"}}})
	if got := todos(t, r); len(got) != 1 || got[0].Content != "Only" {
		t.Fatalf("each call replaces the list: %+v", got)
	}
	if got := todos(t, f.call(t, "TodoWrite", map[string]any{"todos": []any{}})); len(got) != 0 {
		t.Fatalf("an empty list clears it: %+v", got)
	}
}

func TestTodoWriteValidation(t *testing.T) {
	f := setup(t, nil)
	if r := f.call(t, "TodoWrite", map[string]any{"todos": []map[string]any{{"content": "a", "status": "cancelled", "activeForm": "A"}}}); !strings.Contains(str(r, "error"), "pending, in_progress or completed") {
		t.Fatalf("a status that is not one: %v", r)
	}
	if r := f.call(t, "TodoWrite", map[string]any{"todos": []map[string]any{{"status": "pending", "activeForm": "A"}}}); !strings.Contains(str(r, "error"), "no content") {
		t.Fatalf("an entry with no content: %v", r)
	}
	if r := f.call(t, "TodoWrite", map[string]any{}); !isErr(r) {
		t.Fatalf("no todos: %v", r)
	}
	r := f.call(t, "TodoWrite", map[string]any{"todos": []map[string]any{{"content": "No form given", "status": "IN_PROGRESS"}}})
	if got := todos(t, r); len(got) != 1 || got[0].ActiveForm != "No form given" || got[0].Status != "in_progress" {
		t.Fatalf("a missing activeForm falls back to the content, and status is read in any case: %+v", got)
	}
	many := make([]map[string]any, 300)
	for i := range many {
		many[i] = map[string]any{"content": fmt.Sprintf("%d", i) + strings.Repeat("c", 5000), "status": "pending", "activeForm": "x"}
	}
	got := todos(t, f.call(t, "TodoWrite", map[string]any{"todos": many}))
	if len(got) > 256 || len(got[0].Content) > 4100 || !strings.Contains(got[0].Content, "[truncated]") {
		t.Fatalf("caps: %d items, first content %d chars", len(got), len(got[0].Content))
	}
	asString, _ := json.Marshal([]map[string]any{{"content": "from a string", "status": "pending", "activeForm": "x"}})
	if got := todos(t, f.call(t, "TodoWrite", map[string]any{"todos": string(asString)})); len(got) != 1 || got[0].Content != "from a string" {
		t.Fatalf("todos passed as a JSON string: %+v", got)
	}
	if r := f.call(t, "TodoWrite", map[string]any{"todos": "not json"}); !isErr(r) {
		t.Fatalf("unparseable todos: %v", r)
	}
}
