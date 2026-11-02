// Package provider speaks the OpenAI-compatible chat completions API with
// streaming and bounded retries.
package provider

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Message is one conversation message, serialized exactly as the Python
// fork sends it (byte-stable across requests).
type Message struct {
	Role       string     `json:"role"`
	Content    string     `json:"content"`
	ToolCalls  []ToolCall `json:"tool_calls,omitempty"`
	ToolCallID string     `json:"tool_call_id,omitempty"`
	Name       string     `json:"name,omitempty"`
}

// ToolCall is one function call on an assistant message.
type ToolCall struct {
	ID       string       `json:"id"`
	Type     string       `json:"type"`
	Function FunctionCall `json:"function"`
}

// FunctionCall is the function payload of a tool call. Arguments is always
// serialized (no omitempty): a call must carry its arguments key even when
// empty.
type FunctionCall struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

// ToolFunction is the function payload of a tool schema.
type ToolFunction struct {
	Name        string          `json:"name"`
	Description string          `json:"description,omitempty"`
	Parameters  json.RawMessage `json:"parameters,omitempty"`
}

// Tool is a tool schema entry.
type Tool struct {
	Type     string       `json:"type"`
	Function ToolFunction `json:"function"`
}

// Request is one chat completion request.
type Request struct {
	Model           string
	Messages        []Message
	Tools           []Tool
	ReasoningEffort string
	ToolChoice      string
}

// Delta is one streaming fragment.
type Delta struct {
	Content   string
	Reasoning string
}

// Usage is the token usage report.
type Usage struct {
	PromptTokens     int
	CompletionTokens int
	ReasoningTokens  int
	CachedTokens     int
}

// Response is the finished stream.
type Response struct {
	Message      Message
	Reasoning    string
	FinishReason string
	Usage        Usage
}

// HTTPError is a non-2xx status from the endpoint.
type HTTPError struct {
	Status int
	Body   string

	retryAfter string
}

func (e *HTTPError) Error() string {
	return fmt.Sprintf("provider: HTTP %d: %s", e.Status, e.Body)
}

// RetryPolicy bounds the retry loop.
type RetryPolicy struct {
	MaxAttempts int
	BaseDelay   time.Duration
	MaxDelay    time.Duration
}

// Client is a chat completions client bound to one base URL.
type Client struct {
	BaseURL string
	APIKey  string
	Retry   RetryPolicy

	http *http.Client
}

// New returns a client with the default retry policy.
func New(baseURL, apiKey string) *Client {
	return &Client{
		BaseURL: baseURL,
		APIKey:  apiKey,
		Retry: RetryPolicy{
			MaxAttempts: 4,
			BaseDelay:   500 * time.Millisecond,
			MaxDelay:    8 * time.Second,
		},
		http: &http.Client{},
	}
}

// wireRequest is the exact JSON body sent on the wire.
type wireRequest struct {
	Model           string        `json:"model"`
	Messages        []Message     `json:"messages"`
	Tools           []Tool        `json:"tools,omitempty"`
	ReasoningEffort string        `json:"reasoning_effort,omitempty"`
	ToolChoice      string        `json:"tool_choice,omitempty"`
	Stream          bool          `json:"stream"`
	StreamOptions   streamOptions `json:"stream_options"`
}

type streamOptions struct {
	IncludeUsage bool `json:"include_usage"`
}

// Complete runs one streaming chat completion. onDelta receives every
// content and reasoning fragment in order. It returns when the stream ends
// cleanly; a stream that ends without a finish_reason and [DONE] is an
// error. Retries (429, 5xx, connection errors) happen only before any
// response byte reached onDelta.
func (c *Client) Complete(ctx context.Context, req Request, onDelta func(Delta)) (*Response, error) {
	body, err := json.Marshal(wireRequest{
		Model:           req.Model,
		Messages:        req.Messages,
		Tools:           req.Tools,
		ReasoningEffort: req.ReasoningEffort,
		ToolChoice:      req.ToolChoice,
		Stream:          true,
		StreamOptions:   streamOptions{IncludeUsage: true},
	})
	if err != nil {
		return nil, fmt.Errorf("provider: encoding request: %w", err)
	}

	attempts := c.Retry.MaxAttempts
	if attempts < 1 {
		attempts = 1
	}
	var lastErr error
	for attempt := 0; attempt < attempts; attempt++ {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		resp, delivered, retryAfter, err := c.doOnce(ctx, body, onDelta)
		if err == nil {
			return resp, nil
		}
		lastErr = err
		if delivered || !retryable(err) {
			return nil, err
		}
		if attempt == attempts-1 {
			break
		}
		if err := c.sleep(ctx, c.backoff(attempt, retryAfter)); err != nil {
			return nil, err
		}
	}
	return nil, lastErr
}

// doOnce performs a single request and fully reads the stream. It reports
// whether any delta reached onDelta and the Retry-After hint, if any.
func (c *Client) doOnce(ctx context.Context, body []byte, onDelta func(Delta)) (*Response, bool, string, error) {
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/chat/completions", bytes.NewReader(body))
	if err != nil {
		return nil, false, "", err
	}
	httpReq.Header.Set("Authorization", "Bearer "+c.APIKey)
	httpReq.Header.Set("Content-Type", "application/json")

	resp, err := c.http.Do(httpReq)
	if err != nil {
		return nil, false, "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		return nil, false, resp.Header.Get("Retry-After"), &HTTPError{
			Status:     resp.StatusCode,
			Body:       string(data),
			retryAfter: resp.Header.Get("Retry-After"),
		}
	}
	return parseStream(resp.Body, onDelta)
}

// sseChunk is one data: payload of the stream.
type sseChunk struct {
	Choices []struct {
		Delta struct {
			Content    string `json:"content"`
			ReasoningA string `json:"reasoning_content"`
			ReasoningB string `json:"reasoning"`
			ToolCalls  []struct {
				Index    int    `json:"index"`
				ID       string `json:"id"`
				Type     string `json:"type"`
				Function struct {
					Name      string `json:"name"`
					Arguments string `json:"arguments"`
				} `json:"function"`
			} `json:"tool_calls"`
		} `json:"delta"`
		FinishReason *string `json:"finish_reason"`
	} `json:"choices"`
	Usage *struct {
		PromptTokens            int `json:"prompt_tokens"`
		CompletionTokens        int `json:"completion_tokens"`
		CompletionTokensDetails struct {
			ReasoningTokens int `json:"reasoning_tokens"`
		} `json:"completion_tokens_details"`
		PromptTokensDetails struct {
			CachedTokens int `json:"cached_tokens"`
		} `json:"prompt_tokens_details"`
	} `json:"usage"`
}

func parseStream(r io.Reader, onDelta func(Delta)) (*Response, bool, string, error) {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	var (
		content, reasoning strings.Builder
		finish             string
		usage              Usage
		delivered          bool
		done               bool
		calls              = map[int]*ToolCall{}
	)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		payload := strings.TrimPrefix(line, "data: ")
		if payload == "[DONE]" {
			done = true
			break
		}
		var chunk sseChunk
		if err := json.Unmarshal([]byte(payload), &chunk); err != nil {
			return nil, delivered, "", fmt.Errorf("provider: bad SSE chunk %q: %w", payload, err)
		}
		if chunk.Usage != nil {
			usage = Usage{
				PromptTokens:     chunk.Usage.PromptTokens,
				CompletionTokens: chunk.Usage.CompletionTokens,
				ReasoningTokens:  chunk.Usage.CompletionTokensDetails.ReasoningTokens,
				CachedTokens:     chunk.Usage.PromptTokensDetails.CachedTokens,
			}
		}
		for _, ch := range chunk.Choices {
			if ch.FinishReason != nil {
				finish = *ch.FinishReason
			}
			d := ch.Delta
			if d.Content != "" {
				content.WriteString(d.Content)
				delivered = true
				if onDelta != nil {
					onDelta(Delta{Content: d.Content})
				}
			}
			razing := d.ReasoningA
			if razing == "" {
				razing = d.ReasoningB
			}
			if razing != "" {
				reasoning.WriteString(razing)
				delivered = true
				if onDelta != nil {
					onDelta(Delta{Reasoning: razing})
				}
			}
			for _, tc := range d.ToolCalls {
				call, ok := calls[tc.Index]
				if !ok {
					call = &ToolCall{}
					calls[tc.Index] = call
				}
				if tc.ID != "" {
					call.ID = tc.ID
				}
				if tc.Type != "" {
					call.Type = tc.Type
				}
				if tc.Function.Name != "" {
					call.Function.Name = tc.Function.Name
				}
				call.Function.Arguments += tc.Function.Arguments
			}
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, delivered, "", err
	}
	if !done || finish == "" {
		return nil, delivered, "", fmt.Errorf("provider: stream ended without finish_reason and [DONE]")
	}
	msg := Message{Role: "assistant", Content: content.String()}
	if len(calls) > 0 {
		idxs := make([]int, 0, len(calls))
		for i := range calls {
			idxs = append(idxs, i)
		}
		sort.Ints(idxs)
		for _, i := range idxs {
			msg.ToolCalls = append(msg.ToolCalls, *calls[i])
		}
	}
	return &Response{
		Message:      msg,
		Reasoning:    reasoning.String(),
		FinishReason: finish,
		Usage:        usage,
	}, delivered, "", nil
}

// backoff is the exponential delay before attempt+1, honoring a Retry-After
// in seconds and capped by MaxDelay.
func (c *Client) backoff(attempt int, retryAfter string) time.Duration {
	delay := c.Retry.BaseDelay
	for i := 0; i < attempt; i++ {
		delay *= 2
		if delay > c.Retry.MaxDelay {
			delay = c.Retry.MaxDelay
			break
		}
	}
	if delay > c.Retry.MaxDelay {
		delay = c.Retry.MaxDelay
	}
	if ra, ok := parseRetryAfterSeconds(retryAfter); ok && ra > delay {
		delay = ra
	}
	if delay > c.Retry.MaxDelay {
		delay = c.Retry.MaxDelay
	}
	return delay
}

func parseRetryAfterSeconds(s string) (time.Duration, bool) {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, false
	}
	secs, err := strconv.ParseFloat(s, 64)
	if err != nil || secs < 0 {
		return 0, false
	}
	return time.Duration(secs * float64(time.Second)), true
}

// sleep waits d or for ctx to finish, whichever comes first.
func (c *Client) sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return ctx.Err()
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// retryable reports whether an error from doOnce warrants another attempt.
func retryable(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return false
	}
	var httpErr *HTTPError
	if errors.As(err, &httpErr) {
		switch httpErr.Status {
		case 429, 500, 502, 503, 504:
			return true
		}
		return false
	}
	var netErr net.Error
	if errors.As(err, &netErr) {
		return true
	}
	var urlErr *url.Error
	if errors.As(err, &urlErr) {
		return true
	}
	return false
}
