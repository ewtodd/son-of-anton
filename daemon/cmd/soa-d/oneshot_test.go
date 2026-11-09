package main

// soa-d oneshot (the -z replacement) and prompt composition on the command
// line: the lean prompt by default, --memory, --system, and query's full
// prompt.

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

const testRules = `
[[rule]]
id = "plain-prose"
text = "Write in plain English."
scope = "common"
source = "user"

[[rule]]
id = "never-delete"
text = "Never delete user files."
scope = "common"
source = "user"
delegated = true
`

func withRules(t *testing.T, home string) {
	t.Helper()
	p := filepath.Join(home, "memories", "rules.toml")
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(testRules), 0o600); err != nil {
		t.Fatal(err)
	}
}

func systemOf(t *testing.T, fake *fakellm.Server) string {
	t.Helper()
	var m struct {
		Role, Content string
	}
	if err := json.Unmarshal(fake.Messages(0)[0], &m); err != nil || m.Role != "system" {
		t.Fatalf("first message = %s", fake.Messages(0)[0])
	}
	return m.Content
}

func runIn(t *testing.T, args ...string) (int, string, string) {
	t.Helper()
	t.Chdir(t.TempDir())
	var out, errOut bytes.Buffer
	code := run(args, &out, &errOut)
	return code, out.String(), errOut.String()
}

func TestOneshotPrintsOnlyTheFinalAnswer(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "thinking out loud", ToolCalls: []fakellm.ToolCall{{ID: "c1", Name: "terminal", Arguments: `{"command":"echo hi"}`}}},
		fakellm.Turn{Content: "the answer"},
	)
	home := testHome(t, fake)
	code, out, errOut := runIn(t, "oneshot", "--home", home, "do it")
	if code != 0 || out != "the answer\n" {
		t.Fatalf("exit %d, stdout %q, stderr %q: stdout carries only the final answer", code, out, errOut)
	}
}

func TestOneshotUsageFile(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{{ID: "c1", Name: "terminal", Arguments: `{"command":"true"}`}}, PromptTokens: 100, CompletionTokens: 10, CachedTokens: 40},
		fakellm.Turn{Content: "done", PromptTokens: 120, CompletionTokens: 5, ReasoningTokens: 2},
	)
	home := testHome(t, fake)
	usage := filepath.Join(t.TempDir(), "usage.json")
	if code, _, errOut := runIn(t, "oneshot", "--home", home, "--usage-file", usage, "go"); code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	var u map[string]any
	b, err := os.ReadFile(usage)
	if err != nil || json.Unmarshal(b, &u) != nil {
		t.Fatalf("usage file: %s, %v", b, err)
	}
	if u["input_tokens"] != 220.0 || u["output_tokens"] != 15.0 || u["cache_read_tokens"] != 40.0 || u["reasoning_tokens"] != 2.0 ||
		u["api_calls"] != 2.0 || u["model"] != "test-model" || u["completed"] != true || u["failed"] != false || u["session_id"] == "" {
		t.Fatalf("usage = %v", u)
	}
}

func TestOneshotFailureStillWritesUsage(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Status: 400, Body: `{"error":"no such model"}`})
	home := testHome(t, fake)
	usage := filepath.Join(t.TempDir(), "usage.json")
	code, out, errOut := runIn(t, "oneshot", "--home", home, "--usage-file", usage, "go")
	if code != 1 || out != "" || !strings.Contains(errOut, "400") {
		t.Fatalf("exit %d, stdout %q, stderr %q", code, out, errOut)
	}
	var u map[string]any
	b, _ := os.ReadFile(usage)
	if json.Unmarshal(b, &u) != nil || u["failed"] != true || u["failure"] == "" {
		t.Fatalf("usage on failure = %s", b)
	}
	if code, _, _ := runIn(t, "oneshot", "--home", home); code != 2 {
		t.Fatalf("no prompt: exit %d, want 2", code)
	}
}

func TestOneshotIsLeanByDefault(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "ok"}, fakellm.Turn{Content: "ok"})
	home := testHome(t, fake)
	withRules(t, home)
	runIn(t, "oneshot", "--home", home, "task")
	lean := systemOf(t, fake)
	if strings.Contains(lean, prompt.DefaultIdentity) || strings.Contains(lean, "plain-prose") {
		t.Fatalf("a oneshot carries no identity and no ordinary rules:\n%s", lean)
	}
	if !strings.Contains(lean, "[never-delete]") || !strings.Contains(lean, prompt.Guidance) {
		t.Fatalf("a oneshot keeps delegated rules and the guidance:\n%s", lean)
	}
	fake2 := fakellm.New(t, fakellm.Turn{Content: "ok"})
	home2 := testHome(t, fake2)
	withRules(t, home2)
	runIn(t, "oneshot", "--home", home2, "--memory", "task")
	if full := systemOf(t, fake2); !strings.Contains(full, prompt.DefaultIdentity) || !strings.Contains(full, "[plain-prose]") {
		t.Fatalf("--memory gives a oneshot the full prompt:\n%s", full)
	}
}

func TestQueryUsesTheFullPromptUnlessNoMemory(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "ok"})
	home := testHome(t, fake)
	withRules(t, home)
	runIn(t, "query", "--home", home, "q")
	if full := systemOf(t, fake); !strings.Contains(full, prompt.DefaultIdentity) || !strings.Contains(full, "[plain-prose]") || !strings.Contains(full, prompt.Guidance) {
		t.Fatalf("query's default prompt is the full one:\n%s", full)
	}
	fake2 := fakellm.New(t, fakellm.Turn{Content: "ok"})
	home2 := testHome(t, fake2)
	withRules(t, home2)
	runIn(t, "query", "--home", home2, "--no-memory", "q")
	if lean := systemOf(t, fake2); strings.Contains(lean, prompt.DefaultIdentity) || strings.Contains(lean, "plain-prose") {
		t.Fatalf("query --no-memory is lean:\n%s", lean)
	}
}

func TestSystemFileIsTheWholePrompt(t *testing.T) {
	for _, cmd := range []string{"oneshot", "query"} {
		fake := fakellm.New(t, fakellm.Turn{Content: "ok"})
		home := testHome(t, fake)
		withRules(t, home)
		sys := filepath.Join(t.TempDir(), "sys.md")
		if err := os.WriteFile(sys, []byte("Only this.\nNothing else.\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		if code, _, errOut := runIn(t, cmd, "--home", home, "--system", sys, "go"); code != 0 {
			t.Fatalf("%s: exit %d: %s", cmd, code, errOut)
		}
		if got := systemOf(t, fake); got != "Only this.\nNothing else.\n" {
			t.Fatalf("%s --system: system prompt = %q, want the file verbatim", cmd, got)
		}
	}
}

// A --system prompt is the whole prompt: without the send-time rule, the
// switch is on but no prefix goes out, so scripted input stays plain.
func TestSystemPromptWithoutTheRuleGetsNoSendTimePrefix(t *testing.T) {
	for _, cmd := range []string{"oneshot", "query"} {
		fake := fakellm.New(t, fakellm.Turn{Content: "ok"})
		home := testHome(t, fake)
		withRules(t, home)
		sys := filepath.Join(t.TempDir(), "sys.md")
		if err := os.WriteFile(sys, []byte("Only this.\nNothing else.\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		if code, _, errOut := runIn(t, cmd, "--home", home, "--system", sys, "go"); code != 0 {
			t.Fatalf("%s: exit %d: %s", cmd, code, errOut)
		}
		var m struct {
			Role, Content string
		}
		if err := json.Unmarshal(fake.Messages(0)[1], &m); err != nil || m.Role != "user" || m.Content != "go" {
			t.Fatalf("%s --system: user message = %s, want the plain text (the prompt lacks the rule)", cmd, fake.Messages(0)[1])
		}
	}
}

func TestOneshotSessionAndClarify(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{{ID: "c1", Name: "clarify", Arguments: `{"question":"Which one?","choices":["a","b"]}`}}},
		fakellm.Turn{Content: "picked a"},
	)
	home := testHome(t, fake)
	if code, _, errOut := runIn(t, "oneshot", "--home", home, "choose"); code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	if !bytes.Contains(bytes.ToLower(fake.Messages(1)[3]), []byte("no one")) {
		t.Fatalf("clarify in a oneshot answers at once: %s", fake.Messages(1)[3])
	}
	st, err := store.Open(filepath.Join(home, "soa-d.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	hits, _ := st.Search(context.Background(), "picked", 5)
	if len(hits) != 1 {
		t.Fatalf("hits %+v", hits)
	}
	if sess, _ := st.Session(context.Background(), hits[0].SessionID); sess.Kind != "oneshot" || sess.Source != "oneshot" {
		t.Fatalf("session = %+v", sess)
	}
}
