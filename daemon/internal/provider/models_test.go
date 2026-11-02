package provider_test

// Phase 3a: model metadata from /models (the context window and effort
// vocabulary maitred reports), context-overflow errors, and the request
// fields the title call sends.

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

func TestModelInfoReadsContextLengthsAndEfforts(t *testing.T) {
	fake := fakellm.New(t)
	fake.SetModels("big", "vllm", "bare")
	fake.SetModelMeta("big", map[string]any{"context_length": 131072, "reasoning_efforts": []string{"low", "high"}})
	fake.SetModelMeta("vllm", map[string]any{"max_model_len": 32768})
	infos, err := client(fake).ModelInfo(context.Background())
	if err != nil || len(infos) != 3 {
		t.Fatalf("ModelInfo = %+v, %v", infos, err)
	}
	got := map[string]provider.ModelInfo{}
	for _, m := range infos {
		got[m.ID] = m
	}
	if m := got["big"]; m.ContextLength != 131072 || len(m.ReasoningEfforts) != 2 || m.ReasoningEfforts[1] != "high" {
		t.Fatalf("big = %+v", m)
	}
	if got["vllm"].ContextLength != 32768 {
		t.Fatalf("vllm = %+v; max_model_len is a context length too", got["vllm"])
	}
	if got["bare"].ContextLength != 0 {
		t.Fatalf("bare = %+v; no metadata means unknown (0)", got["bare"])
	}
	if ids, err := client(fake).Models(context.Background()); err != nil || len(ids) != 3 {
		t.Fatalf("Models still lists the ids: %v, %v", ids, err)
	}
}

func TestContextOverflowErrorsAreRecognized(t *testing.T) {
	for _, c := range []struct {
		status   int
		body     string
		overflow bool
		limit    int
	}{
		{400, `{"error":{"message":"This model's maximum context length is 32768 tokens. However, you requested 40000 tokens."}}`, true, 32768},
		{400, `{"error":"prompt is too long: 250000 tokens > 200000 maximum"}`, true, 200000},
		{400, `{"error":{"message":"the request exceeds the available context size, try increasing it","type":"exceed_context_size_error","n_prompt_tokens":9000,"n_ctx":8192}}`, true, 8192},
		{400, `{"error":"This model's max_model_len is 65536"}`, true, 65536},
		{413, `payload too large`, true, 0},
		{400, `{"error":"no such model: m"}`, false, 0},
		{503, `{"error":"model is loading"}`, false, 0},
	} {
		limit, ok := provider.ContextOverflow(&provider.HTTPError{Status: c.status, Body: c.body})
		if ok != c.overflow || limit != c.limit {
			t.Errorf("ContextOverflow(%d %s) = %d, %v; want %d, %v", c.status, c.body, limit, ok, c.limit, c.overflow)
		}
	}
	if _, ok := provider.ContextOverflow(context.Canceled); ok {
		t.Error("only an HTTP error can be an overflow")
	}
}

func TestRequestExtrasAreSentOnlyWhenSet(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "a"}, fakellm.Turn{Content: "b"})
	c := client(fake)
	req := basicRequest()
	temp := 0.3
	req.MaxTokens, req.Temperature = 64, &temp
	req.ResponseFormat = json.RawMessage(`{"type":"json_object"}`)
	if _, err := c.Complete(context.Background(), req, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Complete(context.Background(), basicRequest(), nil); err != nil {
		t.Fatal(err)
	}
	first := fake.Request(0)
	rf, _ := first["response_format"].(map[string]any)
	if first["max_tokens"] != 64.0 || first["temperature"] != 0.3 || rf["type"] != "json_object" {
		t.Fatalf("request = %v", first)
	}
	for _, k := range []string{"max_tokens", "temperature", "response_format"} {
		if _, present := fake.Request(1)[k]; present {
			t.Fatalf("%s must be omitted when unset", k)
		}
	}
}
