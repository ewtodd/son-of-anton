package tools_test

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

func answers(r map[string]any) []map[string]any {
	list, _ := r["answers"].([]any)
	var out []map[string]any
	for _, a := range list {
		m, _ := a.(map[string]any)
		out = append(out, m)
	}
	return out
}

func TestAskUserQuestionAsksEachQuestionInTurn(t *testing.T) {
	f := setup(t, nil)
	var asked []tools.Question
	f.env.Ask = func(_ context.Context, q tools.Question) ([]string, error) {
		asked = append(asked, q)
		if q.MultiSelect {
			return []string{"lint, then test", "deploy"}, nil
		}
		return []string{q.Choices[0]}, nil
	}
	r := f.call(t, "AskUserQuestion", map[string]any{"questions": []map[string]any{
		{"question": "Which database?", "header": "Database", "multiSelect": false, "options": []map[string]any{
			{"label": "Postgres", "description": "A server"}, {"label": "SQLite", "description": "A file"},
		}},
		{"question": "Which steps?", "header": "Steps", "multiSelect": true, "options": []map[string]any{
			{"label": "lint, then test", "description": "Checks"}, {"label": "deploy", "description": "Ship it"},
		}},
	}})
	if len(asked) != 2 {
		t.Fatalf("asked %d questions, want 2 in turn", len(asked))
	}
	q := asked[0]
	if q.Text != "Which database?" || q.Header != "Database" || strings.Join(q.Choices, "|") != "Postgres|SQLite" ||
		strings.Join(q.Descriptions, "|") != "A server|A file" || q.Number != 1 || q.Of != 2 || q.MultiSelect {
		t.Fatalf("first question = %+v", q)
	}
	if !asked[1].MultiSelect || asked[1].Number != 2 {
		t.Fatalf("second question = %+v", asked[1])
	}
	got := answers(r)
	if len(got) != 2 || got[0]["question"] != "Which database?" || got[0]["answer"] != "Postgres" {
		t.Fatalf("answers = %v", r)
	}
	if list, _ := got[1]["answer"].([]any); len(list) != 2 || list[0] != "lint, then test" {
		t.Fatalf("a multiSelect answer is a list, commas intact: %v", got[1]["answer"])
	}
}

func TestAskUserQuestionValidation(t *testing.T) {
	f := setup(t, nil)
	f.env.Ask = func(context.Context, tools.Question) ([]string, error) { return []string{"x"}, nil }
	five := make([]map[string]any, 5)
	for i := range five {
		five[i] = map[string]any{"question": "q?", "header": "h", "multiSelect": false, "options": []string{"a", "b"}}
	}
	for name, args := range map[string]map[string]any{
		"no questions":   {"questions": []any{}},
		"five questions": {"questions": five},
		"no text":        {"questions": []map[string]any{{"question": " ", "options": []string{"a", "b"}}}},
		"five options":   {"questions": []map[string]any{{"question": "q?", "options": []string{"a", "b", "c", "d", "e"}}}},
		"a blank label":  {"questions": []map[string]any{{"question": "q?", "options": []map[string]any{{"label": ""}, {"label": "b"}}}}},
	} {
		if r := f.call(t, "AskUserQuestion", args); !isErr(r) {
			t.Errorf("%s: %v", name, r)
		}
	}
	// Options given as bare labels, as the clarify tool took them, still work.
	var got tools.Question
	f.env.Ask = func(_ context.Context, q tools.Question) ([]string, error) { got = q; return []string{"b"}, nil }
	if r := f.call(t, "AskUserQuestion", map[string]any{"questions": []map[string]any{{"question": "q?", "options": []string{"a", "b"}}}}); isErr(r) ||
		strings.Join(got.Choices, ",") != "a,b" {
		t.Fatalf("bare labels: %v %+v", r, got)
	}
}

func TestAskUserQuestionWithNobodyToAskAnswersAtOnce(t *testing.T) {
	f := setup(t, nil) // Ask is nil
	r := f.call(t, "AskUserQuestion", map[string]any{"questions": []map[string]any{{"question": "Proceed?", "header": "Go", "multiSelect": false,
		"options": []map[string]any{{"label": "yes", "description": "go on"}, {"label": "no", "description": "stop"}}}}})
	if isErr(r) || !strings.Contains(strings.ToLower(str(r, "note")), "no one") {
		t.Fatalf("with nobody to ask, the tool answers at once with a plain instruction: %v", r)
	}
}

func TestAskUserQuestionKeepsTheAnswersBeforeAWithdrawal(t *testing.T) {
	f := setup(t, nil)
	n := 0
	f.env.Ask = func(context.Context, tools.Question) ([]string, error) {
		if n++; n == 2 {
			return nil, errors.New("the question was withdrawn")
		}
		return []string{"first"}, nil
	}
	r := f.call(t, "AskUserQuestion", map[string]any{"questions": []map[string]any{
		{"question": "One?", "options": []string{"first", "second"}},
		{"question": "Two?", "options": []string{"first", "second"}},
		{"question": "Three?", "options": []string{"first", "second"}},
	}})
	if !strings.Contains(str(r, "error"), "withdrawn") || len(answers(r)) != 1 || n != 2 {
		t.Fatalf("a withdrawn second question = %v (asked %d)", r, n)
	}
}
