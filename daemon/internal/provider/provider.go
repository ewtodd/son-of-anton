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
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync/atomic"
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
	// Parts, when set, are sent as the content in place of Content (a user
	// message with an image, for vision_analyze); nothing else uses them.
	Parts []ContentPart `json:"-"`
}

// ContentPart is one part of a message's content array.
type ContentPart struct {
	Type     string    `json:"type"`
	Text     string    `json:"text,omitempty"`
	ImageURL *ImageURL `json:"image_url,omitempty"`
}

// ImageURL is an image part's URL (a data: URL when the image is inline).
type ImageURL struct {
	URL string `json:"url"`
}

// MarshalJSON writes Parts as the content array; a message without parts
// serializes exactly as before.
func (m Message) MarshalJSON() ([]byte, error) {
	if m.Parts == nil {
		type plain Message
		return json.Marshal(plain(m))
	}
	return json.Marshal(struct {
		Role       string        `json:"role"`
		Content    []ContentPart `json:"content"`
		ToolCalls  []ToolCall    `json:"tool_calls,omitempty"`
		ToolCallID string        `json:"tool_call_id,omitempty"`
		Name       string        `json:"name,omitempty"`
	}{m.Role, m.Parts, m.ToolCalls, m.ToolCallID, m.Name})
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
	// MaxTokens, Temperature and ResponseFormat are omitted when unset.
	MaxTokens      int
	Temperature    *float64
	ResponseFormat json.RawMessage
	// OnServed, when set, is told the model answering as soon as the
	// response headers name it (maitred's x-maitred-model), before any of
	// the stream; it is not called when they name none.
	OnServed func(model string)
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
	// ServedModel is the model that answered, from maitred's x-maitred-model
	// header (under auto, the one it routed to); empty when not reported.
	ServedModel string
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
	// IdleTimeout abandons an attempt when no byte arrives for this long,
	// from sending the request to the end of the stream. Zero disables it.
	IdleTimeout time.Duration

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
		IdleTimeout: 10 * time.Minute,
		http:        &http.Client{},
	}
}

// errIdle is an attempt abandoned for silence; before any output reached the
// caller it is retried like a connection error.
var errIdle = errors.New("provider: no data from the model within the idle timeout")

// idleReader resets the idle timer on every byte received.
type idleReader struct {
	r io.Reader
	t *time.Timer
	d time.Duration
}

func (i idleReader) Read(p []byte) (int, error) {
	n, err := i.r.Read(p)
	if n > 0 {
		i.t.Reset(i.d)
	}
	return n, err
}

// wireRequest is the exact JSON body sent on the wire.
type wireRequest struct {
	Model           string          `json:"model"`
	Messages        []Message       `json:"messages"`
	Tools           []Tool          `json:"tools,omitempty"`
	ReasoningEffort string          `json:"reasoning_effort,omitempty"`
	ToolChoice      string          `json:"tool_choice,omitempty"`
	MaxTokens       int             `json:"max_tokens,omitempty"`
	Temperature     *float64        `json:"temperature,omitempty"`
	ResponseFormat  json.RawMessage `json:"response_format,omitempty"`
	Stream          bool            `json:"stream"`
	StreamOptions   streamOptions   `json:"stream_options"`
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
		MaxTokens:       req.MaxTokens,
		Temperature:     req.Temperature,
		ResponseFormat:  req.ResponseFormat,
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
		resp, delivered, retryAfter, err := c.doOnce(ctx, body, req.OnServed, onDelta)
		if err == nil {
			return resp, nil
		}
		lastErr = err
		if delivered {
			return nil, &partialError{err}
		}
		if !retryable(err) {
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

// partialError marks a failure after some output reached the caller.
type partialError struct{ error }

func (e *partialError) Unwrap() error { return e.error }

// Fallbackable reports whether err from Complete is one a request may be
// sent again on another model after: retryable, and before any output.
func Fallbackable(err error) bool {
	var p *partialError
	return !errors.As(err, &p) && retryable(err)
}

// doOnce performs a single request and fully reads the stream. It reports
// whether any delta reached onDelta and the Retry-After hint, if any.
func (c *Client) doOnce(ctx context.Context, body []byte, onServed func(string), onDelta func(Delta)) (*Response, bool, string, error) {
	parent := ctx
	var idled atomic.Bool
	var timer *time.Timer
	if c.IdleTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithCancel(parent)
		defer cancel()
		timer = time.AfterFunc(c.IdleTimeout, func() { idled.Store(true); cancel() })
		defer timer.Stop()
	}
	resp, delivered, retryAfter, err := c.attempt(ctx, body, onServed, onDelta, timer, c.IdleTimeout)
	if err != nil && idled.Load() && parent.Err() == nil {
		err = errIdle
	}
	return resp, delivered, retryAfter, err
}

func (c *Client) attempt(ctx context.Context, body []byte, onServed func(string), onDelta func(Delta), timer *time.Timer, idle time.Duration) (*Response, bool, string, error) {
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
	served := resp.Header.Get("X-Maitred-Model")
	if served != "" && onServed != nil {
		onServed(served)
	}
	var rd io.Reader = resp.Body
	if timer != nil {
		timer.Reset(idle)
		rd = idleReader{r: resp.Body, t: timer, d: idle}
	}
	out, delivered, retryAfter, err := parseStream(rd, onDelta)
	if out != nil {
		out.ServedModel = served
	}
	return out, delivered, retryAfter, err
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
	if errors.Is(err, errIdle) {
		return true
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

// Models lists the endpoint's model ids: GET <base>/models, data[].id.
func (c *Client) Models(ctx context.Context) ([]string, error) {
	infos, err := c.ModelInfo(ctx)
	if err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(infos))
	for _, i := range infos {
		ids = append(ids, i.ID)
	}
	return ids, nil
}

func (c *Client) getModels(ctx context.Context) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(c.BaseURL, "/")+"/models", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.APIKey)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode/100 != 2 {
		return nil, &HTTPError{Status: resp.StatusCode, Body: string(body)}
	}
	return body, nil
}

// ModelInfo is one /models entry with the metadata an endpoint reports.
type ModelInfo struct {
	ID               string
	ContextLength    int
	ReasoningEfforts []string
}

// contextKeys are the fields servers use for a model's context window, in
// the order they are tried (../agent/model_metadata.py:259-271).
var contextKeys = []string{
	"context_length", "context_window", "context_size", "max_context_length",
	"max_position_embeddings", "max_model_len", "max_input_tokens",
	"max_sequence_length", "max_seq_len", "n_ctx_train", "n_ctx", "ctx_size",
}

// ModelInfo lists the endpoint's models with their context lengths and
// reasoning efforts: GET <base>/models. A context length the entry does not
// state is 0.
func (c *Client) ModelInfo(ctx context.Context) ([]ModelInfo, error) {
	body, err := c.getModels(ctx)
	if err != nil {
		return nil, err
	}
	var out struct {
		Data []map[string]json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	infos := make([]ModelInfo, 0, len(out.Data))
	for _, d := range out.Data {
		var info ModelInfo
		_ = json.Unmarshal(d["id"], &info.ID)
		for _, k := range contextKeys {
			var n float64
			if json.Unmarshal(d[k], &n) == nil && n >= 1 {
				info.ContextLength = int(n)
				break
			}
		}
		_ = json.Unmarshal(d["reasoning_efforts"], &info.ReasoningEfforts)
		infos = append(infos, info)
	}
	return infos, nil
}

// ContextOverflow reports whether err says the request exceeded the model's
// context window, and the limit it states (0 when it states none).
func ContextOverflow(err error) (limit int, ok bool) {
	var he *HTTPError
	if !errors.As(err, &he) {
		return 0, false
	}
	low := strings.ToLower(he.Body)
	if he.Status != 413 {
		if he.Status != 400 {
			return 0, false
		}
		named := false
		for _, p := range overflowPhrases {
			if strings.Contains(low, p) {
				named = true
				break
			}
		}
		if !named {
			return 0, false
		}
	}
	for _, re := range limitPatterns {
		if m := re.FindStringSubmatch(low); m != nil {
			if n, err := strconv.Atoi(m[1]); err == nil && n > 0 {
				return n, true
			}
		}
	}
	return 0, true
}

var overflowPhrases = []string{
	"context length", "maximum context", "context size", "context window",
	"prompt is too long", "max_model_len", "maximum model length",
	"exceed_context_size", "too many tokens",
}

var limitPatterns = []*regexp.Regexp{
	regexp.MustCompile(`maximum context length is (\d+)`),
	regexp.MustCompile(`max_model_len (?:is )?(\d+)`),
	regexp.MustCompile(`maximum model length (?:is )?(\d+)`),
	regexp.MustCompile(`context (?:length|size|window)(?: is| of|:) *(\d+)`),
	regexp.MustCompile(`>\s*(\d+)\s*max`),
	regexp.MustCompile(`"n_ctx"\s*:\s*(\d+)`),
}
