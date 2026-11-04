package server

// Signal chats (phase 5a). The daemon reads signal-cli's event stream, runs
// each chat as a chat session driven in-process, and answers in the chat. The
// session's approvals and questions go to the chat, never to a socket client,
// and no socket method can reach the session (see socketSession).

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"mime"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf16"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
	"github.com/ewtodd/son-of-anton/daemon/internal/signal"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

const (
	ackEvery      = 30 * time.Second
	typingEvery   = 5 * time.Second
	quoteMax      = 500
	queuedAck     = "Got it. This will be handled after the current task is done."
	commandsHelp  = "/new, /stop, /status, /model, /sendnow, /sendall, /approve, /deny"
	noApproval    = "No approval is pending."
	nothingToStop = "There is nothing running to stop."
)

// chatSink is what a chat session tells its chat. Every method is called with
// the session's lock held, so none may call back into the session or wait on
// the network.
type chatSink interface {
	turnStart()
	turnEnd(status, content, errMsg string)
	approval(id string, req approval.Request, choices []string)
	question(id string, q tools.Question)
	closed(id string)
}

// chatHub serves one instance's Signal chats.
type chatHub struct {
	srv     *Server
	cfg     signal.Config
	typing  bool
	cl      *signal.Client
	in      *signal.Intake
	release func()
	wg      sync.WaitGroup

	mu    sync.Mutex
	chats map[string]*chatState
}

// newChatHub returns the hub when Signal is on and configured, else nil.
func (s *Server) newChatHub() *chatHub {
	if !s.cfg.Daemon.Signal {
		return nil
	}
	cfg := signal.ConfigFromEnv(s.cfg.Getenv)
	if cfg.URL == "" || cfg.Account == "" {
		log.Printf("signal: [daemon] signal is on but SIGNAL_HTTP_URL or SIGNAL_ACCOUNT is not set; Signal stays off")
		return nil
	}
	if v, ok := config.SignalSetting(s.cfg.Platforms.Signal.RequireMention); ok {
		cfg.RequireMention = v
	}
	if p := s.cfg.Gateway.GroupSessionsPerUser; p != nil {
		cfg.PerUser = *p
	}
	typing := true
	if v, ok := config.SignalSetting(s.cfg.Platforms.Signal.TypingIndicator); ok {
		typing = v
	}
	return &chatHub{srv: s, cfg: cfg, typing: typing, cl: signal.NewClient(cfg.URL, cfg.Account),
		in: signal.NewIntake(cfg), chats: map[string]*chatState{}}
}

// start takes the account's lock and starts reading events; a live gateway
// holding the lock leaves Signal off.
func (h *chatHub) start() {
	release, err := signal.AcquireLock(h.cfg.Account)
	if err != nil {
		log.Printf("signal: not starting: %v", err)
		return
	}
	h.release = release
	h.wg.Add(1)
	go func() {
		defer h.wg.Done()
		_ = h.cl.Events(h.srv.base, h.handle)
	}()
}

// stop waits for the hub's goroutines (the server's context is already
// cancelled) and gives the account back.
func (h *chatHub) stop() {
	h.wg.Wait()
	if h.release != nil {
		h.release()
	}
}

// chatState is one chat: its serial queues and what waits for its answer.
type chatState struct {
	hub   *chatHub
	stamp string
	to    string
	out   *outbox // messages to the chat, in order
	work  *outbox // inbound messages, in order
	cmds  *outbox // control commands, in order

	sessMu sync.Mutex // picking, creating and replacing the chat's session

	mu      sync.Mutex
	pending []pendingReq
	ackAt   time.Time
	typing  context.CancelFunc
}

// pendingReq is an approval or a question waiting for the chat's answer.
type pendingReq struct {
	id       string
	l        *live
	question bool
	choices  []string // an approval's offered choices, or a question's options
}

func (h *chatHub) state(stamp string) *chatState {
	h.mu.Lock()
	defer h.mu.Unlock()
	if cs, ok := h.chats[stamp]; ok {
		return cs
	}
	cs := &chatState{hub: h, stamp: stamp, to: replyTarget(stamp), out: new(outbox), work: new(outbox), cmds: new(outbox)}
	for _, o := range []*outbox{cs.out, cs.work, cs.cmds} {
		h.wg.Add(1)
		go func() {
			defer h.wg.Done()
			o.run(h.srv.base)
		}()
	}
	h.chats[stamp] = cs
	return cs
}

// replyTarget is where a chat's replies go, from its stamp:
// signal:dm:<sender> or signal:group:<id>[:<sender>].
func replyTarget(stamp string) string {
	if rest, ok := strings.CutPrefix(stamp, "signal:group:"); ok {
		id, _, _ := strings.Cut(rest, ":")
		return "group:" + id
	}
	return strings.TrimPrefix(stamp, "signal:dm:")
}

// outbox runs functions one at a time, in the order pushed. push never
// blocks, so it is safe under a session's lock.
type outbox struct {
	mu   sync.Mutex
	q    []func()
	wake chan struct{}
	once sync.Once
}

func (o *outbox) init() { o.once.Do(func() { o.wake = make(chan struct{}, 1) }) }

func (o *outbox) push(f func()) {
	o.init()
	o.mu.Lock()
	o.q = append(o.q, f)
	o.mu.Unlock()
	select {
	case o.wake <- struct{}{}:
	default:
	}
}

func (o *outbox) run(ctx context.Context) {
	o.init()
	for {
		o.mu.Lock()
		var f func()
		if len(o.q) > 0 {
			f, o.q = o.q[0], o.q[1:]
		}
		o.mu.Unlock()
		if f != nil {
			f()
			continue
		}
		select {
		case <-ctx.Done():
			return
		case <-o.wake:
		}
	}
}

// ---- sending ----

// say sends plain text to the chat.
func (cs *chatState) say(text string) { cs.sayStyled(text, nil) }

func (cs *chatState) sayStyled(text string, styles []string) {
	cs.out.push(func() {
		h := cs.hub
		ts, err := h.cl.Send(h.srv.base, cs.to, text, styles)
		if err != nil {
			log.Printf("signal: sending to %s: %v", cs.to, err)
		}
		h.in.Sent(ts)
	})
}

// typingStart shows the typing indicator and keeps it up until typingStop.
func (cs *chatState) typingStart() {
	h := cs.hub
	if !h.typing {
		return
	}
	cs.out.push(func() { _ = h.cl.Typing(h.srv.base, cs.to, false) })
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if cs.typing != nil {
		return
	}
	ctx, cancel := context.WithCancel(h.srv.base)
	cs.typing = cancel
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case <-time.After(typingEvery):
				_ = h.cl.Typing(ctx, cs.to, false)
			}
		}
	}()
}

// typingStop ends the indicator, before whatever is sent next.
func (cs *chatState) typingStop() {
	h := cs.hub
	if !h.typing {
		return
	}
	cs.mu.Lock()
	if cs.typing != nil {
		cs.typing()
		cs.typing = nil
	}
	cs.mu.Unlock()
	cs.out.push(func() { _ = h.cl.Typing(h.srv.base, cs.to, true) })
}

// ---- the session's side ----

// chatLink is a live chat session's chatSink.
type chatLink struct {
	cs *chatState
	l  *live
}

func (k *chatLink) turnStart() { k.cs.typingStart() }

func (k *chatLink) turnEnd(status, content, errMsg string) {
	k.cs.typingStop()
	switch status {
	case "done":
		text, styles := signal.Format(content)
		k.cs.sayStyled(text, styles)
	case "error":
		k.cs.say("That did not work: " + errMsg)
	}
}

func (k *chatLink) approval(id string, req approval.Request, choices []string) {
	k.cs.askApproval(k.l, id, req, choices, "")
}

func (k *chatLink) question(id string, q tools.Question) { k.cs.askQuestion(k.l, id, q, "") }

func (k *chatLink) closed(id string) { k.cs.dropPending(id) }

// askApproval puts an approval of session l to the chat; prefix leads the
// message (a worker's approvals say whose they are). An id already asked is
// not asked again.
func (cs *chatState) askApproval(l *live, id string, req approval.Request, choices []string, prefix string) {
	cs.mu.Lock()
	if hasPending(cs.pending, id) {
		cs.mu.Unlock()
		return
	}
	cs.pending = append(cs.pending, pendingReq{id: id, l: l, choices: choices})
	many := len(approvals(cs.pending)) > 1
	cs.mu.Unlock()
	text, styles := approvalText(req, choices, id, many, prefix)
	cs.sayStyled(text, styles)
}

// askQuestion puts a question of session l to the chat.
func (cs *chatState) askQuestion(l *live, id string, q tools.Question, prefix string) {
	cs.mu.Lock()
	if hasPending(cs.pending, id) {
		cs.mu.Unlock()
		return
	}
	cs.pending = append(cs.pending, pendingReq{id: id, l: l, question: true, choices: q.Choices})
	cs.mu.Unlock()
	cs.say(questionText(q, prefix))
}

// dropPending forgets a request that was answered or withdrawn.
func (cs *chatState) dropPending(id string) {
	cs.mu.Lock()
	defer cs.mu.Unlock()
	for i, p := range cs.pending {
		if p.id == id {
			cs.pending = append(cs.pending[:i:i], cs.pending[i+1:]...)
			return
		}
	}
}

func hasPending(list []pendingReq, id string) bool {
	for _, p := range list {
		if p.id == id {
			return true
		}
	}
	return false
}

func approvals(list []pendingReq) []pendingReq {
	var out []pendingReq
	for _, p := range list {
		if !p.question {
			out = append(out, p)
		}
	}
	return out
}

func shortID(id string) string { return strings.TrimPrefix(id, "ap_") }

func utf16Len(s string) int { return len(utf16.Encode([]rune(s))) }

// approvalText is the chat's approval request, with the subject in a
// monospace style (a code block). Several waiting approvals each name an id.
func approvalText(req approval.Request, choices []string, id string, many bool, prefix string) (string, []string) {
	what := map[string]string{"command": "run a command", "write": "write a file", "memory": "save to memory"}[req.Kind]
	if what == "" {
		what = req.Kind
	}
	head := prefix + "⚠️ Permission requested: " + what
	if many {
		head += " [" + shortID(id) + "]"
	}
	text, styles := head, []string(nil)
	if req.Subject != "" {
		text += "\n\n"
		styles = []string{fmt.Sprintf("%d:%d:MONOSPACE", utf16Len(text), utf16Len(req.Subject))}
		text += req.Subject
	}
	if len(req.Reasons) > 0 {
		text += "\n"
		for _, r := range req.Reasons {
			text += "\n• " + r
		}
	}
	always := false
	for _, c := range choices {
		always = always || c == "always"
	}
	if always {
		text += "\n\nAnswer with /approve, /approve always or /deny."
	} else {
		text += "\n\nAnswer with /approve or /deny."
	}
	return text, styles
}

func questionText(q tools.Question, prefix string) string {
	text := "❓ " + prefix + q.Text
	if len(q.Choices) == 0 {
		return text
	}
	text += "\n"
	for i, c := range q.Choices {
		text += "\n  " + strconv.Itoa(i+1) + ". " + c
	}
	return text + "\n\nAnswer with a number, the text of an option, or something else of your own."
}

// ---- chat sessions (the server's side) ----

// notesDir is fiona's notes folder: the chat's directory and the only place
// its file tools reach. Recall indexes it in the gateway scope.
func (s *Server) notesDir() string { return filepath.Join(s.opt.Home, "notes", "gateway") }

// chatRunner makes r a chat session's runner: gateway scope, the chat
// toolset, file tools confined to the notes folder, fiona's identity, recall
// and session_search as the chat's own, and the default model as the fallback
// when the chat's own route is down.
func (s *Server) chatRunner(r *agent.Runner, stamp string, l *live) {
	r.Scope, r.Chat, r.MemoryScope = "gateway", stamp, "gateway"
	r.Policy.Confine = s.notesDir()
	r.Policy.ReadAlso = []string{filepath.Join(s.opt.Home, "cache", "signal")}
	r.Cron = s.cronHost(stamp)
	if chat := s.cfg.ChatModel(); chat != "" && chat != s.cfg.Model.Default && r.Model == chat {
		if ep, err := s.cfg.Endpoint(""); err == nil {
			r.Fallback = &agent.Fallback{Client: r.Client, Model: ep.Model, Effort: ep.ReasoningEffort}
		}
	}
	r.ModelNote = func() string { return s.lastServed(l.id) }
	r.ListNote = func() string { return s.modelsNote(stamp) }
	r.Default = s.defaultRoute(s.cfg.ChatModel())
	deadPin := r.OnDeadPin
	r.OnDeadPin = func(old string, def agent.Fallback) {
		deadPin(old, def)
		if s.chats != nil {
			s.chats.state(stamp).say(deadPinText(old, def.Model))
		}
	}
	r.Tools = s.regChat
	r.AsyncDelivery = false
	r.Relay = func(ctx context.Context, args json.RawMessage) string { return s.relay(ctx, stamp, args) }
	if !s.cfg.Gateway.SingleUser {
		r.WallChat = stamp
	}
	r.Prompt = func(_ context.Context, ss store.Session) (string, error) {
		var hours *[2]int
		start, end, ok, warn := s.cfg.WorkerHours()
		if ok {
			hours = &[2]int{start, end}
		}
		if warn != "" {
			log.Printf("signal: %s", warn)
		}
		identity, err := prompt.FionaIdentity(s.opt.Home, strings.TrimSpace(s.cfg.Daemon.Instance), hours)
		if err != nil {
			return "", err
		}
		res, err := prompt.Build(prompt.Options{
			Home: s.opt.Home, Cwd: ss.Cwd, Scope: "gateway", Memory: true, Identity: identity,
			Now: time.Now().In(prompt.LocalZone()), RulesCharLimit: s.cfg.Memory.RulesCharLimit,
		})
		for _, w := range res.Warnings {
			log.Printf("signal: prompt: %s", w)
		}
		return res.Text, err
	}
	if s.idx != nil {
		r.Recall = func(ctx context.Context, text string) string { return s.idx.Recall(ctx, "gateway", text) }
		r.SearchMessages = s.searchMessages("gateway")
	}
}

// createChat makes a chat's session.
func (s *Server) createChat(ctx context.Context, stamp string) (string, error) {
	ep, err := s.cfg.Endpoint(s.cfg.ChatModel())
	if err != nil {
		return "", err
	}
	mode := s.cfg.PermissionMode()
	r := s.runner(&live{}, ep, mode, "signal", nil)
	s.chatRunner(r, stamp, &live{})
	cwd := s.notesDir()
	if err := os.MkdirAll(cwd, 0o700); err != nil {
		return "", err
	}
	sess, err := r.NewSession(ctx, "chat", cwd)
	if err != nil {
		return "", err
	}
	defer sess.Close()
	// No model of its own: the chat follows the instance's chat model.
	if err := s.st.UpdateSessionSettings(ctx, sess.ID(), "", "", string(mode)); err != nil {
		return "", err
	}
	return sess.ID(), nil
}

// chatLive returns the chat's current session, loaded, making one if the chat
// has none. The caller holds cs.sessMu.
func (s *Server) chatLive(ctx context.Context, cs *chatState) (*live, error) {
	id, err := s.st.ChatSession(ctx, cs.stamp)
	if errors.Is(err, store.ErrNotFound) {
		if id, err = s.createChat(ctx, cs.stamp); err == nil {
			err = s.st.SetChatSession(ctx, cs.stamp, id)
		}
	}
	if err != nil {
		return nil, err
	}
	return s.load(ctx, id)
}

// socketSession is a session as the socket may see it: a chat's (gateway
// scope or stamped) reads as not found, whatever the method.
func (s *Server) socketSession(id string) (store.Session, *rpcError) {
	rec, err := s.st.Session(s.base, id)
	if err != nil {
		return rec, notFound(err, id)
	}
	if store.Readable("common", rec) {
		return rec, nil
	}
	return rec, notFound(store.ErrNotFound, id)
}

// ---- inbound ----

// handle is one event from signal-cli.
func (h *chatHub) handle(data []byte) {
	in, ok := h.in.Accept(data)
	if !ok {
		return
	}
	cs := h.state(in.Chat)
	if name, args, ok := parseCommand(in.Text); ok {
		cs.cmds.push(func() { h.command(cs, name, args) })
		return
	}
	cs.work.push(func() { h.message(cs, in) })
}

// parseCommand reads a control command: a first word that is "/" and
// letters. "/etc/hosts looks wrong" is a message.
func parseCommand(text string) (name, args string, ok bool) {
	t := strings.TrimSpace(text)
	end := strings.IndexFunc(t, unicode.IsSpace)
	if end < 0 {
		end = len(t)
	}
	word := t[:end]
	if !commandWord.MatchString(word) {
		return "", "", false
	}
	return strings.ToLower(word[1:]), strings.TrimSpace(t[end:]), true
}

var commandWord = regexp.MustCompile(`^/[A-Za-z]+$`)

// message handles an ordinary message: it answers a waiting approval or
// question, or becomes a turn (or a queued turn).
func (h *chatHub) message(cs *chatState, in signal.Inbound) {
	if len(in.Attachments) == 0 && h.answerWaiting(cs, in.Text) {
		return
	}
	text := h.userText(in)
	if strings.TrimSpace(text) == "" {
		return
	}
	h.deliver(cs, text, false)
}

// deliver gives text to the chat's session: a turn if it is idle, else queued
// (or, with now, steered in). The first message of a chat makes its session.
func (h *chatHub) deliver(cs *chatState, text string, now bool) (ok bool) {
	s := h.srv
	cs.sessMu.Lock()
	defer cs.sessMu.Unlock()
	mode := "queue"
	if now {
		mode = "now"
	}
	for range 3 {
		l, err := s.chatLive(s.base, cs)
		if err == nil {
			var queued string
			_, queued, _, err = l.send(text, mode)
			if errors.Is(err, errUnloaded) {
				continue // a finishing turn closed it first
			}
			if err == nil {
				if queued != "" {
					h.ack(cs)
				}
				return true
			}
		}
		if s.base.Err() == nil {
			cs.say("That did not work: " + err.Error())
		}
		return false
	}
	cs.say("That did not work: the session kept unloading")
	return false
}

// ack tells the chat its message is queued, at most once per ackEvery.
func (h *chatHub) ack(cs *chatState) {
	cs.mu.Lock()
	due := time.Since(cs.ackAt) >= ackEvery
	if due {
		cs.ackAt = time.Now()
	}
	cs.mu.Unlock()
	if due {
		cs.say(queuedAck)
	}
}

// userText is the text the model sees: the quote the message replies to, the
// message, and a line for each attachment saved to disk.
func (h *chatHub) userText(in signal.Inbound) string {
	text := strings.TrimSpace(in.Text)
	if q := in.Quote; q != nil && strings.TrimSpace(q.Text) != "" {
		quoted := q.Text
		if r := []rune(quoted); len(r) > quoteMax {
			quoted = string(r[:quoteMax])
		}
		lead := "[In reply to the message: \"" + quoted + "\"]"
		if q.Own {
			lead = "[In reply to your earlier message: \"" + quoted + "\"]"
		}
		text = strings.TrimSpace(lead + "\n\n" + text)
	}
	lines := []string{}
	if text != "" {
		lines = append(lines, text)
	}
	for _, a := range in.Attachments {
		path, name, err := h.saveAttachment(in.Timestamp, a)
		if err != nil {
			log.Printf("signal: attachment %s: %v", a.ID, err)
			continue
		}
		if a.Filename != "" {
			name = a.Filename
		}
		kind := a.ContentType
		if kind == "" {
			kind = "unknown type"
		}
		lines = append(lines, fmt.Sprintf("[Attachment from the user: %s (%s), stored at %s]", name, kind, path))
	}
	return strings.Join(lines, "\n")
}

var extByType = map[string]string{
	"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
	"application/pdf": ".pdf", "audio/ogg": ".ogg", "audio/aac": ".aac", "audio/mpeg": ".mp3",
	"video/mp4": ".mp4", "text/plain": ".txt",
}

// saveAttachment fetches an attachment into <home>/cache/signal and returns
// its path and its file name there (without the timestamp).
func (h *chatHub) saveAttachment(ts int64, a signal.Attachment) (path, name string, err error) {
	data, err := h.cl.Attachment(h.srv.base, a.ID)
	if err != nil {
		return "", "", err
	}
	name = safeName(filepath.Base(a.Filename))
	if name == "" {
		ext := extByType[a.ContentType]
		if exts, _ := mime.ExtensionsByType(a.ContentType); ext == "" && len(exts) > 0 {
			ext = exts[0]
		}
		name = safeName(a.ID) + ext
	}
	dir := filepath.Join(h.srv.opt.Home, "cache", "signal")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", "", err
	}
	for n := 1; ; n++ {
		file := strconv.FormatInt(ts, 10) + "_" + name
		if n > 1 {
			file = strconv.FormatInt(ts, 10) + "_" + strconv.Itoa(n) + "_" + name
		}
		path = filepath.Join(dir, file)
		f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
		if errors.Is(err, os.ErrExist) {
			continue
		}
		if err != nil {
			return "", "", err
		}
		_, werr := f.Write(data)
		if cerr := f.Close(); werr == nil {
			werr = cerr
		}
		return path, name, werr
	}
}

// safeName keeps a file name to letters, digits and . _ - so it cannot leave
// the cache directory or confuse a shell.
func safeName(s string) string {
	s = strings.Map(func(r rune) rune {
		if unicode.IsLetter(r) || unicode.IsDigit(r) || r == '.' || r == '_' || r == '-' {
			return r
		}
		return '_'
	}, s)
	return strings.Trim(s, ".")
}

// ---- approvals and questions answered in the chat ----

var (
	yesWords = map[string]bool{"yes": true, "y": true, "ok": true, "okay": true, "approve": true, "confirm": true, "👍": true}
	noWords  = map[string]bool{"no": true, "n": true, "deny": true, "reject": true, "cancel": true, "👎": true}
)

// plainAnswer lowers a message for matching: no case, no trailing ! or ., no
// emoji skin tone or variation selector.
func plainAnswer(text string) string {
	t := strings.Map(func(r rune) rune {
		if r == 0xFE0F || (r >= 0x1F3FB && r <= 0x1F3FF) {
			return -1
		}
		return r
	}, strings.ToLower(strings.TrimSpace(text)))
	return strings.TrimRight(t, "!. ")
}

// answerWaiting treats text as the answer to what waits on the chat: a
// message of only yes/no words answers the oldest approval, and any message
// answers the oldest question. It reports whether text was used up.
func (h *chatHub) answerWaiting(cs *chatState, text string) bool {
	cs.mu.Lock()
	hasApproval := len(approvals(cs.pending)) > 0
	var q *pendingReq
	for i := range cs.pending {
		if cs.pending[i].question {
			q = &cs.pending[i]
			break
		}
	}
	var question pendingReq
	if q != nil {
		question = *q
	}
	cs.mu.Unlock()

	if hasApproval {
		switch t := plainAnswer(text); {
		case yesWords[t]:
			cs.say(h.resolveApproval(cs, "", true, false, ""))
			return true
		case t == "always":
			cs.say(h.resolveApproval(cs, "", true, true, ""))
			return true
		case noWords[t]:
			cs.say(h.resolveApproval(cs, "", false, false, ""))
			return true
		}
	}
	if q == nil {
		return false
	}
	answer := strings.TrimSpace(text)
	if n, err := strconv.Atoi(answer); err == nil && n >= 1 && n <= len(question.choices) {
		answer = question.choices[n-1]
	} else {
		for _, c := range question.choices {
			if strings.EqualFold(c, answer) {
				answer = c
			}
		}
	}
	if e := question.l.answerQuestion(question.id, []string{answer}); e != nil {
		return false // already resolved: the message is an ordinary one
	}
	return true
}

// resolveApproval answers the waiting approval named by the id prefix idTok
// (the oldest when ""), and returns what to tell the chat.
func (h *chatHub) resolveApproval(cs *chatState, idTok string, approve, always bool, reason string) string {
	cs.mu.Lock()
	list := approvals(cs.pending)
	cs.mu.Unlock()
	if len(list) == 0 {
		return noApproval
	}
	target := list[0]
	if idTok != "" {
		var found []pendingReq
		if len(idTok) >= 4 {
			for _, p := range list {
				if strings.HasPrefix(shortID(p.id), strings.ToLower(idTok)) {
					found = append(found, p)
				}
			}
		}
		if len(found) != 1 {
			return "Exactly one pending approval should match " + idTok + ", and none or several do."
		}
		target = found[0]
	}
	choice, reply := "deny", "Denied."
	if approve {
		choice, reply = "once", "Approved."
		for _, c := range target.choices {
			if always && c == "always" {
				choice, reply = "always", "Approved. This choice will be remembered."
			}
		}
	}
	if e := target.l.answerApprovalWith(target.id, choice, reason); e != nil {
		return noApproval
	}
	return reply
}

// ---- control commands ----

// current is the chat's current session, if it is loaded (an unloaded one is
// idle), and its id ("" for none).
func (h *chatHub) current(cs *chatState) (*live, string) {
	id, err := h.srv.st.ChatSession(h.srv.base, cs.stamp)
	if err != nil {
		return nil, ""
	}
	return h.srv.loaded(id), id
}

func (h *chatHub) command(cs *chatState, name, args string) {
	switch name {
	case "stop":
		if l, _ := h.current(cs); l != nil && l.stopChat() {
			cs.say("Stopped. The session is still here if you want to carry on.")
		} else {
			cs.say(nothingToStop)
		}
	case "new":
		cs.sessMu.Lock()
		l, _ := h.current(cs)
		err := h.srv.st.SetChatSession(h.srv.base, cs.stamp, "")
		cs.sessMu.Unlock()
		if err != nil {
			cs.say("That did not work: " + err.Error())
			return
		}
		if l != nil {
			l.stopChat()
		}
		cs.say("Fresh session started.")
	case "sendnow":
		h.sendnow(cs, args)
	case "sendall":
		h.sendall(cs)
	case "model":
		cs.say(h.modelCommand(cs, args))
	case "status":
		cs.say(h.status(cs))
	case "approve":
		always, idTok := false, ""
		for _, t := range strings.Fields(args) {
			if strings.EqualFold(t, "always") {
				always = true
			} else if idTok == "" {
				idTok = t
			}
		}
		cs.say(h.resolveApproval(cs, idTok, true, always, ""))
	case "deny":
		idTok, reason := "", args
		if first, rest, _ := strings.Cut(args, " "); len(first) >= 4 && h.namesApproval(cs, first) {
			idTok, reason = first, strings.TrimSpace(rest)
		}
		cs.say(h.resolveApproval(cs, idTok, false, false, reason))
	default:
		cs.say("/" + name + " is not a command here. Available: " + commandsHelp + ".")
	}
}

// namesApproval says whether tok is the id prefix of a waiting approval.
func (h *chatHub) namesApproval(cs *chatState, tok string) bool {
	cs.mu.Lock()
	defer cs.mu.Unlock()
	for _, p := range approvals(cs.pending) {
		if strings.HasPrefix(shortID(p.id), strings.ToLower(tok)) {
			return true
		}
	}
	return false
}

func (h *chatHub) sendnow(cs *chatState, text string) {
	l, _ := h.current(cs)
	running := l != nil && l.chatRunning()
	switch {
	case text != "" && !running:
		h.deliver(cs, text, false)
	case text != "":
		if h.deliver(cs, text, true) {
			cs.say("Your queued message was added to the turn in progress.")
		}
	case !running:
		cs.say("No turn is in progress.")
	default:
		started, steered, err := l.steerQueue(false)
		switch {
		case err != nil:
			cs.say("That did not work: " + err.Error())
		case !started && !steered:
			cs.say("No messages are queued.")
		default:
			reply := "Added the oldest queued message to the turn in progress."
			if items, err := h.srv.st.QueueItems(h.srv.base, l.id); err == nil && len(items) > 0 {
				reply += " (" + strconv.Itoa(len(items)) + " still queued)"
			}
			cs.say(reply)
		}
	}
}

func (h *chatHub) sendall(cs *chatState) {
	l, _ := h.current(cs)
	if l == nil || !l.chatRunning() {
		cs.say("No turn is in progress.")
		return
	}
	items, _ := h.srv.st.QueueItems(h.srv.base, l.id)
	started, steered, err := l.steerQueue(true)
	switch {
	case err != nil:
		cs.say("That did not work: " + err.Error())
	case !started && !steered:
		cs.say("No messages are queued.")
	default:
		cs.say("Added " + strconv.Itoa(len(items)) + " queued messages to the turn in progress.")
	}
}

// status is /status: the session, model, context use, state, queue and
// approvals.
func (h *chatHub) status(cs *chatState) string {
	s := h.srv
	cs.mu.Lock()
	waiting := len(approvals(cs.pending))
	cs.mu.Unlock()
	id, err := s.st.ChatSession(s.base, cs.stamp)
	model := s.cfg.ChatModel()
	if model == "" {
		model = s.cfg.Model.Default
	}
	lines := []string{}
	state, queued, tokens, window, served := "idle", 0, 0, 0, ""
	if err != nil {
		lines = append(lines, "Session: none yet (your next message opens one)")
	} else {
		rec, err := s.st.Session(s.base, id)
		if err != nil {
			return "That did not work: " + err.Error()
		}
		model, tokens = rec.Model, rec.ContextTokens
		title := rec.Title
		if title == "" {
			title = "untitled"
		}
		lines = append(lines, "Session: "+id+" ("+title+")")
		if l := s.loaded(id); l != nil {
			l.mu.Lock()
			if l.running {
				state = "running"
			}
			model, tokens, window, served = l.model, l.ctxTokens, l.sess.ContextLength(), l.sess.ServedModel()
			l.mu.Unlock()
		} else if ep, err := s.cfg.Endpoint(rec.Model); err == nil {
			window = s.contextLength(ep, id)
		}
		if items, err := s.st.QueueItems(s.base, id); err == nil {
			queued = len(items)
		}
	}
	m := "Model: " + model
	if served != "" && served != model {
		m += " (served: " + served + ")"
	}
	lines = append(lines, m)
	if err == nil {
		ctx := strconv.Itoa(tokens) + " tokens"
		if window > 0 {
			ctx = fmt.Sprintf("%d of %d tokens (%d%%)", tokens, window, tokens*100/window)
		}
		lines = append(lines, "Context: "+ctx)
	}
	lines = append(lines, "State: "+state, "Queued: "+strconv.Itoa(queued), "Approvals waiting: "+strconv.Itoa(waiting))
	return strings.Join(lines, "\n")
}

// ---- live methods for chats ----

// chatRunning reports whether a turn is running.
func (l *live) chatRunning() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.running
}

// stopChat is /stop: it drops the queue and interrupts the turn, and reports
// whether a turn was running that was not already being stopped.
func (l *live) stopChat() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	active := l.running && l.stop == stopNone
	if err := l.srv.st.QueueClear(context.Background(), l.id); err == nil {
		l.emitQueue()
	}
	l.interruptLocked()
	return active
}
