package server

// The relay (phase 5c). A chat hands work to workers: ordinary daemon
// sessions of kind worker, common scope, started by the chat that owns them.
// A terminal that attaches to one as driver has picked it up: chat sends then
// only queue, and its approvals and questions go to the terminal.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

const (
	lastReplyMax   = 500
	titleMax       = 60
	hoursEvery     = time.Minute
	whenTerminal   = "when the terminal that picked it up gets to it"
	whenAfterTurn  = "after the current turn"
	noWorkerBound  = "no worker is bound to this chat: start one with new_in (a directory), or pick one from list"
	workerUntitled = "untitled"
)

// ---- hours ----

// workerZone is the zone worker hours are read in: SON_OF_ANTON_TIMEZONE,
// else the machine's.
func workerZone() *time.Location {
	if name := strings.TrimSpace(os.Getenv("SON_OF_ANTON_TIMEZONE")); name != "" {
		if loc, err := time.LoadLocation(name); err == nil {
			return loc
		}
	}
	return prompt.LocalZone()
}

// inWorkerHours says whether workers may start work at t. With no hours
// configured they may at any time.
func (s *Server) inWorkerHours(t time.Time) bool {
	start, end, ok, _ := s.cfg.WorkerHours()
	if !ok {
		return true
	}
	h := t.In(workerZone()).Hour()
	if start < end {
		return h >= start && h < end
	}
	return h >= start || h < end
}

// whenHours is what a send outside the hours is told ("" within them).
func (s *Server) whenHours(t time.Time) string {
	if s.inWorkerHours(t) {
		return ""
	}
	start, _, _, _ := s.cfg.WorkerHours()
	return fmt.Sprintf("queued until %02d:00", start)
}

// watchWorkerHours starts the queues of idle workers now and then every
// minute, so a queue held back by the hours (or left by a restart) runs once
// they begin.
func (s *Server) watchWorkerHours() {
	s.turns.Add(1)
	go func() {
		defer s.turns.Done()
		t := time.NewTicker(hoursEvery)
		defer t.Stop()
		for {
			s.startWaitingWorkers()
			select {
			case <-s.base.Done():
				return
			case <-t.C:
			}
		}
	}()
}

func (s *Server) startWaitingWorkers() {
	if s.base.Err() != nil || !s.inWorkerHours(time.Now()) {
		return
	}
	ids, err := s.st.QueuedWorkers(s.base)
	if err != nil {
		log.Printf("relay: listing workers with a queue: %v", err)
		return
	}
	for _, id := range ids {
		l, err := s.load(s.base, id)
		if err != nil {
			log.Printf("relay: loading worker %s: %v", id, err)
			continue
		}
		l.startQueue()
		s.maybeUnload(l)
	}
}

// startQueue runs the oldest queued message of an idle worker nobody has
// picked up.
func (l *live) startQueue() {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.unloaded || l.closing || l.running || l.compacting || l.driver != nil {
		return
	}
	l.waiting = false
	l.next()
}

// ---- the worker's side ----

// workerLink is a live worker's chatSink: it tells the chat that owns the
// worker how its turns end, and asks the chat for its approvals and
// questions unless a terminal has picked the worker up.
type workerLink struct {
	cs *chatState
	l  *live
}

func (k *workerLink) label() string {
	t := []rune(k.l.title)
	if len(t) == 0 {
		return workerUntitled
	}
	if len(t) > titleMax {
		t = t[:titleMax]
	}
	return string(t)
}

func (k *workerLink) turnStart() {}

func (k *workerLink) turnEnd(status, content, errMsg string) {
	head := "[worker " + k.label() + " (" + k.l.id + ")"
	var text string
	switch status {
	case "done":
		text = head + " finished]\n" + content
	case "error":
		text = head + " failed: " + errMsg + "]"
	case "interrupted":
		text = head + " was stopped]"
	default:
		return // a steered turn is followed by the steer's own
	}
	cs := k.cs
	cs.work.push(func() { cs.hub.notify(cs, text) })
}

func (k *workerLink) approval(id string, req approval.Request, choices []string) {
	if k.l.driver == nil {
		k.cs.askApproval(k.l, id, req, choices, "Worker "+k.label()+": ")
	}
}

func (k *workerLink) question(id string, q tools.Question) {
	if k.l.driver == nil {
		k.cs.askQuestion(k.l, id, q, "Worker "+k.label()+": ")
	}
}

func (k *workerLink) closed(id string) { k.cs.dropPending(id) }

// notify gives a chat's session a notice turn (queued if it is busy).
func (h *chatHub) notify(cs *chatState, text string) {
	s := h.srv
	cs.sessMu.Lock()
	defer cs.sessMu.Unlock()
	for range 3 {
		l, err := s.chatLive(s.base, cs)
		if err == nil {
			if err = l.notice(text); errors.Is(err, errUnloaded) {
				continue // a finishing turn closed it first
			}
		}
		if err != nil && s.base.Err() == nil {
			log.Printf("relay: telling %s: %v", cs.stamp, err)
		}
		return
	}
}

// notice is a notice turn: run now if the session is idle, else queued.
func (l *live) notice(text string) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.unloaded {
		return errUnloaded
	}
	if l.closing {
		return errors.New("the daemon is shutting down")
	}
	l.noticeLocked(text)
	return nil
}

// handBack gives a worker back to its chat after its terminal left (mu held):
// the chat is asked again for what was waiting, and the queue runs.
func (l *live) handBack() {
	if l.unloaded || l.closing || l.srv.base.Err() != nil {
		return
	}
	if l.chat != nil {
		for id, p := range l.approvals {
			l.chat.approval(id, p.req, p.choices)
		}
		for id, p := range l.questions {
			l.chat.question(id, p.q)
		}
	}
	l.waiting = false
	l.next()
}

// relaySent is how a send to a worker went.
type relaySent struct{ status, when, title string }

// relaySend gives a worker the message: queued if a terminal has picked it
// up or it is outside its hours, steered in with now, else run after the
// current turn or at once.
func (l *live) relaySend(text string, now bool) (relaySent, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.unloaded {
		return relaySent{}, errUnloaded
	}
	if l.closing {
		return relaySent{}, errors.New("the daemon is shutting down")
	}
	queue := func(when string) (relaySent, error) {
		if _, err := l.srv.st.QueuePush(context.Background(), l.id, "user", text); err != nil {
			return relaySent{}, err
		}
		l.emitQueue()
		return relaySent{"queued", when, l.title}, nil
	}
	busy := l.running || l.compacting
	switch {
	case l.driver != nil:
		return queue(whenTerminal)
	case l.srv.whenHours(time.Now()) != "":
		return queue(l.srv.whenHours(time.Now()))
	case busy && now && l.running:
		l.steerWith(text)
		return relaySent{"steered", "now", l.title}, nil
	case busy:
		return queue(whenAfterTurn)
	}
	// Idle: the message joins any queue left from before and the oldest runs.
	if _, err := l.srv.st.QueuePush(context.Background(), l.id, "user", text); err != nil {
		return relaySent{}, err
	}
	l.waiting = false
	l.next()
	l.emitQueue()
	return relaySent{"started", "now", l.title}, nil
}

// ---- the relay tool ----

type relayArgs struct {
	Action    string `json:"action"`
	Session   string `json:"session"`
	Message   string `json:"message"`
	NewIn     string `json:"new_in"`
	Now       bool   `json:"now"`
	Workspace string `json:"workspace"`
	Model     string `json:"model"`
}

func relayJSON(v map[string]any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return `{"success":false,"error":"unencodable result"}`
	}
	return string(b)
}

func relayFail(msg string) string { return relayJSON(map[string]any{"success": false, "error": msg}) }

// relay answers one relay tool call of the chat with the given stamp.
func (s *Server) relay(ctx context.Context, stamp string, raw json.RawMessage) string {
	var a relayArgs
	if err := json.Unmarshal(raw, &a); err != nil {
		return relayFail("invalid arguments: " + err.Error())
	}
	switch a.Action {
	case "list":
		return s.relayList(ctx, stamp, a)
	case "send":
		return s.relaySendTo(ctx, stamp, a)
	case "status", "stop", "detach":
		w, errText := s.relayWorker(ctx, stamp, a.Session)
		if errText != "" {
			return relayFail(errText)
		}
		switch a.Action {
		case "status":
			return s.relayStatus(ctx, w)
		case "stop":
			stopped := false
			if l := s.loaded(w.ID); l != nil {
				stopped = l.interrupt()
			}
			return relayJSON(map[string]any{"success": true, "stopped": stopped})
		}
		detached := false
		if id, err := s.st.RelayBinding(ctx, stamp); err == nil && id == w.ID {
			if err := s.st.SetRelayBinding(ctx, stamp, ""); err != nil {
				return relayFail(err.Error())
			}
			detached = true
		}
		return relayJSON(map[string]any{"success": true, "detached": detached})
	}
	return relayFail("action must be one of list, send, status, stop, detach")
}

// mayDrive says whether the chat may see and drive the worker: its own, or
// any when the instance is single-user.
func (s *Server) mayDrive(stamp string, w store.Session) bool {
	return w.Kind == "worker" && (s.cfg.Gateway.SingleUser || w.Owner == stamp)
}

// chatWorkers are the workers the chat may drive, newest activity first;
// workspace, when set, keeps those in that directory or below.
func (s *Server) chatWorkers(ctx context.Context, stamp, workspace string) ([]store.Summary, error) {
	sums, err := s.st.ListReadableIn(ctx, "common", "", "", []string{"worker"}, 0)
	if err != nil {
		return nil, err
	}
	var out []store.Summary
	for _, sum := range sums {
		if s.mayDrive(stamp, sum.Session) && within(sum.Session.Cwd, workspace) {
			out = append(out, sum)
		}
	}
	return out, nil
}

// within says whether dir is root or below it ("" root matches everything).
func within(dir, root string) bool {
	if root == "" {
		return true
	}
	rel, err := filepath.Rel(filepath.Clean(root), dir)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, "../")
}

// relayWorker finds the worker a call means: ref (an id, or a title matched
// without case) among the chat's workers, else the one bound to the chat.
func (s *Server) relayWorker(ctx context.Context, stamp, ref string) (store.Session, string) {
	ref = strings.TrimSpace(ref)
	if ref == "" {
		id, err := s.st.RelayBinding(ctx, stamp)
		if err == nil {
			if w, err := s.st.Session(ctx, id); err == nil && s.mayDrive(stamp, w) {
				return w, ""
			}
		}
		return store.Session{}, noWorkerBound
	}
	if w, err := s.st.Session(ctx, ref); err == nil && s.mayDrive(stamp, w) {
		return w, ""
	}
	ws, err := s.chatWorkers(ctx, stamp, "")
	if err != nil {
		return store.Session{}, err.Error()
	}
	for _, sum := range ws {
		if strings.EqualFold(sum.Session.Title, ref) {
			return sum.Session, ""
		}
	}
	return store.Session{}, "no worker " + ref + " for this chat"
}

// workerState is a worker's state for list and status: picked up (a terminal
// drives it), running, queued (with when it will run) or idle.
func (s *Server) workerState(id string, queued int) string {
	running, picked := false, false
	if l := s.loaded(id); l != nil {
		l.mu.Lock()
		running, picked = l.running || l.compacting, l.driver != nil
		l.mu.Unlock()
	}
	switch {
	case picked:
		return "picked up"
	case running:
		return "running"
	case queued > 0:
		when := s.whenHours(time.Now())
		if when == "" {
			when = "within a minute"
		}
		return "queued (" + when + ")"
	}
	return "idle"
}

func (s *Server) relayList(ctx context.Context, stamp string, a relayArgs) string {
	ws, err := s.chatWorkers(ctx, stamp, a.Workspace)
	if err != nil {
		return relayFail(err.Error())
	}
	bound, _ := s.st.RelayBinding(ctx, stamp)
	out := []map[string]any{}
	for _, sum := range ws {
		w := sum.Session
		items, _ := s.st.QueueItems(ctx, w.ID)
		entry := map[string]any{"worker": w.ID, "title": w.Title, "cwd": w.Cwd, "state": s.workerState(w.ID, len(items)),
			"bound": w.ID == bound, "last_active": sum.Updated.Format(time.RFC3339)}
		out = append(out, s.withModel(entry, w))
	}
	return relayJSON(map[string]any{"success": true, "workers": out})
}

func (s *Server) relayStatus(ctx context.Context, w store.Session) string {
	items, err := s.st.QueueItems(ctx, w.ID)
	if err != nil {
		return relayFail(err.Error())
	}
	queued := []string{}
	for _, it := range items {
		queued = append(queued, it.Text)
	}
	waiting := 0
	if l := s.loaded(w.ID); l != nil {
		l.mu.Lock()
		waiting = len(l.approvals)
		l.mu.Unlock()
	}
	reply := ""
	if rows, _, err := s.st.MessagesBefore(ctx, w.ID, 0, 40); err == nil {
		for i := len(rows) - 1; i >= 0; i-- {
			if rows[i].Role == "assistant" && strings.TrimSpace(rows[i].Content) != "" {
				reply = rows[i].Content
				break
			}
		}
	}
	if r := []rune(reply); len(r) > lastReplyMax {
		reply = string(r[:lastReplyMax])
	}
	res := map[string]any{"success": true, "worker": w.ID, "title": w.Title, "cwd": w.Cwd,
		"state": s.workerState(w.ID, len(items)), "queued": queued, "approvals_waiting": waiting, "last_reply": reply}
	return relayJSON(s.withModel(res, w))
}

func (s *Server) relaySendTo(ctx context.Context, stamp string, a relayArgs) string {
	msg := strings.TrimSpace(a.Message)
	if msg == "" {
		return relayFail("message is required")
	}
	model := strings.TrimSpace(a.Model)
	if model != "" {
		var err error
		if model, err = s.workerModel(model); err != nil {
			return relayFail(err.Error())
		}
	}
	var id string
	if dir := strings.TrimSpace(a.NewIn); dir != "" {
		if fi, err := os.Stat(dir); !filepath.IsAbs(dir) || err != nil || !fi.IsDir() {
			return relayFail(fmt.Sprintf("new_in %q must be an absolute path to an existing directory", dir))
		}
		var err error
		if id, _, err = s.createAs(ctx, "worker", stamp, dir, "relay"); err != nil {
			return relayFail("starting the worker: " + err.Error())
		}
	} else {
		w, errText := s.relayWorker(ctx, stamp, a.Session)
		if errText != "" {
			return relayFail(errText)
		}
		id = w.ID
	}
	if err := s.st.SetRelayBinding(ctx, stamp, id); err != nil {
		return relayFail(err.Error())
	}
	for range 3 {
		l, err := s.load(ctx, id)
		if err != nil {
			return relayFail(err.Error())
		}
		if model != "" {
			params, _ := json.Marshal(map[string]any{"model": model})
			if _, e := s.set(l, params, func(err error) (any, *rpcError) { return nil, errf(codeBadParams, "%v", err) }); e != nil {
				return relayFail("setting the worker's model: " + e.Message)
			}
		}
		sent, err := l.relaySend(msg, a.Now)
		if errors.Is(err, errUnloaded) {
			continue // another connection's detach closed it first
		}
		if err != nil {
			return relayFail(err.Error())
		}
		return relayJSON(map[string]any{"success": true, "worker": id, "title": sent.title, "status": sent.status, "when": sent.when})
	}
	return relayFail("the worker kept unloading")
}

// withModel adds the worker session's model to a list or status entry, and
// the model that served its latest reply when this daemon's life has one.
func (s *Server) withModel(m map[string]any, w store.Session) map[string]any {
	model := w.Model
	if l := s.loaded(w.ID); l != nil {
		l.mu.Lock()
		model = l.model
		l.mu.Unlock()
	}
	m["model"] = model
	if served := s.lastServed(w.ID); served != "" {
		m["served_model"] = served
	}
	return m
}
