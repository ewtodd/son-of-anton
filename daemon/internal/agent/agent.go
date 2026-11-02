package agent

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/paths"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// PromptBuilder produces the system prompt for a new session. It is called
// exactly once per NewSession and never again for that session.
type PromptBuilder func(ctx context.Context, s store.Session) (string, error)

// Loop defaults.
const (
	// DefaultMaxSteps bounds the tool steps per turn before a final
	// tool_choice=none request.
	DefaultMaxSteps = 500
	// DefaultMaxToolResultChars caps a tool result's size in context.
	DefaultMaxToolResultChars = 100_000
)

// Runner creates and drives sessions against one client and model.
type Runner struct {
	Store              *store.Store
	Client             *provider.Client
	Model              string
	ReasoningEffort    string
	Prompt             PromptBuilder
	Source             string
	Tools              *tools.Registry
	MaxSteps           int
	MaxToolResultChars int
	Policy             paths.Policy
	Mode               approval.Mode
	Approver           approval.Approver
	Allowlist          approval.Allowlist
}

// NewSession builds the prompt (exactly once), stores it, and creates the
// session row. The prompt builder sees the session it is building for: the
// kind, cwd, model and source that will be created.
func (r *Runner) NewSession(ctx context.Context, kind, cwd string) (*Session, error) {
	pending := store.Session{
		Kind:   kind,
		Source: r.Source,
		Cwd:    cwd,
		Model:  r.Model,
	}
	text, err := r.Prompt(ctx, pending)
	if err != nil {
		return nil, err
	}
	hash, err := r.Store.PutPrompt(ctx, text)
	if err != nil {
		return nil, err
	}
	pending.PromptHash = hash
	sess, err := r.Store.CreateSession(ctx, pending)
	if err != nil {
		return nil, err
	}
	return r.newSession(sess.ID, text, cwd)
}

// Resume loads a stored session and its prompt; the Prompt builder is never
// called.
func (r *Runner) Resume(ctx context.Context, id string) (*Session, error) {
	sess, err := r.Store.Session(ctx, id)
	if err != nil {
		return nil, err
	}
	text, err := r.Store.Prompt(ctx, sess.PromptHash)
	if err != nil {
		return nil, err
	}
	return r.newSession(id, text, sess.Cwd)
}

func (r *Runner) newSession(id, prompt, cwd string) (*Session, error) {
	state, err := tools.NewState(cwd)
	if err != nil {
		return nil, fmt.Errorf("agent: session state: %w", err)
	}
	mode := r.Mode
	if mode == "" {
		mode = approval.Default
	}
	return &Session{
		runner: r,
		id:     id,
		prompt: prompt,
		state:  state,
		gate:   approval.NewGate(mode, r.Approver, r.Allowlist),
	}, nil
}

// Session is one conversation, bound to its stored transcript and prompt.
type Session struct {
	runner *Runner
	id     string
	prompt string
	state  *tools.State
	gate   *approval.Gate
}

// ID returns the session ID.
func (s *Session) ID() string { return s.id }

// Close releases the session's terminal state.
func (s *Session) Close() error { return s.state.Close() }

// Event is one streaming update.
type Event struct {
	Kind   string // "assistant.delta" | "reasoning.delta" | "tool.start" | "tool.end"
	Text   string // the fragment, the call arguments, or the result
	Tool   string // tool name (tool.start / tool.end)
	CallID string // the call id (tool.start / tool.end)
}

// Result is the finished turn.
type Result struct {
	Content   string
	Reasoning string
	Usage     provider.Usage
}

// Send runs one turn: the request is built from the stored transcript (the
// system prompt, then every stored message in order), plus the new user
// message. The model may answer directly, or call tools; each tool step is
// persisted as it completes and the loop continues from the store, so a
// failure never loses a step.
func (s *Session) Send(ctx context.Context, text string, onEvent func(Event)) (Result, error) {
	r := s.runner
	stored, err := r.Store.Messages(ctx, s.id)
	if err != nil {
		return Result{}, err
	}
	msgs := buildStepMessages(s.prompt, stored, text)
	first := true
	steps := 0
	maxSteps := r.MaxSteps
	if maxSteps <= 0 {
		maxSteps = DefaultMaxSteps
	}
	maxChars := r.MaxToolResultChars
	if maxChars <= 0 {
		maxChars = DefaultMaxToolResultChars
	}
	for {
		req := provider.Request{
			Model:           r.Model,
			ReasoningEffort: r.ReasoningEffort,
			Messages:        msgs,
		}
		if r.Tools != nil {
			req.Tools = r.Tools.Schemas()
			if steps >= maxSteps {
				req.ToolChoice = "none"
			}
		}

		resp, err := r.Client.Complete(ctx, req, func(d provider.Delta) {
			if onEvent == nil {
				return
			}
			if d.Content != "" {
				onEvent(Event{Kind: "assistant.delta", Text: d.Content})
			}
			if d.Reasoning != "" {
				onEvent(Event{Kind: "reasoning.delta", Text: d.Reasoning})
			}
		})
		if err != nil {
			return Result{}, err
		}
		if len(resp.Message.ToolCalls) == 0 || req.ToolChoice == "none" {
			assistant := store.Message{
				Role:      "assistant",
				Content:   resp.Message.Content,
				Reasoning: resp.Reasoning,
			}
			if err := s.persistStep(ctx, text, first, assistant, nil); err != nil {
				return Result{}, err
			}
			return Result{Content: resp.Message.Content, Reasoning: resp.Reasoning, Usage: resp.Usage}, nil
		}
		rows := s.runToolCalls(ctx, resp.Message.ToolCalls, onEvent, maxChars)
		assistant := store.Message{
			Role:      "assistant",
			Content:   resp.Message.Content,
			Reasoning: resp.Reasoning,
			ToolCalls: marshalToolCalls(resp.Message.ToolCalls),
		}
		if err := s.persistStep(ctx, text, first, assistant, rows); err != nil {
			return Result{}, err
		}
		first = false
		steps++
		if ctx.Err() != nil {
			return Result{}, ctx.Err()
		}
		// Rebuild from the store: the next request replays exactly what
		// was persisted, byte for byte.
		stored, err = r.Store.Messages(ctx, s.id)
		if err != nil {
			return Result{}, err
		}
		msgs = buildStepMessages(s.prompt, stored, "")
	}
}

// runToolCalls executes every call in order. A cancelled context stops the
// remaining calls (each gets a cancellation result); the running call keeps
// whatever its tool returned.
func (s *Session) runToolCalls(ctx context.Context, calls []provider.ToolCall, onEvent func(Event), maxChars int) []store.Message {
	env := &tools.Env{Policy: s.runner.Policy, Gate: s.gate, State: s.state}
	rows := make([]store.Message, 0, len(calls))
	for _, call := range calls {
		if onEvent != nil {
			onEvent(Event{Kind: "tool.start", Tool: call.Function.Name, CallID: call.ID, Text: string(call.Function.Arguments)})
		}
		result := s.runOneCall(ctx, env, call, maxChars)
		if onEvent != nil {
			onEvent(Event{Kind: "tool.end", Tool: call.Function.Name, CallID: call.ID, Text: result})
		}
		rows = append(rows, store.Message{
			Role:       "tool",
			ToolCallID: call.ID,
			ToolName:   call.Function.Name,
			Content:    result,
			APIContent: result,
		})
	}
	return rows
}

func (s *Session) runOneCall(ctx context.Context, env *tools.Env, call provider.ToolCall, maxChars int) string {
	if ctx.Err() != nil {
		return errorJSON("cancelled: the user interrupted before this call ran")
	}
	if s.runner.Tools == nil {
		return errorJSON("unknown tool: " + call.Function.Name)
	}
	tool, ok := s.runner.Tools.Lookup(call.Function.Name)
	if !ok {
		return errorJSON("unknown tool: " + call.Function.Name)
	}
	if !json.Valid([]byte(call.Function.Arguments)) {
		return errorJSON("invalid arguments: not valid JSON")
	}
	out, err := tool.Run(ctx, env, json.RawMessage(call.Function.Arguments))
	if err != nil {
		return errorJSON(err.Error())
	}
	return truncateToolResult(out, maxChars)
}

// persistStep stores one completed step. It uses a context that is not
// cancelled: an interrupted turn must still keep its steps.
func (s *Session) persistStep(ctx context.Context, text string, first bool, assistant store.Message, toolRows []store.Message) error {
	msgs := make([]store.Message, 0, 2+len(toolRows))
	if first {
		msgs = append(msgs, store.Message{Role: "user", Content: text, APIContent: text})
	}
	msgs = append(msgs, assistant)
	msgs = append(msgs, toolRows...)
	_, err := s.runner.Store.AppendMessages(context.WithoutCancel(ctx), s.id, msgs...)
	return err
}

// buildStepMessages is the wire form of one request: the system prompt, the
// stored transcript in order, and (for the first request of a turn) the new
// user message. No synthetic messages.
func buildStepMessages(prompt string, stored []store.Message, userText string) []provider.Message {
	msgs := make([]provider.Message, 0, len(stored)+2)
	msgs = append(msgs, provider.Message{Role: "system", Content: prompt})
	for _, m := range stored {
		msgs = append(msgs, toWireMessage(m))
	}
	if userText != "" {
		msgs = append(msgs, provider.Message{Role: "user", Content: userText})
	}
	return msgs
}

// truncateToolResult caps a tool result at maxChars, keeping the head and
// the tail.
func truncateToolResult(out string, maxChars int) string {
	if maxChars <= 0 || len(out) <= maxChars {
		return out
	}
	head := maxChars * 2 / 5
	tail := maxChars - head
	notice := fmt.Sprintf("\n\n... [TRUNCATED - %d chars omitted out of %d total] ...\n\n", len(out)-head-tail, len(out))
	return out[:head] + notice + out[len(out)-tail:]
}

func errorJSON(msg string) string {
	b, err := json.Marshal(map[string]string{"error": msg})
	if err != nil {
		return `{"error":"internal error"}`
	}
	return string(b)
}

// toWireMessage converts a stored transcript row into the wire form it was
// sent in: user messages replay api_content when present, assistant
// messages keep their tool calls, reasoning is never sent.
func toWireMessage(m store.Message) provider.Message {
	out := provider.Message{Role: m.Role, Content: m.Content}
	switch m.Role {
	case "user":
		if m.APIContent != "" {
			out.Content = m.APIContent
		}
	case "assistant":
		if len(m.ToolCalls) > 0 {
			var calls []provider.ToolCall
			if err := json.Unmarshal(m.ToolCalls, &calls); err == nil {
				out.ToolCalls = calls
			}
		}
	case "tool":
		out.ToolCallID = m.ToolCallID
		out.Name = m.ToolName
	}
	return out
}

func marshalToolCalls(calls []provider.ToolCall) json.RawMessage {
	if len(calls) == 0 {
		return nil
	}
	b, err := json.Marshal(calls)
	if err != nil {
		return nil
	}
	return b
}
