// Package title names sessions: an instant title derived from the opening
// message, then one from the title model. A port of ../agent/title_generator.py.
package title

import (
	"context"
	"encoding/json"
	"errors"
	"regexp"
	"strings"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

const (
	maxDerived    = 48   // MAX_DERIVED_TITLE_CHARS
	maxInput      = 1000 // the opening message sent to the title model
	maxTitleWords = 12
)

// systemPrompt is _TITLE_PROMPT_TEMPLATE (TG:62-81), verbatim.
const systemPrompt = "You name chat sessions. Given the user's opening message, write a title " +
	"that lets them find this conversation again in a list.\n\n" +
	"Rules:\n" +
	"- 3 to 7 words, sentence case (capitalize only the first word and proper nouns).\n" +
	"- Name what the user wants DONE, not that they asked a question.\n" +
	"- Keep technical terms, filenames, numbers, and error codes exact.\n" +
	"- Drop filler words: the, this, my, a, an.\n" +
	"- No trailing punctuation, no quotes, no tool names, no 'Title:' prefix.\n" +
	"- Never answer the message. Name it.\n" +
	"- Always produce something, even for a bare greeting.\n" +
	"- Write the title in the same language as the user's message.\n" +
	"Good: {\"title\": \"Fix login button on mobile\"}\n" +
	"Good: {\"title\": \"Postgres connection pool exhaustion\"}\n" +
	"Good: {\"title\": \"Friendly greeting\"}\n" +
	"Too vague: {\"title\": \"Code changes\"}\n" +
	"Too long: {\"title\": \"Investigate and fix the issue where the login button " +
	"does not respond on mobile devices\"}\n\n" +
	"Reply with JSON only: {\"title\": \"...\"}"

// responseFormat is _TITLE_RESPONSE_FORMAT (TG:87-99).
const responseFormat = `{"type":"json_schema","json_schema":{"name":"session_title","strict":true,"schema":{"type":"object","properties":{"title":{"type":"string"}},"required":["title"],"additionalProperties":false}}}`

// Derive is the instant title: the first non-empty line, cut at 48
// characters on a word boundary with "…". Empty when there is no text.
func Derive(text string) string {
	var line string
	for _, ln := range strings.Split(text, "\n") {
		if f := strings.Fields(ln); len(f) > 0 {
			line = strings.Join(f, " ")
			break
		}
	}
	if r := []rune(line); len(r) > maxDerived {
		cut := string(r[:maxDerived])
		if i := strings.LastIndex(cut, " "); i > maxDerived/2 {
			cut = cut[:i]
		}
		line = strings.TrimRight(cut, " ,.;:—-") + "…"
	}
	return line
}

// Generate asks the title model for a title for the opening message text.
// A reply that is empty, answer-shaped (over 12 words) or degenerate is an
// error.
func Generate(ctx context.Context, c *provider.Client, model, text string) (string, error) {
	if r := []rune(text); len(r) > maxInput {
		text = string(r[:maxInput])
	}
	if strings.TrimSpace(text) == "" {
		return "", errors.New("title: no text to name")
	}
	temp := 0.3
	resp, err := c.Complete(ctx, provider.Request{
		Model: model,
		Messages: []provider.Message{
			{Role: "system", Content: systemPrompt},
			{Role: "user", Content: text},
		},
		MaxTokens:      64,
		Temperature:    &temp,
		ResponseFormat: json.RawMessage(responseFormat),
	}, nil)
	if err != nil {
		return "", err
	}
	t := clean(extract(resp.Message.Content))
	switch {
	case t == "":
		return "", errors.New("title: the model returned no title")
	case len(strings.Fields(t)) > maxTitleWords:
		return "", errors.New("title: the model answered instead of naming")
	case degenerate(t):
		return "", errors.New("title: degenerate title")
	}
	return t, nil
}

var (
	fencedRE = regexp.MustCompile("(?s)^```(?:json)?\\s*(.*?)\\s*```$")
	titleRE  = regexp.MustCompile(`"title"\s*:\s*"((?:[^"\\]|\\.)*)"`)
	openRE   = regexp.MustCompile(`"title"\s*:\s*"?((?:[^"\\{}]|\\.)*)`)
	thinkRE  = regexp.MustCompile(`(?is)<think>.*?</think>|<thinking>.*?</thinking>|<reasoning>.*?</reasoning>`)
)

// extract pulls the title out of a reply (TG:230-280).
func extract(content string) string {
	raw := strings.TrimSpace(content)
	if raw == "" {
		return ""
	}
	if m := fencedRE.FindStringSubmatch(raw); m != nil {
		raw = strings.TrimSpace(m[1])
	}
	var obj struct {
		Title *string `json:"title"`
	}
	if json.Unmarshal([]byte(raw), &obj) == nil && obj.Title != nil {
		return strings.TrimSpace(*obj.Title)
	}
	for _, re := range []*regexp.Regexp{titleRE, openRE} {
		if m := re.FindStringSubmatch(raw); m != nil {
			v := strings.TrimSpace(m[1])
			if v == "" {
				continue
			}
			var s string
			if json.Unmarshal([]byte(`"`+v+`"`), &s) == nil {
				return strings.TrimSpace(s)
			}
			return v
		}
	}
	raw = strings.TrimSpace(thinkRE.ReplaceAllString(raw, ""))
	for _, ln := range strings.Split(raw, "\n") {
		if ln = strings.TrimSpace(ln); ln != "" {
			raw = ln
			break
		}
	}
	if len(raw) >= 6 && strings.EqualFold(raw[:6], "title:") {
		raw = strings.TrimSpace(raw[6:])
	}
	return strings.TrimSpace(strings.Trim(raw, `"'`))
}

// clean normalizes a title (TG:283-295).
func clean(s string) string {
	t := strings.Join(strings.Fields(s), " ")
	t = strings.TrimSpace(strings.Trim(t, `"'`))
	if len(t) >= 6 && strings.EqualFold(t[:6], "title:") {
		t = strings.TrimSpace(t[6:])
	}
	t = strings.TrimRight(t, ".!,;:")
	if r := []rune(t); len(r) > 80 {
		t = strings.TrimRight(string(r[:77]), " ") + "..."
	}
	return t
}

// degenerate is true for brace leaks and runs of 4 or more identical words
// (TG:298-318).
func degenerate(t string) bool {
	if strings.ContainsAny(t, "{}") {
		return true
	}
	words := strings.Fields(strings.ToLower(t))
	run := 1
	for i := 1; i < len(words); i++ {
		if words[i] == words[i-1] {
			run++
			if run >= 4 {
				return true
			}
		} else {
			run = 1
		}
	}
	return false
}
