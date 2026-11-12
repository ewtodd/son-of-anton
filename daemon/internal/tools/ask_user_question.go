package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

const (
	askMaxQuestions = 4
	askMaxOptions   = 4
)

// Question is one question AskUserQuestion puts to the person driving the
// session. Choices are its options' labels and Descriptions their
// explanations, index for index ("" where an option has none). Number and Of
// place it among the questions of one call.
type Question struct {
	Text         string
	Header       string
	Choices      []string
	Descriptions []string
	MultiSelect  bool
	Number, Of   int
}

// Questioner asks a question and returns the answer(s). A nil Questioner
// means nobody can answer.
type Questioner func(ctx context.Context, q Question) ([]string, error)

const askNobody = "No one is available to answer here. Decide by yourself on the most sensible option, " +
	"say in your final reply which option you chose and why, and keep going."

type askUserQuestionTool struct{}

func (askUserQuestionTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "AskUserQuestion",
		Description: "Ask the user one to four questions and wait for the answers. Keep it for decisions that change the outcome " +
			"and that the conversation does not settle, not for routine go-aheads. " +
			"Each question offers two to four options, and the user can always answer in their own words instead. " +
			"Set multiSelect when more than one option may apply. The questions are asked one after another, " +
			"and the reply gives each question's answer: a list of the picks for a multiSelect question, else one string.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "questions": {"type": "array", "minItems": 1, "maxItems": 4, "description": "The questions, in the order to ask them.", "items": {
      "type": "object",
      "properties": {
        "question": {"type": "string", "description": "The full question, clear without the rest of the conversation."},
        "header": {"type": "string", "description": "A label of a word or two for the question, such as \"Database\" (12 characters at most)."},
        "options": {"type": "array", "minItems": 2, "maxItems": 4, "description": "The answers to offer.", "items": {
          "type": "object",
          "properties": {
            "label": {"type": "string", "description": "The option as the user picks it: a few words."},
            "description": {"type": "string", "description": "What choosing it means."}
          },
          "required": ["label", "description"]
        }},
        "multiSelect": {"type": "boolean", "description": "Let the user pick more than one option."}
      },
      "required": ["question", "header", "options", "multiSelect"]
    }}
  },
  "required": ["questions"]
}`),
	}}
}

// parseQuestions reads the questions leniently: an option may be given as
// its label alone, and a question with no options is an open one.
func parseQuestions(raw json.RawMessage) ([]Question, string) {
	var a struct {
		Questions []struct {
			Question    string `json:"question"`
			Header      string `json:"header"`
			Options     []any  `json:"options"`
			MultiSelect bool   `json:"multiSelect"`
		} `json:"questions"`
	}
	if err := json.Unmarshal(raw, &a); err != nil {
		return nil, "invalid arguments: " + err.Error()
	}
	if len(a.Questions) == 0 || len(a.Questions) > askMaxQuestions {
		return nil, fmt.Sprintf("questions must hold 1 to %d questions; it holds %d", askMaxQuestions, len(a.Questions))
	}
	var qs []Question
	for i, in := range a.Questions {
		q := Question{Text: strings.TrimSpace(in.Question), Header: strings.TrimSpace(in.Header), MultiSelect: in.MultiSelect,
			Number: i + 1, Of: len(a.Questions)}
		if q.Text == "" {
			return nil, fmt.Sprintf("question %d has no question text", i+1)
		}
		if len(in.Options) > askMaxOptions {
			return nil, fmt.Sprintf("question %d offers %d options; offer at most %d (the user can always answer in their own words)", i+1, len(in.Options), askMaxOptions)
		}
		for _, o := range in.Options {
			label, desc := "", ""
			switch o := o.(type) {
			case string:
				label = o
			case map[string]any:
				label, desc = argStr(o, "label"), argStr(o, "description")
			}
			if label = strings.TrimSpace(label); label == "" {
				return nil, fmt.Sprintf("question %d has an option with no label", i+1)
			}
			q.Choices = append(q.Choices, label)
			q.Descriptions = append(q.Descriptions, desc)
		}
		qs = append(qs, q)
	}
	return qs, ""
}

func (askUserQuestionTool) Run(ctx context.Context, env *Env, raw json.RawMessage) (string, error) {
	qs, msg := parseQuestions(raw)
	if msg != "" {
		return errorResult(msg), nil
	}
	if env.Ask == nil {
		return jsonResult(map[string]any{"answers": []any{}, "note": askNobody})
	}
	answers := []map[string]any{}
	for _, q := range qs {
		got, err := env.Ask(ctx, q)
		if err != nil {
			return jsonResult(map[string]any{"error": "no answer came back: " + err.Error(), "answers": answers})
		}
		var answer any = strings.Join(got, ", ")
		if q.MultiSelect {
			answer = append([]string{}, got...)
		}
		answers = append(answers, map[string]any{"question": q.Text, "answer": answer})
	}
	return jsonResult(map[string]any{"answers": answers})
}
