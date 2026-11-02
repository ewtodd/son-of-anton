package tools_test

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

func TestClarifyAsksTheUser(t *testing.T) {
	f := setup(t, nil)
	var asked tools.Question
	f.env.Ask = func(_ context.Context, q tools.Question) ([]string, error) {
		asked = q
		return []string{"Postgres"}, nil
	}
	r := f.call(t, "clarify", map[string]any{"question": "Which database?", "choices": []string{"Postgres", "SQLite", "MySQL", "Mongo", "Redis"}})
	if asked.Text != "Which database?" || len(asked.Choices) != 4 || asked.MultiSelect {
		t.Fatalf("asked %+v; at most 4 choices are offered", asked)
	}
	if str(r, "question") != "Which database?" || str(r, "user_response") != "Postgres" {
		t.Fatalf("clarify = %v", r)
	}
	offered, _ := json.Marshal(r["choices_offered"])
	if !strings.Contains(string(offered), "SQLite") || strings.Contains(string(offered), "Redis") {
		t.Fatalf("choices_offered = %s", offered)
	}
}

func TestClarifyMultiSelectReturnsAList(t *testing.T) {
	f := setup(t, nil)
	f.env.Ask = func(_ context.Context, q tools.Question) ([]string, error) {
		if !q.MultiSelect {
			t.Error("multi_select not passed on")
		}
		return []string{"lint, then test", "deploy"}, nil
	}
	r := f.call(t, "clarify", map[string]any{"question": "Which steps?", "choices": []string{"lint, then test", "deploy"}, "multi_select": true})
	list, _ := r["user_response"].([]any)
	if len(list) != 2 || list[0] != "lint, then test" {
		t.Fatalf("a multi-select answer is a list, commas intact: %v", r["user_response"])
	}
}

func TestClarifyWithNobodyToAskAnswersAtOnce(t *testing.T) {
	f := setup(t, nil) // Ask is nil
	r := f.call(t, "clarify", map[string]any{"question": "Proceed?", "choices": []string{"yes", "no"}, "multi_select": true})
	resp, _ := r["user_response"].(string)
	if isErr(r) || resp == "" || !strings.Contains(strings.ToLower(resp), "no one") {
		t.Fatalf("with nobody to ask, clarify answers at once with a plain instruction: %v", r)
	}
	if r := f.call(t, "clarify", map[string]any{"question": ""}); !isErr(r) {
		t.Fatalf("an empty question: %v", r)
	}
}
