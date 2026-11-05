package server

// The controller of a physics run (phase 11c). One goroutine drives a run:
// it starts each work session as one turn of the worker session, writes the
// session's log and commits at its end, has the manager session review it
// (one turn, ending with the review tool), sends the feedback back to the
// same worker session, and scores the run when it is accepted or out of
// budget. The sessions are ordinary live sessions pinned loaded for the run;
// the controller learns of their usage and tool calls through the hooks in
// live.onAgentLocked, which run with the session's lock held and so only
// take the run's own lock.

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/physics"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/sandbox"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

type pendingCall struct{ tool, args string }

// physRun is one run's controller. Lock order: live.mu, then physRun.mu.
type physRun struct {
	s        *Server
	id       string
	spec     physics.Spec
	specText string
	lim      physics.Limits
	repo     physics.Repo
	events   *physics.EventLog
	fs       *sandbox.FS   // the only way the daemon touches the run directory once the worker exists
	done     chan struct{} // closed when the run ends or is interrupted

	callsPath, linesPath string
	worker, manager      *live

	mu         sync.Mutex
	rec        store.PhysicsRun
	activeFrom time.Time
	cur        *live  // the session whose turn is running
	session    int    // the work session going, or under review
	sessionTok int    // tokens of the work session going
	steered    bool   // the 90% steer was given in this work session
	pending    bool   // a budget steer waits for the next tool to end
	totalSent  bool   // the total or hours steer was given
	exhausted  string // total_tokens or hours, once spent
	limitStop  bool   // a work session was stopped by a limit
	stopReq    bool
	verdict    string
	feedback   string
	calls      map[string]pendingCall
	watchers   map[*conn]bool
	lines      []string
	ticking    chan struct{}
}

// init prepares the controller for rec; it is called before the run is
// visible to anyone else.
func (r *physRun) init(rec store.PhysicsRun) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.rec = rec
	r.fs = (sandbox.Policy{RunDir: rec.RunDir}).FS()
	r.events = physics.NewEventLog(r.fs, filepath.Join(rec.RunDir, "EVENT_LOG.jsonl"))
	r.callsPath = filepath.Join(r.s.physicsDir(), r.id+".calls.jsonl")
	r.linesPath = filepath.Join(r.s.physicsDir(), r.id+".log")
	r.calls = map[string]pendingCall{}
	r.activeFrom = time.Now()
	if b, err := os.ReadFile(r.linesPath); err == nil {
		r.lines = strings.Split(strings.TrimRight(string(b), "\n"), "\n")
	}
}

// begin starts driving the run (init was called).
func (r *physRun) begin() {
	r.s.turns.Add(1)
	go func() {
		defer r.s.turns.Done()
		r.loop()
	}()
}

func (r *physRun) snapshot() store.PhysicsRun {
	r.mu.Lock()
	defer r.mu.Unlock()
	rec := r.rec
	rec.Elapsed = r.elapsedLocked()
	return rec
}

func (r *physRun) elapsedLocked() float64 {
	return r.rec.Elapsed + time.Since(r.activeFrom).Seconds()
}

// save stores the run's row.
func (r *physRun) save() {
	r.mu.Lock()
	rec := r.rec
	rec.Elapsed = r.elapsedLocked()
	r.mu.Unlock()
	_ = r.s.st.SavePhysicsRun(context.Background(), rec)
}

// say records one line of the run's progress and tells its watchers.
func (r *physRun) say(line string) {
	r.mu.Lock()
	r.lines = append(r.lines, line)
	conns := r.watcherList()
	if f, err := os.OpenFile(r.linesPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600); err == nil {
		_, _ = f.WriteString(line + "\n")
		_ = f.Close()
	}
	r.mu.Unlock()
	ev := eventLine(map[string]any{"kind": "physics.line", "run": r.id, "text": line})
	for _, c := range conns {
		c.send(ev)
	}
}

func (r *physRun) watcherList() []*conn {
	out := make([]*conn, 0, len(r.watchers))
	for c := range r.watchers {
		out = append(out, c)
	}
	return out
}

// watch registers c and returns the lines so far and the state.
func (r *physRun) watch(c *conn) ([]string, string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.watchers[c] = true
	return append([]string{}, r.lines...), r.rec.State
}

func (r *physRun) unwatch(c *conn) {
	r.mu.Lock()
	delete(r.watchers, c)
	r.mu.Unlock()
}

// physicsUnwatch forgets a closed connection.
func (s *Server) physicsUnwatch(c *conn) {
	s.physMu.Lock()
	a := s.physActive
	s.physMu.Unlock()
	if a != nil {
		a.unwatch(c)
	}
}

func (r *physRun) setVerdict(verdict, feedback string) {
	r.mu.Lock()
	r.verdict, r.feedback = verdict, feedback
	r.mu.Unlock()
}

// requestStop is problem stop: the running turn is interrupted and the run
// scored.
func (r *physRun) requestStop() {
	r.mu.Lock()
	r.stopReq = true
	cur := r.cur
	r.mu.Unlock()
	if cur != nil {
		cur.interrupt()
	}
}

func (r *physRun) stopRequested() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.stopReq
}

// ---- the hooks (the session's lock is held) ----

// assess decides, under r.mu, what the limits ask of the worker now: steer it
// with the budget message, or stop its turn. Tokens of a work session: steer
// at 90%, stop at 125%. The total tokens and the hours: steer when spent,
// stop at 125% of the tokens and 110% of the hours.
func (r *physRun) assess() (steer, stop bool) {
	total := r.rec.Tokens
	hours := r.elapsedLocked() / 3600
	if r.exhausted == "" {
		switch {
		case r.lim.TotalTokens > 0 && total >= r.lim.TotalTokens:
			r.exhausted = "total_tokens"
		case r.lim.Hours > 0 && hours >= r.lim.Hours:
			r.exhausted = "hours"
		}
	}
	if r.lim.SessionTokens > 0 && r.sessionTok*4 >= r.lim.SessionTokens*5 ||
		r.lim.TotalTokens > 0 && total*4 >= r.lim.TotalTokens*5 ||
		r.lim.Hours > 0 && hours >= r.lim.Hours*1.1 {
		return false, true
	}
	if r.lim.SessionTokens > 0 && r.sessionTok*10 >= r.lim.SessionTokens*9 && !r.steered {
		r.steered, steer = true, true
	}
	if r.exhausted != "" && !r.totalSent {
		r.totalSent, steer = true, true
	}
	return steer, false
}

// applyStop ends the worker's turn at the next safe point, running commands
// allowed to finish (l.mu held).
func (r *physRun) applyStop(l *live) {
	r.mu.Lock()
	already := r.limitStop
	r.limitStop = true
	r.mu.Unlock()
	if already {
		return
	}
	l.stop, l.steer, l.hasSteer = stopInterrupt, "", false
	l.sess.Stop(true)
}

func (r *physRun) onUsage(l *live, u *provider.Usage, d time.Duration, finish string) {
	served := l.sess.ServedModel()
	r.mu.Lock()
	r.events.Add("llm_call", map[string]any{
		"agent": l.agent, "session": r.session, "model": l.model, "served_model": served,
		"input_tokens": u.PromptTokens, "cached_tokens": u.CachedTokens, "output_tokens": u.CompletionTokens,
		"duration_s": d.Seconds(), "stop_reason": finish,
	})
	used := max(0, u.PromptTokens-u.CachedTokens) + u.CompletionTokens
	r.rec.Tokens += used
	var steer, stop bool
	if l.agent == "worker" {
		r.sessionTok += used
		steer, stop = r.assess()
		if steer {
			r.pending = true
		}
	} else {
		r.assess()
	}
	r.mu.Unlock()
	if stop {
		r.applyStop(l)
	}
}

func (r *physRun) onToolStart(l *live, callID, tool, args string) {
	r.mu.Lock()
	r.calls[callID] = pendingCall{tool, args}
	r.mu.Unlock()
}

func (r *physRun) onToolEnd(l *live, callID, tool, result string, d time.Duration) {
	var res struct {
		Output   string `json:"output"`
		ExitCode *int   `json:"exit_code"`
	}
	_ = json.Unmarshal([]byte(result), &res)
	r.mu.Lock()
	pc := r.calls[callID]
	delete(r.calls, callID)
	fields := map[string]any{"agent": l.agent, "session": r.session, "tool": tool, "duration_s": d.Seconds()}
	if tool == "terminal" && res.ExitCode != nil {
		fields["exit_code"] = *res.ExitCode
	}
	r.events.Add("tool_call", fields)
	if l.agent == "worker" {
		out := res.Output
		if tool != "terminal" {
			out = result
		}
		c := physics.Call{Session: r.session, Tool: tool, Summary: callSummary(tool, pc.args), Duration: d.Seconds(), Output: out}
		if tool == "terminal" {
			c.ExitCode = res.ExitCode
		}
		if b, err := json.Marshal(c); err == nil {
			if f, err := os.OpenFile(r.callsPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600); err == nil {
				_, _ = f.Write(append(b, '\n'))
				_ = f.Close()
			}
		}
	}
	steer := r.pending && l.agent == "worker"
	if steer {
		r.pending = false
	}
	r.mu.Unlock()
	if steer {
		l.steerWith(physics.BudgetMessage)
	}
}

// callSummary is the command or the main argument of a tool call.
func callSummary(tool, args string) string {
	var a map[string]any
	_ = json.Unmarshal([]byte(args), &a)
	s := func(k string) string { v, _ := a[k].(string); return v }
	var out string
	switch tool {
	case "terminal":
		out = s("command")
	case "read_file", "write_file", "patch":
		out = s("path")
	case "search_files":
		out = s("pattern")
	case "process":
		out = strings.TrimSpace(s("action") + " " + s("session_id"))
	}
	if out == "" {
		out = args
	}
	return out
}

// readCalls are the tool calls of work session n, in order.
func (r *physRun) readCalls(n int) []physics.Call {
	b, err := os.ReadFile(r.callsPath)
	if err != nil {
		return nil
	}
	var out []physics.Call
	for _, line := range strings.Split(string(b), "\n") {
		var c physics.Call
		if line != "" && json.Unmarshal([]byte(line), &c) == nil && c.Session == n {
			out = append(out, c)
		}
	}
	return out
}

// ---- the loop ----

type step int

const (
	stepNext step = iota
	stepScore
	stepStopped
	stepInterrupted
)

func (r *physRun) loop() {
	var err error
	if r.worker, err = r.pin(r.rec.Worker); err == nil {
		r.manager, err = r.pin(r.rec.Manager)
	}
	if err != nil {
		r.interrupt("loading the sessions: " + err.Error())
		return
	}
	r.ticking = make(chan struct{})
	go r.tick(r.ticking)
	for {
		var next step
		var why string
		switch {
		case r.s.base.Err() != nil:
			next = stepInterrupted
		case r.stopRequested():
			next = stepStopped
		case r.snapshot().State == "reviewing":
			next, why = r.reviewStep()
		default:
			next, why = r.workStep()
		}
		switch next {
		case stepScore:
			r.finalize("")
			return
		case stepStopped:
			r.finalize("stopped")
			return
		case stepInterrupted:
			r.interrupt(why)
			return
		}
	}
}

// tick enforces the hours while a turn runs.
func (r *physRun) tick(stop <-chan struct{}) {
	t := time.NewTicker(250 * time.Millisecond)
	defer t.Stop()
	for {
		select {
		case <-stop:
			return
		case <-t.C:
		}
		r.mu.Lock()
		l := r.cur
		var steer, stop bool
		if l != nil && l.agent == "worker" {
			steer, stop = r.assess()
		}
		r.mu.Unlock()
		if l == nil || !(steer || stop) {
			continue
		}
		l.mu.Lock()
		if l.running {
			if stop {
				r.applyStop(l)
			} else {
				l.steerWith(physics.BudgetMessage)
			}
		}
		l.mu.Unlock()
	}
}

// outOfBudget says whether the total tokens or the hours are spent.
func (r *physRun) outOfBudget() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.assess()
	return r.exhausted != ""
}

type turnEnd struct{ status, text, errMsg string }

// runTurn gives l's session text as a turn and waits for it to end.
func (r *physRun) runTurn(l *live, text string, started func()) (turnEnd, error) {
	ended := make(chan turnEnd, 1)
	watch := func(status, text, errMsg string) {
		select {
		case ended <- turnEnd{status, text, errMsg}:
		default:
		}
	}
	r.mu.Lock()
	r.cur = l
	r.mu.Unlock()
	defer func() {
		r.mu.Lock()
		r.cur = nil
		r.mu.Unlock()
	}()
	queued, err := l.runTask(text, watch)
	if err != nil {
		return turnEnd{}, err
	}
	if queued {
		return turnEnd{}, fmt.Errorf("session %s was busy", l.id)
	}
	if started != nil {
		started()
	}
	if r.stopRequested() {
		l.interrupt()
	}
	select {
	case e := <-ended:
		return e, nil
	case <-r.s.base.Done():
		select {
		case e := <-ended:
			return e, nil
		case <-time.After(15 * time.Second):
			return turnEnd{status: "interrupted"}, nil
		}
	}
}

// workStep runs one work session (a resumed one continues) and what follows
// it: the log, the format check and the commit.
func (r *physRun) workStep() (step, string) {
	if r.outOfBudget() {
		return stepScore, ""
	}
	r.mu.Lock()
	n := r.rec.SessionsDone + 1
	msg, resumed := r.rec.NextMessage, r.rec.WorkStarted
	if resumed {
		msg = physics.InterruptedMessage
	}
	r.startSession(n)
	r.rec.State = "working"
	r.mu.Unlock()
	r.save()
	r.say(fmt.Sprintf("work session %d started", n))

	te, err := r.runTurn(r.worker, msg, func() {
		r.mu.Lock()
		r.rec.WorkStarted = true
		r.mu.Unlock()
		r.save()
	})
	if err != nil {
		return stepInterrupted, "starting the work session: " + err.Error()
	}
	if r.s.base.Err() != nil {
		return stepInterrupted, ""
	}
	if te.status == "error" {
		return stepInterrupted, "the worker's turn failed: " + te.errMsg
	}
	if err := r.endWorkSession(n); err != nil {
		return stepInterrupted, err.Error()
	}
	switch {
	case r.stopRequested():
		return stepStopped, ""
	case r.outOfBudget():
		return stepScore, ""
	}
	return stepNext, ""
}

// startSession resets the per-session counters (r.mu held).
func (r *physRun) startSession(n int) {
	r.session, r.sessionTok = n, 0
	r.steered, r.pending, r.limitStop = false, false, false
}

// endWorkSession writes logs/session-N.md, checks RESULTS.txt's format and
// commits, and moves the run to its review.
func (r *physRun) endWorkSession(n int) error {
	// The run directory is the worker's: every access is beneath it. A refused
	// write (a symlink planted where the log goes) leaves no log; the run goes on.
	log := physics.SessionLog(n, r.readCalls(n))
	if _, _, err := r.fs.WriteAtomic(filepath.Join(r.rec.RunDir, "logs", fmt.Sprintf("session-%d.md", n)), log); err != nil {
		r.say(fmt.Sprintf("logs/session-%d.md was not written: %v", n, err))
	}
	first := strings.TrimSpace(strings.SplitN(strings.TrimSpace(r.closing(r.rec.Worker)), "\n", 2)[0])
	if rr := []rune(first); len(rr) > 200 {
		first = string(rr[:200])
	}
	if err := r.repo.Commit(fmt.Sprintf("session %d: %s", n, first)); err != nil {
		return err
	}
	r.mu.Lock()
	r.rec.SessionsDone, r.rec.State = n, "reviewing"
	tok := r.sessionTok
	r.mu.Unlock()
	r.save()
	r.say(fmt.Sprintf("work session %d ended: %d tokens", n, tok))
	return nil
}

// closing is the last message with text of a session ("" markers aside).
func (r *physRun) closing(session string) string {
	rows, _, err := r.s.st.MessagesBefore(context.Background(), session, 0, 60)
	if err == nil {
		for i := len(rows) - 1; i >= 0; i-- {
			c := strings.TrimSpace(rows[i].Content)
			if rows[i].Role == "assistant" && c != "" && c != agent.InterruptedText && c != agent.FailedText {
				return c
			}
		}
	}
	return "(no closing message)"
}

func (r *physRun) formatCheck() string {
	text, found := physics.ReadResults(r.fs, r.rec.RunDir)
	return physics.FormatCheck(r.spec.Keys(), text, found)
}

// reviewStep has the manager review the work session just ended.
func (r *physRun) reviewStep() (step, string) {
	if r.outOfBudget() {
		return stepScore, ""
	}
	r.mu.Lock()
	n := r.rec.SessionsDone
	r.startSession(n)
	r.verdict, r.feedback = "", ""
	r.mu.Unlock()
	check := r.formatCheck()
	msg := physics.ReviewMessage(n, r.lim.MaxSessions, r.closing(r.rec.Worker), check)

	te, err := r.runTurn(r.manager, msg, nil)
	if err != nil {
		return stepInterrupted, "starting the review: " + err.Error()
	}
	if r.s.base.Err() != nil {
		return stepInterrupted, ""
	}
	if r.stopRequested() {
		return stepStopped, ""
	}
	if te.status == "error" {
		return stepInterrupted, "the manager's turn failed: " + te.errMsg
	}
	r.mu.Lock()
	missing := r.verdict == ""
	r.mu.Unlock()
	if missing {
		if te, err = r.runTurn(r.manager, physics.ReminderMessage, nil); err != nil {
			return stepInterrupted, "starting the review: " + err.Error()
		}
		if r.s.base.Err() != nil {
			return stepInterrupted, ""
		}
		if r.stopRequested() {
			return stepStopped, ""
		}
		if te.status == "error" {
			return stepInterrupted, "the manager's turn failed: " + te.errMsg
		}
	}
	r.mu.Lock()
	verdict, feedback := r.verdict, r.feedback
	r.mu.Unlock()
	if verdict == "" {
		verdict, feedback = "continue", te.text
		if strings.TrimSpace(feedback) == "" {
			feedback = r.closing(r.rec.Manager)
		}
	}

	if err := r.fs.Append(filepath.Join(r.rec.RunDir, "REVIEWS.md"), fmt.Sprintf("## Review %d: %s\n\n%s\n\n", n, verdict, strings.TrimSpace(feedback))); err != nil {
		r.say("REVIEWS.md was not written: " + err.Error())
	}
	if err := r.repo.Commit(fmt.Sprintf("review %d: %s", n, verdict)); err != nil {
		return stepInterrupted, err.Error()
	}
	r.say(fmt.Sprintf("review %d: %s", n, verdict))

	if verdict == "accept" || n >= r.lim.MaxSessions || r.outOfBudget() {
		return stepScore, ""
	}
	r.mu.Lock()
	r.rec.NextMessage = physics.FeedbackMessage(n, r.lim.MaxSessions, strings.TrimSpace(feedback), check)
	r.rec.WorkStarted, r.rec.State = false, "working"
	r.mu.Unlock()
	r.save()
	return stepNext, ""
}

// ---- the end ----

func (r *physRun) release() {
	if r.ticking != nil {
		close(r.ticking)
		r.ticking = nil
	}
	for _, l := range []*live{r.worker, r.manager} {
		if l != nil {
			l.mu.Lock()
			l.run, l.pinned = nil, false
			l.mu.Unlock()
			r.s.maybeUnload(l)
		}
	}
	r.mu.Lock()
	conns := r.watcherList()
	state := r.rec.State
	r.watchers = map[*conn]bool{}
	r.mu.Unlock()
	ev := eventLine(map[string]any{"kind": "physics.end", "run": r.id, "state": state})
	for _, c := range conns {
		c.send(ev)
	}
	r.s.physMu.Lock()
	if r.s.physActive == r {
		r.s.physActive = nil
	}
	r.s.physMu.Unlock()
	close(r.done)
}

// finalize scores RESULTS.txt against the spec's checks, writes and commits
// FORMAL_EVAL.md, and ends the run in state passed or failed, or forced.
func (r *physRun) finalize(forced string) {
	text, found := physics.ReadResults(r.fs, r.rec.RunDir)
	eval := physics.Score(r.spec.Checks, text, found)
	state := eval.Status()
	if forced != "" {
		state = forced
	}
	_, _, werr := r.fs.WriteAtomic(filepath.Join(r.rec.RunDir, "FORMAL_EVAL.md"), eval.Markdown())
	if werr != nil {
		r.say("FORMAL_EVAL.md was not written: " + werr.Error())
	}
	cerr := r.repo.Commit("formal evaluation: " + eval.Status())
	if cerr != nil {
		r.say("scoring: " + cerr.Error())
	}
	// The lines come first: whoever sees the run end must find them.
	r.say(fmt.Sprintf("scored: %s (%d/%d)", eval.Status(), eval.Passed, len(eval.Results)))
	if forced != "" {
		r.say("run " + state)
	}
	r.mu.Lock()
	r.rec.State, r.rec.Phase, r.rec.Ended = state, "", time.Now()
	r.mu.Unlock()
	r.save()
	r.release()
}

// interrupt ends this activation of the run: it can be resumed.
func (r *physRun) interrupt(why string) {
	r.mu.Lock()
	r.rec.Phase, r.rec.State = r.rec.State, "interrupted"
	r.mu.Unlock()
	r.save()
	if why != "" {
		r.say("interrupted: " + why)
	} else {
		r.say("interrupted")
	}
	r.release()
}

// pin loads session id and keeps it loaded for the run, with the hooks set.
func (r *physRun) pin(id string) (*live, error) {
	for range 3 {
		l, err := r.s.load(context.Background(), id)
		if err != nil {
			return nil, err
		}
		l.mu.Lock()
		if l.unloaded {
			l.mu.Unlock()
			continue
		}
		l.run, l.pinned = r, true
		l.mu.Unlock()
		return l, nil
	}
	return nil, fmt.Errorf("session %s kept unloading", id)
}
