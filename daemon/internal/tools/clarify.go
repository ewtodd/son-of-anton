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

const clarifyNobody = "No one is available to answer here. Decide by yourself on the most sensible option, " +
	"say in your final reply which option you chose and why, and keep going."

type clarifyTool struct{}

func (clarifyTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "clarify",
		Description: "Put a question to the user and wait for the answer. Reserve it for a decision that changes the outcome and that you cannot make from the context. " +
			"A short list of likely answers can be attached (at most four); the user may still reply in their own words. Routine go-aheads are not worth a question.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "question": {"type": "string", "description": "The question, worded so it can be answered without seeing the rest of the conversation."},
    "choices": {"type": "array", "items": {"type": "string"}, "description": "Likely answers to offer, four at most. Leave out for an open question."},
    "multi_select": {"type": "boolean", "description": "Let the user pick more than one of the choices."}
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
		return errorResult("no answer came back: " + err.Error()), nil
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
