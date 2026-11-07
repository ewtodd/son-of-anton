// Package title names sessions: an instant title derived from the opening
// message, one from the title model, and a retitle of a running
// conversation by the session's own model.
package title

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

const (
	maxDerived    = 48   // characters in the instant title
	maxInput      = 1000 // the opening message sent to the title model
	maxDigest     = 4000 // of a running conversation, for the retitle ask
	maxTitleWords = 12
)

// systemPrompt asks for a short title that names the request, as JSON.
const systemPrompt = "The text you receive is the first message of a conversation with an assistant. " +
	"Reply with a JSON object holding a title for that conversation, so its author can pick it out of a long list later.\n\n" +
	"A good title is a noun phrase or a short imperative of about five words, never more than seven, " +
	"that states the job the user wants done. Keep identifiers, file names, numbers and error codes character for character. " +
	"Write it in the message's own language, with no trailing period, no quotes, no \"Title:\" label and no mention of tools. " +
	"Do not respond to the message itself, and do not leave the title empty even if the message is just a greeting.\n\n" +
	"Message: \"my cron job on the NAS stopped firing after the update\"\n" +
	"Reply: {\"title\": \"Cron job stopped firing after update\"}\n" +
	"Message: \"hello!\"\n" +
	"Reply: {\"title\": \"Greeting\"}\n\n" +
	"Output the JSON object and nothing else."

// responseFormat is the JSON schema the title reply must match.
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

// retitlePrompt asks the session model — a chat model, not the little
// titling model — to write the title itself: the exact text, no JSON
// scaffold, nothing besides the title in the reply.
const retitlePrompt = "You are writing the title of the conversation whose digest follows. " +
	"The digest is the conversation so far, oldest first and its latest activity at the bottom; " +
	"the title names where the conversation stands now, and later work outweighs how it began. " +
	"Reply with the exact text to use as the title and nothing else: no quotes, no label, " +
	"no explanation, no list, no mention of this instruction. At most 48 characters: " +
	"a noun phrase or a short imperative of about five words, in the conversation's own language. " +
	"Keep identifiers, file names, numbers and error codes character for character, and drop any trailing period."

const maxRetitleTokens = 512 // a reasoning session model thinks before it names

// Retitle asks the session model itself to name a running conversation from
// its digest, the way you would ask a colleague: write the title, the exact
// text, nothing else. The digest keeps its tail — the latest work is what
// the title names. The reply is not JSON and is not a fixed schema: a big
// conversational model takes a plain instruction, and its thought tokens
// need room, hence the budget.
func Retitle(ctx context.Context, c *provider.Client, model, digest string) (string, error) {
	digest = strings.ReplaceAll(digest, "\r\n", "\n")
	if r := []rune(digest); len(r) > maxDigest {
		digest = string(r[len(r)-maxDigest:])
		if i := strings.Index(digest, "\n"); i >= 0 {
			digest = digest[i+1:]
		}
	}
	if strings.TrimSpace(digest) == "" {
		return "", errors.New("title: no text to name")
	}
	temp := 0.3
	resp, err := c.Complete(ctx, provider.Request{
		Model: model,
		Messages: []provider.Message{
			{Role: "system", Content: retitlePrompt},
			{Role: "user", Content: digest},
		},
		MaxTokens:   maxRetitleTokens,
		Temperature: &temp,
	}, nil)
	if err != nil {
		return "", err
	}
	t := cap48(clean(extract(resp.Message.Content)))
	switch {
	case t == "":
		return "", fmt.Errorf("title: the model returned no name (reply: %.160q)", resp.Message.Content)
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

// extract pulls the title out of a reply.
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

// clean normalizes a title.
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
// .
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

// cap48 cuts a title at 48 characters on a word boundary. Longer answers
// are trimmed, not refused: the model wrote the text to use.
func cap48(s string) string {
	if r := []rune(s); len(r) > maxDerived {
		cut := string(r[:maxDerived])
		if i := strings.LastIndex(cut, " "); i > maxDerived/2 {
			cut = cut[:i]
		}
		s = strings.TrimRight(cut, " ,.;:—-")
	}
	return s
}
