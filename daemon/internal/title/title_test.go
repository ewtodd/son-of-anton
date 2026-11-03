package title_test

// Session titles (phase 3a), ported from ../agent/title_generator.py: an
// instant title derived from the first message, then one from the title
// model, cleaned up and rejected when it reads like an answer.

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/title"
)

var ctx = context.Background()

func TestDeriveTakesTheFirstLineCutAtAWord(t *testing.T) {
	if got := title.Derive("fix the login button on mobile"); got != "fix the login button on mobile" {
		t.Fatalf("short = %q", got)
	}
	if got := title.Derive("  \n\nfirst real line\nsecond"); got != "first real line" {
		t.Fatalf("first non-empty line = %q", got)
	}
	long := "please refactor the configuration loader so that every section validates its keys"
	got := title.Derive(long)
	if !strings.HasSuffix(got, "…") || utf8.RuneCountInString(got) > 49 {
		t.Fatalf("long = %q; at most 48 characters plus …", got)
	}
	if !strings.HasPrefix(long, strings.TrimSuffix(got, "…")+" ") {
		t.Fatalf("long = %q; cut at a word boundary", got)
	}
	if title.Derive(" \n ") != "" {
		t.Fatal("nothing to derive from")
	}
}

func TestGenerateAsksTheTitleModel(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: `{"title": "Fix login button on mobile"}`}, fakellm.Turn{Content: `{"title":"Long input"}`})
	c := provider.New(fake.URL, "k")
	got, err := title.Generate(ctx, c, "llama/little-titles", "the login button doesn't work on my phone, can you fix it")
	if err != nil || got != "Fix login button on mobile" {
		t.Fatalf("Generate = %q, %v", got, err)
	}
	req := fake.Request(0)
	rf, _ := req["response_format"].(map[string]any)
	if req["model"] != "llama/little-titles" || req["max_tokens"] != 64.0 || req["temperature"] != 0.3 || rf["type"] != "json_schema" {
		t.Fatalf("title request = %v", req)
	}
	msgs := fake.Messages(0)
	if len(msgs) != 2 || !strings.Contains(string(msgs[0]), "first message of a conversation") || !strings.Contains(string(msgs[0]), "Output the JSON object and nothing else") {
		t.Fatalf("messages = %s; the system prompt is the title prompt", msgs)
	}
	if !strings.Contains(string(msgs[1]), "login button") {
		t.Fatalf("the user message is the opening message: %s", msgs[1])
	}
	if _, err := title.Generate(ctx, c, "m", strings.Repeat("word ", 1000)); err != nil {
		t.Fatal(err)
	}
	var second struct {
		Messages []struct{ Content string } `json:"messages"`
	}
	b := fake.Bodies()[1]
	if err := json.Unmarshal(b, &second); err != nil || utf8.RuneCountInString(second.Messages[1].Content) > 1000 {
		t.Fatalf("the opening message is capped at 1000 characters (got %d)", utf8.RuneCountInString(second.Messages[1].Content))
	}
}

func TestTitlesAreCleanedOrRejected(t *testing.T) {
	cases := []struct {
		out  string
		want string // "" means Generate must fail
	}{
		{"```json\n{\"title\": \"Postgres pool exhaustion\"}\n```", "Postgres pool exhaustion"},
		{`"Friendly greeting."`, "Friendly greeting"},
		{`Title: Session naming`, "Session naming"},
		{`{"title": "Response Response Response Response"}`, ""},
		{"<think>the user wants a title</think>\nRefactor the parser", "Refactor the parser"},
		{`{"title": "This is clearly an answer and not a title because it keeps on going past twelve words"}`, ""},
		{`{"title": ""}`, ""},
		{`{"title": "Supercalifragilisticexpialidocious antidisestablishmentarianism floccinaucinihilipilification pneumonoultramicroscopic"}`, "Supercalifragilisticexpialidocious antidisestablishmentarianism floccinaucini..."},
	}
	turns := make([]fakellm.Turn, len(cases))
	for i, c := range cases {
		turns[i] = fakellm.Turn{Content: c.out}
	}
	fake := fakellm.New(t, turns...)
	cl := provider.New(fake.URL, "k")
	cl.Retry = provider.RetryPolicy{MaxAttempts: 1}
	for _, c := range cases {
		got, err := title.Generate(ctx, cl, "m", "hello")
		if c.want == "" {
			if err == nil {
				t.Errorf("model said %q: got title %q, want it rejected", c.out, got)
			}
			continue
		}
		if err != nil || got != c.want {
			t.Errorf("model said %q: got %q, %v; want %q", c.out, got, err, c.want)
		}
	}
}
