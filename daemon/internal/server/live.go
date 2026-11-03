package server

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

type stopReason int

const (
	stopNone stopReason = iota
	stopSteer
	stopInterrupt
)

// tailEv is one event of the running turn since its last committed row.
type tailEv struct {
	kind  string
	text  string // merged delta text
	line  []byte
	reqID string // an approval or question request still open
}

type pendingApproval struct {
	ch      chan approval.Choice
	choices []string
}

type answer struct {
	answers   []string
	withdrawn bool
}

type pendingQuestion struct{ ch chan answer }

type usageTotals struct{ input, output, cached, reasoning, calls int }

// live is a loaded session. Everything below mu changes only under mu.
type live struct {
	srv  *Server
	id   string
	sess *agent.Session // set once, before the live is shared

	mu                  sync.Mutex
	kind, cwd           string
	parent, agent       string // a child session's parent and subagent type
	model, effort, mode string
	created, updated    time.Time
	preview             string
	viewers             map[*conn]bool
	driver              *conn
	running             bool
	startedAt           time.Time
	turnCancel          context.CancelFunc
	stop                stopReason
	steer               string
	hasSteer            bool
	waiting             bool // the queue waits for a turn that ends done, or queue.steer
	closing, unloaded   bool
	tail                []tailEv
	announced           int64
	usage               usageTotals
	ctxTokens           int
	toolCalls           int // a child's tool calls so far, those of earlier runs included
	title               string
	compactions         int
	compacting          bool // a manual compaction is running
	ctxKnown            int  // the window last set on the agent session
	approvals           map[string]*pendingApproval
	questions           map[string]*pendingQuestion
	resolved            map[string]bool
	bg                  map[string]context.CancelFunc       // running background tasks of this session, by child id
	watchers            []func(status, text, errMsg string) // told when the session is idle after a turn (a task call waits here)
}

func newLive(s *Server, rec store.Session, sum store.Summary) *live {
	return &live{
		srv: s, id: rec.ID, kind: rec.Kind, cwd: rec.Cwd, parent: rec.ParentID, agent: rec.Agent, bg: map[string]context.CancelFunc{},
		created: rec.Created, updated: sum.Updated, preview: sum.Preview,
		usage: usageTotals{rec.Usage.Input, rec.Usage.Output, rec.Usage.Cached, rec.Usage.Reasoning, rec.Usage.Calls}, ctxTokens: rec.ContextTokens,
		viewers: map[*conn]bool{}, waiting: true,
		approvals: map[string]*pendingApproval{}, questions: map[string]*pendingQuestion{}, resolved: map[string]bool{},
	}
}

// ---- events (mu held) ----

// emit sends an event to every attached client and records it in the live
// tail.
func (l *live) emit(kind string, f map[string]any) {
	f["session"] = l.id
	f["kind"] = kind
	line := eventLine(f)
	for c := range l.viewers {
		c.send(line)
	}
	switch kind {
	case "session.updated", "queue.updated", "role", "notice", "approval.resolved", "question.resolved":
		return
	case "assistant.delta", "reasoning.delta":
		if n := len(l.tail); n > 0 && l.tail[n-1].kind == kind {
			l.tail[n-1].text += f["text"].(string)
			return
		}
		l.tail = append(l.tail, tailEv{kind: kind, text: f["text"].(string)})
	default:
		l.tail = append(l.tail, tailEv{kind: kind, line: line})
	}
}

func (l *live) emitSession() {
	l.emit("session.updated", map[string]any{"state": l.sessionObj()})
}

func (l *live) emitQueue() {
	items, err := l.srv.st.QueueItems(context.Background(), l.id)
	if err != nil {
		l.emit("notice", map[string]any{"level": "warning", "text": "reading the queue: " + err.Error()})
	}
	l.emit("queue.updated", map[string]any{"items": queueObjs(items)})
}

// sendTail replays the running turn's events to c.
func (l *live) sendTail(c *conn) {
	for _, e := range l.tail {
		line := e.line
		if e.text != "" {
			line = eventLine(map[string]any{"session": l.id, "kind": e.kind, "text": e.text})
		}
		c.send(line)
	}
}

// ---- turns (mu held unless noted) ----

func (l *live) startTurn(text string) {
	l.nameSession(text)
	ctx, cancel := context.WithCancel(context.Background())
	l.running, l.startedAt, l.stop, l.turnCancel, l.tail = true, time.Now(), stopNone, cancel, nil
	l.emit("turn.start", map[string]any{"text": text})
	l.emitSession()
	l.srv.turns.Add(1)
	l.sess.Begin() // a steer or interrupt from here on reaches this Send
	go func() {
		defer l.srv.turns.Done()
		res, err := l.sess.Send(ctx, text, l.onAgent)
		cancel()
		l.finish(res, err)
		l.srv.indexAfterTurn(l.id)
	}()
}

// onAgent receives the agent loop's events (no lock held on entry).
func (l *live) onAgent(e agent.Event) {
	if p := l.onAgentLocked(e); p != nil {
		// A child's tool starting is news to its parent. The child's lock is
		// released first: the parent's is taken on its own.
		if parent := l.srv.loaded(l.parent); parent != nil {
			parent.mu.Lock()
			parent.emit("child.progress", p)
			parent.mu.Unlock()
		}
	}
}

// onAgentLocked handles e under the lock and returns the child.progress
// payload when e is a child's tool start.
func (l *live) onAgentLocked(e agent.Event) (progress map[string]any) {
	l.mu.Lock()
	defer l.mu.Unlock()
	switch e.Kind {
	case "assistant.delta", "reasoning.delta":
		l.emit(e.Kind, map[string]any{"text": e.Text})
	case "tool.start":
		summary := summarize(e.Tool, e.Text)
		l.emit("tool.start", map[string]any{"call_id": e.CallID, "name": e.Tool, "arguments": e.Text, "summary": summary})
		l.toolCalls++
		if l.parent != "" {
			progress = map[string]any{"child_id": l.id, "name": e.Tool, "summary": summary, "tool_calls": l.toolCalls}
		}
	case "tool.end":
		l.emit("tool.end", map[string]any{"call_id": e.CallID, "name": e.Tool, "result": e.Text,
			"is_error": isError(e.Tool, e.Text), "duration_ms": e.Duration.Milliseconds()})
	case "compaction":
		l.compacted(*e.Compaction)
	case "warning":
		l.emit("notice", map[string]any{"level": "warning", "text": e.Text})
	case "message":
		m := e.Message
		l.announced, l.updated = m.Seq, m.TS
		if l.preview == "" && m.Role == "user" {
			l.preview = store.Preview(m.Content)
		}
		l.emit("message", map[string]any{"message": messageObj(*m)})
		open := l.tail[:0]
		for _, t := range l.tail {
			if t.reqID != "" {
				open = append(open, t)
			}
		}
		l.tail = open
	}
	return progress
}

// finish records the end of a turn and decides what runs next.
func (l *live) finish(res agent.Result, err error) {
	l.mu.Lock()
	status, msg := "done", ""
	switch {
	case err == nil:
	case l.stop == stopSteer:
		status = "steered"
	case l.stop == stopInterrupt || errors.Is(err, agent.ErrStopped) || errors.Is(err, context.Canceled):
		status = "interrupted"
	default:
		status, msg = "error", err.Error()
	}
	l.usage.input += res.Usage.PromptTokens
	l.usage.output += res.Usage.CompletionTokens
	l.usage.cached += res.Usage.CachedTokens
	l.usage.reasoning += res.Usage.ReasoningTokens
	l.usage.calls += res.APICalls
	if res.APICalls > 0 {
		l.ctxTokens = res.Last.PromptTokens + res.Last.CompletionTokens
	}
	if err := l.srv.st.AddUsage(context.Background(), l.id, store.Usage{Input: res.Usage.PromptTokens, Output: res.Usage.CompletionTokens,
		Cached: res.Usage.CachedTokens, Reasoning: res.Usage.ReasoningTokens, Calls: res.APICalls}, l.ctxTokens); err != nil {
		l.emit("notice", map[string]any{"level": "warning", "text": "storing usage: " + err.Error()})
	}
	if n := l.sess.ContextLength(); n != l.ctxKnown {
		// An overflow error stated the real window: keep it for the session's life.
		l.ctxKnown = n
		l.srv.metaMu.Lock()
		l.srv.learned[l.id] = n
		l.srv.metaMu.Unlock()
	}
	end := map[string]any{"status": status, "usage": usageObj(res.Usage), "api_calls": res.APICalls}
	if msg != "" {
		end["error"] = msg
	}
	l.emit("turn.end", end)
	l.running, l.turnCancel, l.tail = false, nil, nil
	switch status {
	case "done":
		l.waiting = false
	case "interrupted", "error":
		l.waiting = true
	}
	l.emitSession()
	l.next()
	if !l.running {
		ws := l.watchers
		l.watchers = nil
		for _, w := range ws {
			w(status, res.Content, msg)
		}
	}
	l.mu.Unlock()
	l.srv.maybeUnload(l)
}

// next starts what follows an ended turn: a pending steer, then the oldest
// queue item unless the queue waits.
func (l *live) next() {
	if l.closing || l.running || l.compacting {
		return
	}
	if l.hasSteer {
		text := l.steer
		l.steer, l.hasSteer = "", false
		l.startTurn(text)
		return
	}
	if l.waiting {
		return
	}
	items, err := l.srv.st.QueueItems(context.Background(), l.id)
	if err != nil || len(items) == 0 {
		return
	}
	if err := l.srv.st.QueueRemove(context.Background(), l.id, items[0].ID); err != nil {
		l.emit("notice", map[string]any{"level": "error", "text": "taking the next queued message: " + err.Error()})
		return
	}
	l.emitQueue()
	l.startTurn(items[0].Text)
}

// send is turn.send (and /commit): start at once when idle, else queue or
// steer. Returns started, the queued item id, steered.
func (l *live) send(text, mode string) (bool, string, bool, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.closing {
		return false, "", false, errors.New("the daemon is shutting down")
	}
	switch {
	case !l.running && !l.compacting:
		l.startTurn(text)
		return true, "", false, nil
	case mode == "now" && l.running:
		l.steerWith(text)
		return false, "", true, nil
	default:
		it, err := l.srv.st.QueuePush(context.Background(), l.id, "user", text)
		if err != nil {
			return false, "", false, err
		}
		l.emitQueue()
		return false, it.ID, false, nil
	}
}

// steerWith ends the running turn after its running tools finish and runs
// text next, ahead of the queue.
func (l *live) steerWith(text string) {
	if l.hasSteer {
		l.steer += "\n\n" + text
	} else {
		l.steer, l.hasSteer = text, true
	}
	if l.stop == stopNone {
		l.stop = stopSteer
	}
	l.sess.Stop(true)
}

// steerQueue is queue.steer.
func (l *live) steerQueue(all bool) (started, steered bool, err error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.closing {
		return false, false, errors.New("the daemon is shutting down")
	}
	items, err := l.srv.st.QueueItems(context.Background(), l.id)
	if err != nil || len(items) == 0 {
		return false, false, err
	}
	if !all {
		items = items[:1]
	}
	texts, ids := make([]string, len(items)), make([]string, len(items))
	for i, it := range items {
		texts[i], ids[i] = it.Text, it.ID
	}
	if err := l.srv.st.QueueRemove(context.Background(), l.id, ids...); err != nil {
		return false, false, err
	}
	l.emitQueue()
	text := strings.Join(texts, "\n\n")
	if !l.running {
		l.startTurn(text)
		return true, false, nil
	}
	l.steerWith(text)
	return false, true, nil
}

// interrupt is turn.interrupt.
func (l *live) interrupt() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.interruptLocked()
}

func (l *live) interruptLocked() bool {
	stopped := false
	for id, cancel := range l.bg {
		cancel() // the background tasks go with the interrupt
		delete(l.bg, id)
		stopped = true
	}
	if !l.running {
		return stopped
	}
	l.stop, l.steer, l.hasSteer = stopInterrupt, "", false
	l.turnCancel()
	l.sess.Stop(false)
	for id, p := range l.approvals {
		l.resolveApproval(id, p, approval.Deny)
	}
	for id, q := range l.questions {
		delete(l.questions, id)
		l.closeRequest(id, "question.resolved", nil)
		q.ch <- answer{withdrawn: true}
	}
	return true
}

func (l *live) shutdown() {
	l.mu.Lock()
	l.closing = true
	l.interruptLocked()
	l.mu.Unlock()
}

// drainNotices turns background completion notices into turns or queue items.
func (l *live) drainNotices() {
	texts := l.sess.TakeNotifications()
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.closing || l.unloaded {
		return
	}
	for _, t := range texts {
		if !l.running && !l.compacting && !l.waiting {
			l.startTurn(t)
			continue
		}
		if _, err := l.srv.st.QueuePush(context.Background(), l.id, "notice", t); err != nil {
			l.emit("notice", map[string]any{"level": "error", "text": "queueing a notice: " + err.Error()})
			continue
		}
		l.emitQueue()
	}
}

// ---- approvals and questions ----

// closeRequest announces a request's end and drops it from the live tail.
func (l *live) closeRequest(id, kind string, f map[string]any) {
	if f == nil {
		f = map[string]any{}
	}
	f[map[string]string{"approval.resolved": "approval_id", "question.resolved": "question_id"}[kind]] = id
	l.resolved[id] = true
	open := l.tail[:0]
	for _, t := range l.tail {
		if t.reqID != id {
			open = append(open, t)
		}
	}
	l.tail = open
	l.emit(kind, f)
}

func (l *live) resolveApproval(id string, p *pendingApproval, c approval.Choice) {
	delete(l.approvals, id)
	l.closeRequest(id, "approval.resolved", map[string]any{"choice": string(c)})
	p.ch <- c
}

// request sends an approval.request or question.request and records it as
// open in the live tail.
func (l *live) request(kind, id string, f map[string]any) {
	f["session"], f["kind"] = l.id, kind
	line := eventLine(f)
	for c := range l.viewers {
		c.send(line)
	}
	l.tail = append(l.tail, tailEv{kind: kind, line: line, reqID: id})
}

// approve is the session's approval.Approver: it waits for the driver.
func (l *live) approve(ctx context.Context, req approval.Request) (approval.Choice, error) {
	return l.approveAs(ctx, req, "")
}

// approveAs is approve for a request of the child session child (”: this
// session's own); the request names it so the driver can show whose it is.
func (l *live) approveAs(ctx context.Context, req approval.Request, child string) (approval.Choice, error) {
	id := newID("ap_")
	choices := []string{"once", "deny"}
	if req.AllowRemember {
		choices = []string{"once", "session", "always", "deny"}
	}
	p := &pendingApproval{ch: make(chan approval.Choice, 1), choices: choices}
	l.mu.Lock()
	l.approvals[id] = p
	reasons := req.Reasons
	if reasons == nil {
		reasons = []string{}
	}
	f := map[string]any{"approval_id": id, "approval_kind": req.Kind, "subject": req.Subject, "reasons": reasons, "choices": choices}
	if child != "" {
		f["child_session"] = child
	}
	l.request("approval.request", id, f)
	l.mu.Unlock()
	select {
	case c := <-p.ch:
		return c, nil
	case <-ctx.Done():
		l.mu.Lock()
		if cur, ok := l.approvals[id]; ok {
			l.resolveApproval(id, cur, approval.Deny)
		}
		l.mu.Unlock()
		return <-p.ch, nil
	}
}

// ask is the session's tools.Questioner.
func (l *live) ask(ctx context.Context, q tools.Question) ([]string, error) {
	id := newID("qu_")
	p := &pendingQuestion{ch: make(chan answer, 1)}
	choices := q.Choices
	if choices == nil {
		choices = []string{}
	}
	l.mu.Lock()
	l.questions[id] = p
	l.request("question.request", id, map[string]any{"question_id": id, "text": q.Text, "choices": choices, "multi_select": q.MultiSelect})
	l.mu.Unlock()
	var a answer
	select {
	case a = <-p.ch:
	case <-ctx.Done():
		l.mu.Lock()
		if _, ok := l.questions[id]; ok {
			delete(l.questions, id)
			l.closeRequest(id, "question.resolved", nil)
		}
		l.mu.Unlock()
		return nil, ctx.Err()
	}
	if a.withdrawn {
		return nil, errors.New("the question was withdrawn")
	}
	return a.answers, nil
}

// answerApproval handles approval.answer (driver already checked).
func (l *live) answerApproval(id, choice string) *rpcError {
	l.mu.Lock()
	defer l.mu.Unlock()
	p, ok := l.approvals[id]
	if !ok {
		return l.notPending(id, "approval")
	}
	valid := false
	for _, c := range p.choices {
		valid = valid || c == choice
	}
	if !valid {
		return errf(codeBadParams, "choice %q is not offered (%s)", choice, strings.Join(p.choices, ", "))
	}
	l.resolveApproval(id, p, approval.Choice(choice))
	return nil
}

func (l *live) answerQuestion(id string, answers []string) *rpcError {
	l.mu.Lock()
	defer l.mu.Unlock()
	q, ok := l.questions[id]
	if !ok {
		return l.notPending(id, "question")
	}
	delete(l.questions, id)
	l.closeRequest(id, "question.resolved", nil)
	q.ch <- answer{answers: answers}
	return nil
}

func (l *live) notPending(id, what string) *rpcError {
	if l.resolved[id] {
		return errf(codeNotPending, "that %s is no longer pending", what)
	}
	return errf(codeNotFound, "no such %s %q", what, id)
}

// ---- attach ----

var errUnloaded = errors.New("session was unloaded")

// attach registers c and replies (through reply, under the lock, so the
// response precedes every later event) with the history; then the live tail.
func (l *live) attach(c *conn, driver bool, afterSeq *int64, limit int, reply func(any)) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.unloaded {
		return errUnloaded
	}
	var rows []store.Message
	var more, gap bool
	var err error
	ctx := context.Background()
	if afterSeq != nil {
		if rows, more, err = l.srv.st.MessagesAfter(ctx, l.id, *afterSeq, limit); err != nil {
			return err
		}
		if more {
			gap = true
			more = false
		}
	}
	if afterSeq == nil || gap {
		if rows, more, err = l.srv.st.MessagesBefore(ctx, l.id, 0, limit); err != nil {
			return err
		}
	}
	items, err := l.srv.st.QueueItems(ctx, l.id)
	if err != nil {
		return err
	}
	l.viewers[c] = true
	if driver {
		if l.driver != nil && l.driver != c {
			l.driver.send(eventLine(map[string]any{"session": l.id, "kind": "role", "as": "viewer"}))
		}
		l.driver = c
	} else if l.driver == c {
		l.driver = nil
	}
	role := "viewer"
	if l.driver == c {
		role = "driver"
	}
	var shown []messageJSON
	for _, m := range rows {
		if m.Seq <= l.announced { // a row not yet announced arrives as an event
			shown = append(shown, messageObj(m))
		}
	}
	if shown == nil {
		shown = []messageJSON{}
	}
	comps, err := l.srv.st.Compactions(ctx, l.id)
	if err != nil {
		return err
	}
	reply(map[string]any{"session": l.sessionObj(), "compactions": compactionObjs(comps), "messages": shown, "more_before": more, "gap": gap, "queue": queueObjs(items), "role": role})
	if l.running {
		l.sendTail(c)
	}
	return nil
}

func (l *live) detach(c *conn) {
	l.mu.Lock()
	defer l.mu.Unlock()
	delete(l.viewers, c)
	if l.driver == c {
		l.driver = nil
	}
}

// isDriver reports whether c drives the session.
func (l *live) isDriver(c *conn) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.driver == c
}

// ---- objects ----

func (l *live) sessionObj() map[string]any {
	o := map[string]any{
		"id": l.id, "kind": l.kind, "title": l.title, "cwd": l.cwd,
		"context_length": l.sess.ContextLength(), "compactions": l.compactions,
		"model": l.model, "reasoning_effort": l.effort, "mode": l.mode,
		"status": "idle", "created": l.created.Format(time.RFC3339), "updated": l.updated.Format(time.RFC3339),
		"preview": l.preview, "context_tokens": l.ctxTokens,
		"usage": map[string]any{"input_tokens": l.usage.input, "output_tokens": l.usage.output,
			"cached_tokens": l.usage.cached, "reasoning_tokens": l.usage.reasoning, "api_calls": l.usage.calls},
		"background_processes": l.sess.BackgroundRunning(),
		"served_model":         l.sess.ServedModel(),
		"parent_id":            l.parent,
		"agent":                l.agent,
	}
	if l.running {
		o["status"] = "running"
		o["turn_started"] = l.startedAt.Format(time.RFC3339)
	}
	return o
}

func (l *live) snapshot() map[string]any {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.sessionObj()
}

type messageJSON = map[string]any

func messageObj(m store.Message) messageJSON {
	o := messageJSON{"seq": m.Seq, "role": m.Role, "content": m.Content, "reasoning": m.Reasoning,
		"tool_call_id": m.ToolCallID, "tool_name": m.ToolName, "ts": m.TS.Format(time.RFC3339Nano)}
	if len(m.ToolCalls) > 0 {
		var calls []struct {
			ID       string `json:"id"`
			Function struct {
				Name      string `json:"name"`
				Arguments string `json:"arguments"`
			} `json:"function"`
		}
		if json.Unmarshal(m.ToolCalls, &calls) == nil {
			flat := make([]map[string]string, len(calls))
			for i, c := range calls {
				flat[i] = map[string]string{"id": c.ID, "name": c.Function.Name, "arguments": c.Function.Arguments}
			}
			o["tool_calls"] = flat
		}
	}
	return o
}

func queueObjs(items []store.QueueItem) []map[string]any {
	out := make([]map[string]any, len(items))
	for i, it := range items {
		out[i] = map[string]any{"id": it.ID, "kind": it.Kind, "text": it.Text}
	}
	return out
}
