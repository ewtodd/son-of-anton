package tools

import (
	"context"
	"encoding/json"
	"strings"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

const clarifyMaxChoices = 4

// Question is what clarify asks the person driving the session.
type Question struct {
	Text        string
	Choices     []string
	MultiSelect bool
}

// Questioner asks a question and returns the answer(s). A nil Questioner
// means nobody can answer.
type Questioner func(ctx context.Context, q Question) ([]string, error)

const clarifyNobody = "No one can answer questions in this run. Make the most reasonable choice yourself, " +
	"say which one you made and why in your final answer, and continue."

type clarifyTool struct{}

func (clarifyTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "clarify",
		Description: "Ask the user a question when a choice matters and you cannot settle it yourself. " +
			"Offer up to 4 choices when the answers are a short list; the user can always answer freely. " +
			"Do not use it for confirmations of routine work.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "question": {"type": "string", "description": "The question to ask."},
    "choices": {"type": "array", "items": {"type": "string"}, "description": "Up to 4 suggested answers."},
    "multi_select": {"type": "boolean", "description": "Allow choosing several of the choices."}
  },
  "required": ["question"]
}`),
	}}
}

func (clarifyTool) Run(ctx context.Context, env *Env, raw json.RawMessage) (string, error) {
	var a struct {
		Question    string   `json:"question"`
		Choices     []string `json:"choices"`
		MultiSelect bool     `json:"multi_select"`
	}
	if err := json.Unmarshal(raw, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	q := strings.TrimSpace(a.Question)
	if q == "" {
		return errorResult("question is required"), nil
	}
	var choices []string
	for _, c := range a.Choices {
		if c = strings.TrimSpace(c); c != "" && len(choices) < clarifyMaxChoices {
			choices = append(choices, c)
		}
	}
	out := map[string]any{"question": q, "choices_offered": choices}
	if env.Ask == nil {
		out["user_response"] = clarifyNobody
		return jsonResult(out)
	}
	answers, err := env.Ask(ctx, Question{Text: q, Choices: choices, MultiSelect: a.MultiSelect})
	if err != nil {
		return errorResult("the question was not answered: " + err.Error()), nil
	}
	if a.MultiSelect {
		if answers == nil {
			answers = []string{}
		}
		out["user_response"] = answers
	} else if len(answers) > 0 {
		out["user_response"] = answers[0]
	} else {
		out["user_response"] = ""
	}
	return jsonResult(out)
}
