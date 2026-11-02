package provider_test

import (
	"bytes"
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

func client(fake *fakellm.Server) *provider.Client {
	c := provider.New(fake.URL, "test-key")
	c.Retry = provider.RetryPolicy{MaxAttempts: 4, BaseDelay: time.Millisecond, MaxDelay: 5 * time.Millisecond}
	return c
}

func basicRequest() provider.Request {
	return provider.Request{
		Model: "m",
		Messages: []provider.Message{
			{Role: "system", Content: "be brief"},
			{Role: "user", Content: "hi"},
		},
	}
}

func TestStreamAssemblesContentReasoningAndUsage(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{
		Content: "hello world", Reasoning: "thinking", ChunkRunes: 3,
		PromptTokens: 10, CompletionTokens: 5, ReasoningTokens: 2, CachedTokens: 7,
	})
	var content, reasoning strings.Builder
	contentDeltas := 0
	resp, err := client(fake).Complete(context.Background(), basicRequest(), func(d provider.Delta) {
		if d.Content != "" {
			contentDeltas++
		}
		content.WriteString(d.Content)
		reasoning.WriteString(d.Reasoning)
	})
	if err != nil {
		t.Fatal(err)
	}
	if resp.Message.Role != "assistant" || resp.Message.Content != "hello world" || resp.Reasoning != "thinking" {
		t.Fatalf("response = %+v", resp)
	}
	if content.String() != "hello world" || reasoning.String() != "thinking" || contentDeltas < 2 {
		t.Fatalf("deltas: content %q (%d deltas), reasoning %q", content.String(), contentDeltas, reasoning.String())
	}
	if resp.FinishReason != "stop" {
		t.Fatalf("FinishReason = %q", resp.FinishReason)
	}
	want := provider.Usage{PromptTokens: 10, CompletionTokens: 5, ReasoningTokens: 2, CachedTokens: 7}
	if resp.Usage != want {
		t.Fatalf("Usage = %+v, want %+v", resp.Usage, want)
	}
}

func TestRequestShape(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "a"}, fakellm.Turn{Content: "b"})
	c := client(fake)
	req := basicRequest()
	req.ReasoningEffort = "high"
	if _, err := c.Complete(context.Background(), req, nil); err != nil {
		t.Fatal(err)
	}
	req.ReasoningEffort = ""
	if _, err := c.Complete(context.Background(), req, nil); err != nil {
		t.Fatal(err)
	}

	first := fake.Request(0)
	if first["model"] != "m" || first["stream"] != true || first["reasoning_effort"] != "high" {
		t.Fatalf("request = %v", first)
	}
	opts, _ := first["stream_options"].(map[string]any)
	if opts["include_usage"] != true {
		t.Fatalf("stream_options = %v, want include_usage", first["stream_options"])
	}
	if _, present := fake.Request(1)["reasoning_effort"]; present {
		t.Fatal("an empty reasoning effort must be omitted from the request")
	}
	h := fake.Headers()[0]
	if h.Get("Authorization") != "Bearer test-key" || !strings.HasPrefix(h.Get("Content-Type"), "application/json") {
		t.Fatalf("headers = %v", h)
	}
	msgs := fake.Messages(0)
	if len(msgs) != 2 || !bytes.Contains(msgs[1], []byte(`"hi"`)) {
		t.Fatalf("messages = %s", msgs)
	}
}

func TestToolCallDeltasAssemble(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{ToolCalls: []fakellm.ToolCall{
		{ID: "call_1", Name: "terminal", Arguments: `{"command":"ls -la"}`},
		{ID: "call_2", Name: "read_file", Arguments: `{"path":"/etc/hosts"}`},
	}})
	resp, err := client(fake).Complete(context.Background(), basicRequest(), nil)
	if err != nil {
		t.Fatal(err)
	}
	calls := resp.Message.ToolCalls
	if len(calls) != 2 || resp.FinishReason != "tool_calls" {
		t.Fatalf("tool calls = %+v, finish %q", calls, resp.FinishReason)
	}
	if calls[0].ID != "call_1" || calls[0].Type != "function" || calls[0].Function.Name != "terminal" ||
		calls[0].Function.Arguments != `{"command":"ls -la"}` {
		t.Fatalf("call 0 = %+v", calls[0])
	}
	if calls[1].Function.Name != "read_file" || calls[1].Function.Arguments != `{"path":"/etc/hosts"}` {
		t.Fatalf("call 1 = %+v", calls[1])
	}
}

func TestRetriesTransientFailuresThenSucceeds(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Status: 503, Body: "model swapping"},
		fakellm.Turn{Status: 429, RetryAfter: "0", Body: "slow down"},
		fakellm.Turn{Content: "ok"},
	)
	resp, err := client(fake).Complete(context.Background(), basicRequest(), nil)
	if err != nil || resp.Message.Content != "ok" {
		t.Fatalf("resp = %+v, err = %v", resp, err)
	}
	if fake.Count() != 3 {
		t.Fatalf("requests = %d, want 3", fake.Count())
	}
	// A retry resends the identical request.
	b := fake.Bodies()
	if !bytes.Equal(b[0], b[2]) {
		t.Fatal("a retried request must be byte-identical to the first attempt")
	}
}

func TestClientErrorsAreNotRetried(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Status: 400, Body: `{"error":"bad request"}`})
	_, err := client(fake).Complete(context.Background(), basicRequest(), nil)
	var httpErr *provider.HTTPError
	if !errors.As(err, &httpErr) || httpErr.Status != 400 || !strings.Contains(httpErr.Body, "bad request") {
		t.Fatalf("err = %v, want *provider.HTTPError 400", err)
	}
	if fake.Count() != 1 {
		t.Fatalf("requests = %d, want 1", fake.Count())
	}
}

func TestGivesUpAfterMaxAttempts(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Status: 503}, fakellm.Turn{Status: 503}, fakellm.Turn{Status: 503},
	)
	c := client(fake)
	c.Retry.MaxAttempts = 3
	_, err := c.Complete(context.Background(), basicRequest(), nil)
	var httpErr *provider.HTTPError
	if !errors.As(err, &httpErr) || httpErr.Status != 503 {
		t.Fatalf("err = %v, want the last 503", err)
	}
	if fake.Count() != 3 {
		t.Fatalf("requests = %d, want 3", fake.Count())
	}
}

func TestCancellationStopsTheRequest(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Hang: true})
	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(50*time.Millisecond, cancel)
	start := time.Now()
	_, err := client(fake).Complete(ctx, basicRequest(), nil)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
	if time.Since(start) > 2*time.Second {
		t.Fatal("cancellation took too long")
	}
}

func TestAnIncompleteStreamIsAnErrorAndNotRetried(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "partial answer here", ChunkRunes: 4, FailMidStream: true})
	if _, err := client(fake).Complete(context.Background(), basicRequest(), nil); err == nil {
		t.Fatal("a stream that ends without finishing must be an error")
	}
	if fake.Count() != 1 {
		t.Fatalf("requests = %d; output already reached the caller, so no retry", fake.Count())
	}
}

func TestTheSameRequestSerializesToTheSameBytes(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "a"}, fakellm.Turn{Content: "a"})
	c := client(fake)
	req := basicRequest()
	req.ReasoningEffort = "high"
	req.Tools = []provider.Tool{{Type: "function", Function: provider.ToolFunction{
		Name: "terminal", Description: "run a command",
		Parameters: []byte(`{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}`),
	}}}
	for range 2 {
		if _, err := c.Complete(context.Background(), req, nil); err != nil {
			t.Fatal(err)
		}
	}
	b := fake.Bodies()
	if !bytes.Equal(b[0], b[1]) {
		t.Fatalf("bodies differ:\n%s\n%s", b[0], b[1])
	}
}

func TestAToolCallAlwaysCarriesItsArguments(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "done"})
	req := basicRequest()
	req.Messages = append(req.Messages,
		provider.Message{Role: "assistant", ToolCalls: []provider.ToolCall{{
			ID: "c1", Type: "function",
			Function: provider.FunctionCall{Name: "list_things", Arguments: ""},
		}}},
		provider.Message{Role: "tool", ToolCallID: "c1", Name: "list_things", Content: "[]"},
	)
	if _, err := client(fake).Complete(context.Background(), req, nil); err != nil {
		t.Fatal(err)
	}
	if call := fake.Messages(0)[2]; !bytes.Contains(call, []byte(`"arguments":""`)) {
		t.Fatalf("assistant tool call = %s; a call must always carry its arguments key", call)
	}
}

func TestToolSchemasAreTyped(t *testing.T) {
	tool := provider.Tool{Type: "function", Function: provider.ToolFunction{Name: "terminal"}}
	if tool.Function.Name != "terminal" {
		t.Fatalf("tool = %+v", tool)
	}
}

func TestToolChoiceIsSentOnlyWhenSet(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "a"}, fakellm.Turn{Content: "b"})
	c := client(fake)
	req := basicRequest()
	req.ToolChoice = "none"
	if _, err := c.Complete(context.Background(), req, nil); err != nil {
		t.Fatal(err)
	}
	req.ToolChoice = ""
	if _, err := c.Complete(context.Background(), req, nil); err != nil {
		t.Fatal(err)
	}
	if fake.Request(0)["tool_choice"] != "none" {
		t.Fatalf("tool_choice = %v", fake.Request(0)["tool_choice"])
	}
	if _, present := fake.Request(1)["tool_choice"]; present {
		t.Fatal("an empty tool_choice must be omitted")
	}
}

func TestASilentStreamHitsTheIdleTimeout(t *testing.T) {
	// No byte at all: a transient failure, retried.
	fake := fakellm.New(t, fakellm.Turn{Hang: true}, fakellm.Turn{Content: "second try"})
	c := client(fake)
	c.IdleTimeout = 200 * time.Millisecond
	start := time.Now()
	resp, err := c.Complete(context.Background(), basicRequest(), nil)
	if err != nil || resp.Message.Content != "second try" || time.Since(start) > 5*time.Second {
		t.Fatalf("resp %+v, err %v, after %v", resp, err, time.Since(start))
	}

	// Output, then silence: an error, and no retry (output already streamed).
	stall := fakellm.New(t, fakellm.Turn{Content: "partial answer", ChunkRunes: 4, StallAfterFirst: true})
	c = client(stall)
	c.IdleTimeout = 200 * time.Millisecond
	start = time.Now()
	if _, err := c.Complete(context.Background(), basicRequest(), func(provider.Delta) {}); err == nil {
		t.Fatal("a stream that goes silent mid-answer must fail")
	}
	if stall.Count() != 1 || time.Since(start) > 5*time.Second {
		t.Fatalf("requests %d after %v", stall.Count(), time.Since(start))
	}
}

func TestTheDefaultIdleTimeoutLeavesRoomToThink(t *testing.T) {
	if c := provider.New("http://x/v1", "k"); c.IdleTimeout < 5*time.Minute {
		t.Fatalf("IdleTimeout default %v: local reasoning models can be silent for minutes", c.IdleTimeout)
	}
}
