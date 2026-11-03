package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/lsp"
	"github.com/ewtodd/son-of-anton/daemon/internal/mcp"
	"github.com/ewtodd/son-of-anton/daemon/internal/memory"
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

// How a turn that did not end with a final answer is closed in the
// transcript, so it always ends on an assistant row.
const (
	InterruptedText = "[response interrupted]" // ../agent/agent_runtime_helpers.py:1597
	FailedText      = "[response failed]"
)

// ErrStopped is Send's error after Stop.
var ErrStopped = errors.New("agent: the turn was stopped")

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
	// AsyncDelivery says whether a background completion notice can ever be
	// delivered in this run; Ask answers clarify (nil: nobody can).
	AsyncDelivery bool
	Ask           tools.Questioner
	// Compaction configures summarizing a long transcript.
	Compaction Compaction
	// Recall returns the block of remembered facts and notes relevant to a
	// turn's text, "" for none; nil for lean runs.
	Recall func(ctx context.Context, text string) string
	// Memory backs the memory tool (nil: unavailable) for sessions of
	// MemoryScope; MemoryApproval is memory.write_approval.
	Memory         *memory.Store
	MemoryScope    string
	MemoryApproval bool
	// SearchMessages adds meaning-based matches to session_search (nil: off).
	SearchMessages tools.MessageSearch
	// WallChat walls session_search to one chat (tools.Env.WallChat).
	WallChat string
	// Web configures web_search and web_extract (tools.Env.Web).
	Web *tools.Web
	// Vision is where vision_analyze sends images (tools.Env.Vision); NoScripts
	// is why execute_code refuses to run (tools.Env.NoScripts), "" to allow it.
	Vision    *tools.Vision
	NoScripts string
	// Fallback, when set, takes a request that fails on Model after the
	// client's own retries (before any output); the next request tries Model
	// again. A session switched to another model has no fallback.
	Fallback *Fallback
	// MCP reaches the MCP servers (tools.Env.MCP).
	MCP *mcp.Manager
	// Cron backs the cronjob tool for this session (nil: unavailable).
	Cron *tools.Cron
	// LSP reports the errors an edit introduced (tools.Env.LSP).
	LSP *lsp.Service
	// Relay answers the relay tool (tools.Env.Relay).
	Relay func(ctx context.Context, args json.RawMessage) string
	// Owner stamps the sessions NewSession creates with the chat that started them.
	Owner string
	// Scope and Chat stamp the sessions NewSession creates ("" Scope is common).
	Scope, Chat string
}

// Fallback is the route a request is sent again on.
type Fallback struct {
	Client *provider.Client
	Model  string
	Effort string
}

// NewSession builds the prompt (exactly once), stores it, and creates the
// session row. The prompt builder sees the session it is building for: the
// kind, cwd, model and source that will be created.
func (r *Runner) NewSession(ctx context.Context, kind, cwd string) (*Session, error) {
	pending := store.Session{
		Kind:      kind,
		Source:    r.Source,
		Cwd:       cwd,
		Workspace: paths.Workspace(cwd),
		Model:     r.Model,
		Scope:     r.Scope,
		Chat:      r.Chat,
		Owner:     r.Owner,
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
	s, err := r.newSession(id, text, sess.Cwd)
	if err != nil {
		return nil, err
	}
	stored, err := r.Store.Messages(ctx, id)
	if err != nil {
		_ = s.Close()
		return nil, err
	}
	for i := len(stored) - 1; i >= 0; i-- {
		if stored[i].Role == "tool" && stored[i].ToolName == "todo" && s.state.RestoreTodos(stored[i].Content) == nil {
			break
		}
	}
	return s, nil
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
		client: r.Client,
		model:  r.Model,
		effort: r.ReasoningEffort,
		comp:   compState{ctxLen: r.Compaction.ContextLength, client: r.Compaction.Client, model: r.Compaction.Model},
	}, nil
}

// Session is one conversation, bound to its stored transcript and prompt.
type Session struct {
	runner *Runner
	id     string
	prompt string
	state  *tools.State
	gate   *approval.Gate

	mu            sync.Mutex // guards the fields below
	client        *provider.Client
	model, effort string
	served        string // the model the last response reported serving it
	turn          *turnCtl
	comp          compState
}

// turnCtl is the running turn's stop switches. A reserved turn (Begin, no
// Send yet) has no cancel funcs; a Stop on it is recorded for Send to apply.
type turnCtl struct {
	reqCancel, toolCancel context.CancelFunc
	stopped               atomic.Bool
	reserved              bool // Begin, no Send yet (under Session.mu)
	cancelTools           bool // a Stop while reserved did not let tools finish
}

// Begin reserves the next turn: a Stop from now until that Send ends applies
// to it, even one that lands before Send has started. A caller that runs
// Send on another goroutine calls Begin first, so no stop falls in between.
func (s *Session) Begin() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.turn == nil {
		s.turn = &turnCtl{reserved: true}
	}
}

// Stop ends the running turn at the next safe point; a no-op when idle. A
// model request in flight is cancelled. With finishTools, running tool calls
// finish and keep their results; without, they are cancelled. Calls that had
// not started are cancelled either way.
func (s *Session) Stop(finishTools bool) {
	s.mu.Lock()
	t := s.turn
	if t != nil && t.reserved {
		t.stopped.Store(true)
		t.cancelTools = t.cancelTools || !finishTools
		s.mu.Unlock()
		return
	}
	s.mu.Unlock()
	if t == nil {
		return
	}
	t.stopped.Store(true)
	t.reqCancel()
	if !finishTools {
		t.toolCancel()
	}
}

// SetModel changes the model client, name and reasoning effort from the next
// turn; the prompt and tools are untouched.
func (s *Session) SetModel(c *provider.Client, model, effort string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.client, s.model, s.effort = c, model, effort
	s.served = "" // known again after the next response
}

// SetMode changes the permission mode at once.
func (s *Session) SetMode(m approval.Mode) { s.gate.SetMode(m) }

// BackgroundRunning counts the running background processes.
func (s *Session) BackgroundRunning() int { return s.state.BackgroundRunning() }

// KillBackground kills the running background processes and returns how many.
func (s *Session) KillBackground() int { return s.state.KillBackground() }

// OnNotify registers f, called (on any goroutine) when a completion notice is
// queued. Drain with TakeNotifications.
func (s *Session) OnNotify(f func()) { s.state.SetNotify(f) }

// OnBackgroundExit registers f, called (on any goroutine) each time one of
// the session's background processes exits.
func (s *Session) OnBackgroundExit(f func()) { s.state.SetOnExit(f) }

// ID returns the session ID.
func (s *Session) ID() string { return s.id }

// Close releases the session's tool state, killing its background processes.
func (s *Session) Close() error { return s.state.Close() }

// Notify queues a completion notice (a background subagent finished) for the
// session.
func (s *Session) Notify(text string) { s.state.Notify(text) }

// TakeNotifications drains the queued background completion notices.
func (s *Session) TakeNotifications() []string { return s.state.TakeNotifications() }

// Event is one streaming update.
type Event struct {
	Kind       string         // "assistant.delta" | "reasoning.delta" | "tool.start" | "tool.end" | "message" | "compaction" | "warning"
	Text       string         // the fragment, the call arguments, or the result
	Tool       string         // tool name (tool.start / tool.end)
	CallID     string         // the call id (tool.start / tool.end)
	Message    *store.Message // the stored row (message)
	Duration   time.Duration  // how long the call ran (tool.end)
	Compaction *CompactResult // the finished compaction (compaction)
}

// Result is the finished turn.
type Result struct {
	Content   string
	Reasoning string
	// Usage sums every request of the turn; APICalls counts them.
	Usage    provider.Usage
	APICalls int
	// Last is the latest request's usage alone.
	Last provider.Usage
}

// Send runs one turn: the request is built from the stored transcript (the
// system prompt, then every stored message in order), plus the new user
// message. The model may answer directly, or call tools; each tool step is
// persisted as it completes and the loop continues from the store, so a
// failure never loses a step.
func (s *Session) Send(ctx context.Context, text string, onEvent func(Event)) (Result, error) {
	r := s.runner
	reqCtx, reqCancel := context.WithCancel(ctx)
	toolCtx, toolCancel := context.WithCancel(ctx)
	t := &turnCtl{reqCancel: reqCancel, toolCancel: toolCancel}
	s.mu.Lock()
	reserved := s.turn
	if s.comp.compacting {
		s.mu.Unlock()
		reqCancel()
		toolCancel()
		return Result{}, errors.New("agent: a compaction is running")
	}
	if reserved != nil && !reserved.reserved {
		s.mu.Unlock()
		reqCancel()
		toolCancel()
		return Result{}, errors.New("agent: a turn is already running")
	}
	s.turn = t
	client, model, effort := s.client, s.model, s.effort
	s.mu.Unlock()
	// A Stop that landed between Begin and here applies now; none can reach
	// the reservation any more.
	if reserved != nil && reserved.stopped.Load() {
		t.stopped.Store(true)
		reqCancel()
		if reserved.cancelTools {
			toolCancel()
		}
	}
	defer func() {
		s.mu.Lock()
		s.turn = nil
		s.mu.Unlock()
		reqCancel()
		toolCancel()
	}()
	emit := func(e Event) {
		if onEvent != nil {
			onEvent(e)
		}
	}

	// What recall returns for this turn rides the user message, stored with it
	// so every later request replays the same bytes.
	apiText := text
	if r.Recall != nil {
		if block := r.Recall(ctx, text); block != "" {
			apiText = text + "\n\n" + block
		}
	}
	msgs, err := s.build(ctx, apiText)
	if err != nil {
		return Result{}, err
	}
	// began stamps the user message, so a turn's span runs from its start even
	// though its first step is stored whole when the step ends.
	began := time.Now()
	var total Result
	first := true
	autoTried, retried := false, false
	// pending is the user message not yet stored.
	pending := func() string {
		if first {
			return apiText
		}
		return ""
	}
	steps := 0
	maxSteps := r.MaxSteps
	if maxSteps <= 0 {
		maxSteps = DefaultMaxSteps
	}
	maxChars := r.MaxToolResultChars
	if maxChars <= 0 {
		maxChars = DefaultMaxToolResultChars
	}
	// stopped says whether the turn must end at the next safe point.
	stopped := func() bool { return t.stopped.Load() || ctx.Err() != nil }
	// closeTurn records a turn that did not end with a final answer: the
	// user message if it is not stored yet, then a closing assistant row.
	closeTurn := func(row store.Message, cause error) (Result, error) {
		if err := s.persistStep(ctx, text, apiText, first, began, row, nil, emit); err != nil {
			return total, err
		}
		return total, cause
	}
	stopErr := func() error {
		if t.stopped.Load() {
			return ErrStopped
		}
		return ctx.Err()
	}
	for {
		// A long transcript is compacted before the request, at most once a turn.
		if !autoTried && s.autoDue() {
			autoTried = true
			if s.autoCompact(reqCtx, emit) {
				if msgs, err = s.build(ctx, pending()); err != nil {
					return closeTurn(store.Message{Role: "assistant", Content: FailedText}, err)
				}
			}
		}
		req := provider.Request{
			Model:           model,
			ReasoningEffort: effort,
			Messages:        msgs,
		}
		if r.Tools != nil {
			req.Tools = r.Tools.Schemas()
			if steps >= maxSteps {
				req.ToolChoice = "none"
			}
		}

		var content, reasoning strings.Builder
		onDelta := func(d provider.Delta) {
			content.WriteString(d.Content)
			reasoning.WriteString(d.Reasoning)
			if d.Content != "" {
				emit(Event{Kind: "assistant.delta", Text: d.Content})
			}
			if d.Reasoning != "" {
				emit(Event{Kind: "reasoning.delta", Text: d.Reasoning})
			}
		}
		resp, err := client.Complete(reqCtx, req, onDelta)
		if fb := r.Fallback; err != nil && fb != nil && model == r.Model && !stopped() && provider.Fallbackable(err) {
			req.Model, req.ReasoningEffort = fb.Model, fb.Effort
			resp, err = fb.Client.Complete(reqCtx, req, onDelta)
			if err == nil && resp.ServedModel == "" {
				resp.ServedModel = fb.Model
			}
		}
		if err != nil {
			if !stopped() && !retried && s.overflowCompact(reqCtx, err, emit) {
				retried = true
				if msgs, err = s.build(ctx, pending()); err == nil {
					continue
				}
				return closeTurn(store.Message{Role: "assistant", Content: FailedText}, err)
			}
			if stopped() {
				// The streamed text, trimmed, closes the turn.
				row := store.Message{Role: "assistant", Content: strings.TrimSpace(content.String()), Reasoning: reasoning.String()}
				if row.Content == "" {
					row.Content = InterruptedText
				}
				return closeTurn(row, stopErr())
			}
			return closeTurn(store.Message{Role: "assistant", Content: FailedText}, err)
		}
		retried = false
		// Unconditional: a request back on the primary route after a fallback
		// must not keep naming the fallback model.
		s.mu.Lock()
		s.served = resp.ServedModel
		s.mu.Unlock()
		total.APICalls++
		s.noteResponse(resp.Usage.PromptTokens, emit)
		total.Last = resp.Usage
		total.Usage.PromptTokens += resp.Usage.PromptTokens
		total.Usage.CompletionTokens += resp.Usage.CompletionTokens
		total.Usage.ReasoningTokens += resp.Usage.ReasoningTokens
		total.Usage.CachedTokens += resp.Usage.CachedTokens
		if len(resp.Message.ToolCalls) == 0 || req.ToolChoice == "none" {
			assistant := store.Message{
				Role:      "assistant",
				Content:   resp.Message.Content,
				Reasoning: resp.Reasoning,
			}
			if err := s.persistStep(ctx, text, apiText, first, began, assistant, nil, emit); err != nil {
				return total, err
			}
			total.Content, total.Reasoning = resp.Message.Content, resp.Reasoning
			return total, nil
		}
		rows := s.runToolCalls(toolCtx, stopped, resp.Message.ToolCalls, emit, maxChars)
		assistant := store.Message{
			Role:      "assistant",
			Content:   resp.Message.Content,
			Reasoning: resp.Reasoning,
			ToolCalls: marshalToolCalls(resp.Message.ToolCalls),
		}
		if err := s.persistStep(ctx, text, apiText, first, began, assistant, rows, emit); err != nil {
			return total, err
		}
		first = false
		steps++
		if stopped() {
			return closeTurn(store.Message{Role: "assistant", Content: InterruptedText}, stopErr())
		}
		// Rebuild from the store: the next request replays exactly what
		// was persisted, byte for byte.
		if msgs, err = s.build(ctx, ""); err != nil {
			return closeTurn(store.Message{Role: "assistant", Content: FailedText}, err)
		}
	}
}

// runToolCalls executes the calls. A run of consecutive calls to tools that
// are tools.Concurrent runs at the same time; every other call runs alone,
// in order. Rows come back in the calls' order. Once the turn is stopped the
// calls not yet started are not run (each gets a cancellation result); a
// running call keeps whatever its tool returned.
func (s *Session) runToolCalls(ctx context.Context, stopped func() bool, calls []provider.ToolCall, emitEvent func(Event), maxChars int) []store.Message {
	base := tools.Env{Policy: s.runner.Policy, Gate: s.gate, State: s.state, Ask: s.runner.Ask, AsyncDelivery: s.runner.AsyncDelivery, Model: s.ServedModel(),
		Memory: s.runner.Memory, MemoryScope: s.runner.MemoryScope, MemoryApproval: s.runner.MemoryApproval,
		Store: s.runner.Store, SessionID: s.id, SearchMessages: s.runner.SearchMessages, WallChat: s.runner.WallChat, Web: s.runner.Web, MCP: s.runner.MCP,
		Vision: s.runner.Vision, LSP: s.runner.LSP, NoScripts: s.runner.NoScripts, Cron: s.runner.Cron, Relay: s.runner.Relay}
	var emitMu sync.Mutex
	emit := func(e Event) {
		emitMu.Lock()
		defer emitMu.Unlock()
		emitEvent(e)
	}
	rows := make([]store.Message, len(calls))
	one := func(i int) {
		call := calls[i]
		env := base
		env.CallID = call.ID
		// A script run by execute_code calls the session's tools through here.
		env.Call = func(ctx context.Context, name string, args json.RawMessage) string {
			inner := base
			inner.CallID = call.ID
			return s.runOneCall(ctx, &inner, provider.ToolCall{ID: call.ID, Function: provider.FunctionCall{Name: name, Arguments: string(args)}}, maxChars)
		}
		emit(Event{Kind: "tool.start", Tool: call.Function.Name, CallID: call.ID, Text: string(call.Function.Arguments)})
		began := time.Now()
		var result string
		if stopped() {
			result = errorJSON("cancelled: the user interrupted before this call ran")
		} else {
			result = s.runOneCall(ctx, &env, call, maxChars)
		}
		emit(Event{Kind: "tool.end", Tool: call.Function.Name, CallID: call.ID, Text: result, Duration: time.Since(began)})
		// Web and MCP results are stored wrapped as data, so they replay byte for byte.
		stored := tools.WrapUntrusted(call.Function.Name, result)
		rows[i] = store.Message{
			Role:       "tool",
			ToolCallID: call.ID,
			ToolName:   call.Function.Name,
			Content:    stored,
			APIContent: stored,
		}
	}
	for i := 0; i < len(calls); {
		j := i
		for j < len(calls) && s.concurrent(calls[j].Function.Name) {
			j++
		}
		if j == i {
			one(i)
			i++
			continue
		}
		var wg sync.WaitGroup
		for k := i; k < j; k++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				one(k)
			}()
		}
		wg.Wait()
		i = j
	}
	return rows
}

// concurrent says whether the named tool may run beside its siblings.
func (s *Session) concurrent(name string) bool {
	if s.runner.Tools == nil {
		return false
	}
	t, ok := s.runner.Tools.Lookup(name)
	if !ok {
		return false
	}
	c, ok := t.(tools.Concurrent)
	return ok && c.Concurrent()
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

// persistStep stores one completed step and announces each stored row. It
// uses a context that is not cancelled: an interrupted turn must still keep
// its steps.
func (s *Session) persistStep(ctx context.Context, text, apiText string, first bool, began time.Time, assistant store.Message, toolRows []store.Message, emit func(Event)) error {
	msgs := make([]store.Message, 0, 2+len(toolRows))
	if first {
		msgs = append(msgs, store.Message{Role: "user", Content: text, APIContent: apiText, TS: began})
	}
	msgs = append(msgs, assistant)
	msgs = append(msgs, toolRows...)
	if _, err := s.runner.Store.AppendMessages(context.WithoutCancel(ctx), s.id, msgs...); err != nil {
		return err
	}
	for i := range msgs {
		m := msgs[i]
		emit(Event{Kind: "message", Message: &m})
	}
	return nil
}

// build loads the transcript and its latest compaction and returns the wire
// form of the next request. userText is the new user message of the turn's
// first request, "" after.
func (s *Session) build(ctx context.Context, userText string) ([]provider.Message, error) {
	stored, err := s.runner.Store.Messages(ctx, s.id)
	if err != nil {
		return nil, err
	}
	comps, err := s.runner.Store.Compactions(ctx, s.id)
	if err != nil {
		return nil, err
	}
	var last *store.Compaction
	if n := len(comps); n > 0 {
		last = &comps[n-1]
	}
	return buildStepMessages(s.promptText(), stored, last, userText), nil
}

// buildStepMessages is the wire form of one request: the system prompt, the
// summary of the latest compaction (joined to the first kept user row, which
// keeps roles alternating), the rows after it in order, and (for the first
// request of a turn) the new user message. No other synthetic messages.
func buildStepMessages(prompt string, stored []store.Message, comp *store.Compaction, userText string) []provider.Message {
	msgs := make([]provider.Message, 0, len(stored)+3)
	msgs = append(msgs, provider.Message{Role: "system", Content: prompt})
	if comp != nil {
		block := summaryBlock(comp.Summary)
		stored = rowsAfter(stored, comp.ThroughSeq)
		switch {
		case len(stored) > 0 && stored[0].Role == "user":
			first := toWireMessage(stored[0])
			first.Content = block + "\n\n" + first.Content
			msgs = append(msgs, first)
			stored = stored[1:]
		case len(stored) == 0 && userText != "":
			userText = block + "\n\n" + userText
		default:
			msgs = append(msgs, provider.Message{Role: "user", Content: block})
		}
	}
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

// ServedModel is the model that answered the latest request, as the endpoint
// reported it (maitred's x-maitred-model; under auto, the one it routed to),
// else the session's model.
func (s *Session) ServedModel() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.served != "" {
		return s.served
	}
	return s.model
}
