// Package agent is the tool-less agent loop: it builds each request from
// the stored transcript, streams one turn, and persists the turn only after
// the model has answered.
package agent

import (
	"context"
	"encoding/json"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// PromptBuilder produces the system prompt for a new session. It is called
// exactly once per NewSession and never again for that session.
type PromptBuilder func(ctx context.Context, s store.Session) (string, error)

// Runner creates and drives sessions against one client and model.
type Runner struct {
	Store           *store.Store
	Client          *provider.Client
	Model           string
	ReasoningEffort string
	Prompt          PromptBuilder
	Source          string
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
	return &Session{runner: r, id: sess.ID, prompt: text}, nil
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
	return &Session{runner: r, id: id, prompt: text}, nil
}

// Session is one conversation, bound to its stored transcript and prompt.
type Session struct {
	runner *Runner
	id     string
	prompt string
}

// ID returns the session ID.
func (s *Session) ID() string { return s.id }

// Event is one streaming update.
type Event struct {
	Kind string // "assistant.delta" | "reasoning.delta"
	Text string
}

// Result is the finished turn.
type Result struct {
	Content   string
	Reasoning string
	Usage     provider.Usage
}

// Send runs one turn: the request is built from the stored transcript (the
// system prompt, then every stored message in order), plus the new user
// message. Nothing is persisted until the model has answered; then the user
// message (with exactly the bytes sent) and the assistant message are
// appended in one call.
func (s *Session) Send(ctx context.Context, text string, onEvent func(Event)) (Result, error) {
	stored, err := s.runner.Store.Messages(ctx, s.id)
	if err != nil {
		return Result{}, err
	}
	req := provider.Request{
		Model:           s.runner.Model,
		ReasoningEffort: s.runner.ReasoningEffort,
		Messages:        make([]provider.Message, 0, len(stored)+2),
	}
	req.Messages = append(req.Messages, provider.Message{Role: "system", Content: s.prompt})
	for _, m := range stored {
		req.Messages = append(req.Messages, toWireMessage(m))
	}
	req.Messages = append(req.Messages, provider.Message{Role: "user", Content: text})

	resp, err := s.runner.Client.Complete(ctx, req, func(d provider.Delta) {
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

	_, err = s.runner.Store.AppendMessages(ctx, s.id,
		store.Message{Role: "user", Content: text, APIContent: text},
		store.Message{
			Role:      "assistant",
			Content:   resp.Message.Content,
			Reasoning: resp.Reasoning,
			ToolCalls: marshalToolCalls(resp.Message.ToolCalls),
		})
	if err != nil {
		return Result{}, err
	}
	return Result{
		Content:   resp.Message.Content,
		Reasoning: resp.Reasoning,
		Usage:     resp.Usage,
	}, nil
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
