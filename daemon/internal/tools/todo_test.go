package tools_test

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

type todoItem struct {
	ID, Content, Status string
}

func todos(t *testing.T, r map[string]any) []todoItem {
	t.Helper()
	list, _ := r["todos"].([]any)
	var out []todoItem
	for _, e := range list {
		m := e.(map[string]any)
		out = append(out, todoItem{str(m, "id"), str(m, "content"), str(m, "status")})
	}
	return out
}

func TestTodoReplaceReadAndSummary(t *testing.T) {
	f := setup(t, nil)
	r := f.call(t, "todo", map[string]any{"todos": []map[string]any{
		{"id": "1", "content": "write tests", "status": "completed"},
		{"id": "2", "content": "implement", "status": "in_progress"},
		{"id": "3", "content": "review", "status": "pending"},
	}})
	if got := todos(t, r); len(got) != 3 || got[1] != (todoItem{"2", "implement", "in_progress"}) {
		t.Fatalf("todos = %+v", got)
	}
	s, _ := r["summary"].(map[string]any)
	if num(s, "total") != 3 || num(s, "completed") != 1 || num(s, "in_progress") != 1 || num(s, "pending") != 1 || num(s, "cancelled") != 0 {
		t.Fatalf("summary = %v", s)
	}
	if got := todos(t, f.call(t, "todo", map[string]any{})); len(got) != 3 {
		t.Fatalf("calling with no todos reads the list: %+v", got)
	}
	r = f.call(t, "todo", map[string]any{"todos": []map[string]any{{"id": "9", "content": "only", "status": "pending"}}})
	if got := todos(t, r); len(got) != 1 || got[0].ID != "9" {
		t.Fatalf("without merge the list is replaced: %+v", got)
	}
}

func TestTodoMerge(t *testing.T) {
	f := setup(t, nil)
	f.call(t, "todo", map[string]any{"todos": []map[string]any{
		{"id": "a", "content": "first", "status": "pending"},
		{"id": "b", "content": "second", "status": "pending"},
	}})
	r := f.call(t, "todo", map[string]any{"merge": true, "todos": []map[string]any{
		{"id": "a", "status": "completed"},
		{"id": "c", "content": "third", "status": "pending"},
		{"content": "no id, skipped", "status": "pending"},
		{"id": "b", "status": "bogus"},
	}})
	got := todos(t, r)
	want := []todoItem{{"a", "first", "completed"}, {"b", "second", "pending"}, {"c", "third", "pending"}}
	if len(got) != len(want) {
		t.Fatalf("merged = %+v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("merged[%d] = %+v, want %+v", i, got[i], want[i])
		}
	}
}

func TestTodoValidation(t *testing.T) {
	f := setup(t, nil)
	r := f.call(t, "todo", map[string]any{"todos": []map[string]any{
		{"id": "x", "content": "a", "status": "nonsense"},
		{"id": "x", "content": "a again", "status": "pending"},
	}})
	if got := todos(t, r); len(got) != 1 || got[0].Content != "a again" || got[0].Status != "pending" {
		t.Fatalf("duplicate ids keep the last; a bad status becomes pending: %+v", got)
	}
	many := make([]map[string]any, 300)
	for i := range many {
		many[i] = map[string]any{"id": fmt.Sprintf("t%d", i), "content": strings.Repeat("c", 5000), "status": "pending"}
	}
	r = f.call(t, "todo", map[string]any{"todos": many})
	got := todos(t, r)
	if len(got) > 256 || len(got[0].Content) > 4100 || !strings.Contains(got[0].Content, "[truncated]") {
		t.Fatalf("caps: %d items, first content %d chars", len(got), len(got[0].Content))
	}
	asString, _ := json.Marshal([]map[string]any{{"id": "s", "content": "from a string", "status": "pending"}})
	if got := todos(t, f.call(t, "todo", map[string]any{"todos": string(asString)})); len(got) != 1 || got[0].ID != "s" {
		t.Fatalf("todos passed as a JSON string: %+v", got)
	}
	if r := f.call(t, "todo", map[string]any{"todos": "not json"}); !isErr(r) {
		t.Fatalf("unparseable todos: %v", r)
	}
}

func TestTodoListsArePerSessionAndRestorable(t *testing.T) {
	f := setup(t, nil)
	r := f.call(t, "todo", map[string]any{"todos": []map[string]any{{"id": "1", "content": "carry over", "status": "in_progress"}}})
	raw, _ := json.Marshal(r)
	other := setup(t, nil)
	if got := todos(t, other.call(t, "todo", map[string]any{})); len(got) != 0 {
		t.Fatalf("another session starts empty: %+v", got)
	}
	if err := other.env.State.RestoreTodos(string(raw)); err != nil {
		t.Fatal(err)
	}
	if got := todos(t, other.call(t, "todo", map[string]any{})); len(got) != 1 || got[0].Content != "carry over" {
		t.Fatalf("restored from a stored todo result: %+v", got)
	}
}
