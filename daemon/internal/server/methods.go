package server

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/paths"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// alreadyReplied is a handler result meaning it sent its own response.
type alreadyReplied struct{}

const (
	historyDefault = 200
	historyMax     = 1000
)

// sessionParams is the shape most methods take.
type sessionParams struct {
	Session string `json:"session"`
}

func (s *Server) dispatch(c *conn, req *request) (any, *rpcError) {
	bad := func(err error) (any, *rpcError) { return nil, errf(codeBadParams, "invalid params: %v", err) }
	var sp sessionParams
	// Methods on one session: parse the id once, find the session, and for
	// driver-only methods check the role.
	driverOnly := true
	switch req.Method {
	case "hello", "session.list", "session.create", "session.attach", "session.history", "session.detach", "session.children", "model.list":
		driverOnly = false
	case "turn.send", "turn.interrupt", "queue.steer", "queue.remove", "queue.clear",
		"approval.answer", "question.answer", "session.set", "command.run":
	default:
		return nil, errf(codeNoMethod, "unknown method %q", req.Method)
	}
	if driverOnly {
		if err := json.Unmarshal(req.Params, &sp); err != nil {
			return bad(err)
		}
		l, e := s.attachedLive(c, sp.Session)
		if e != nil {
			return nil, e
		}
		return s.driverMethod(c, l, req, bad)
	}
	switch req.Method {
	case "hello":
		var p struct {
			Protocol int    `json:"protocol"`
			Client   string `json:"client"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil {
			return bad(err)
		}
		if p.Protocol != 1 {
			return nil, errf(codeProtocol, "unsupported protocol version %d (this daemon speaks 1)", p.Protocol)
		}
		c.hello, c.client = true, p.Client
		return map[string]any{"protocol": 1, "version": s.opt.Version, "pid": os.Getpid(), "home": s.opt.Home}, nil
	case "session.list":
		return s.list(req.Params, bad)
	case "session.create":
		var p struct {
			Cwd string `json:"cwd"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil {
			return bad(err)
		}
		if fi, err := os.Stat(p.Cwd); !filepath.IsAbs(p.Cwd) || err != nil || !fi.IsDir() {
			return nil, errf(codeBadParams, "cwd %q must be an absolute path to an existing directory", p.Cwd)
		}
		id, warnings, err := s.create(s.base, p.Cwd, c.client)
		if err != nil {
			return nil, errf(codeInternal, "creating the session: %v", err)
		}
		sum, err := s.st.SessionSummary(s.base, id)
		if err != nil {
			return nil, errf(codeInternal, "%v", err)
		}
		if warnings == nil {
			warnings = []string{}
		}
		return map[string]any{"session": summaryObj(sum), "warnings": warnings}, nil
	case "session.attach":
		return s.attach(c, req, bad)
	case "session.history":
		var p struct {
			Session   string `json:"session"`
			BeforeSeq int64  `json:"before_seq"`
			Limit     int    `json:"limit"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil {
			return bad(err)
		}
		if _, e := s.socketSession(p.Session); e != nil {
			return nil, e
		}
		rows, more, err := s.st.MessagesBefore(s.base, p.Session, p.BeforeSeq, clampLimit(p.Limit))
		if err != nil {
			return nil, errf(codeInternal, "%v", err)
		}
		return map[string]any{"messages": messageObjs(rows), "more_before": more}, nil
	case "session.children":
		if err := json.Unmarshal(req.Params, &sp); err != nil {
			return bad(err)
		}
		return s.children(sp.Session)
	case "session.detach":
		if err := json.Unmarshal(req.Params, &sp); err != nil {
			return bad(err)
		}
		if l := c.attached[sp.Session]; l != nil {
			delete(c.attached, sp.Session)
			l.detach(c)
			s.maybeUnload(l)
		}
		return map[string]any{}, nil
	default: // model.list
		return s.models(req.Params, bad)
	}
}

func notFound(err error, id string) *rpcError {
	if errors.Is(err, store.ErrNotFound) {
		return errf(codeNotFound, "no such session %q", id)
	}
	return errf(codeInternal, "%v", err)
}

func clampLimit(n int) int {
	if n <= 0 {
		return historyDefault
	}
	return min(n, historyMax)
}

// attachedLive finds a loaded session this connection is attached to.
func (s *Server) attachedLive(c *conn, id string) (*live, *rpcError) {
	if l := s.loaded(id); l != nil && c.attached[id] == l {
		return l, nil
	}
	if _, err := s.st.Session(s.base, id); err != nil {
		return nil, notFound(err, id)
	}
	return nil, errf(codeNotDriver, "not attached to session %q", id)
}

func (s *Server) driverMethod(c *conn, l *live, req *request, bad func(error) (any, *rpcError)) (any, *rpcError) {
	// Anyone may attach as a viewer, but these all need the driver.
	if !l.isDriver(c) {
		return nil, errf(codeNotDriver, "this connection is not the driver of session %q", l.id)
	}
	switch req.Method {
	case "turn.send":
		var p struct {
			Text string `json:"text"`
			Mode string `json:"mode"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil {
			return bad(err)
		}
		if p.Mode != "" && p.Mode != "queue" && p.Mode != "now" {
			return nil, errf(codeBadParams, "mode must be queue or now")
		}
		if strings.TrimSpace(p.Text) == "" {
			return nil, errf(codeBadParams, "text is empty")
		}
		return sendResult(l.send(p.Text, p.Mode))
	case "turn.interrupt":
		return map[string]any{"interrupted": l.interrupt()}, nil
	case "queue.steer":
		var p struct {
			All bool `json:"all"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil {
			return bad(err)
		}
		started, steered, err := l.steerQueue(p.All)
		if err != nil {
			return nil, errf(codeInternal, "%v", err)
		}
		return map[string]any{"started": started, "steered": steered}, nil
	case "queue.remove", "queue.clear":
		var p struct {
			Items []string `json:"items"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil {
			return bad(err)
		}
		items, err := l.editQueue(req.Method == "queue.clear", p.Items)
		if err != nil {
			return nil, errf(codeInternal, "%v", err)
		}
		return map[string]any{"queue": items}, nil
	case "approval.answer":
		var p struct {
			ID     string `json:"approval_id"`
			Choice string `json:"choice"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil {
			return bad(err)
		}
		if e := l.answerApproval(p.ID, p.Choice); e != nil {
			return nil, e
		}
		return map[string]any{}, nil
	case "question.answer":
		var p struct {
			ID      string   `json:"question_id"`
			Answers []string `json:"answers"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil {
			return bad(err)
		}
		if e := l.answerQuestion(p.ID, p.Answers); e != nil {
			return nil, e
		}
		return map[string]any{}, nil
	case "session.set":
		return s.set(l, req.Params, bad)
	default: // command.run
		var p struct {
			Name string `json:"name"`
			Args string `json:"args"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil {
			return bad(err)
		}
		switch p.Name {
		case "commit":
			return sendResult(l.send(s.commitPrompt(p.Args), "queue"))
		case "stop":
			return map[string]any{"killed": l.killBackground()}, nil
		case "compact":
			return l.compactCommand(p.Args)
		}
		return nil, errf(codeBadParams, "unknown command %q", p.Name)
	}
}

func sendResult(started bool, queued string, steered bool, err error) (any, *rpcError) {
	if err != nil {
		return nil, errf(codeInternal, "%v", err)
	}
	r := map[string]any{"started": started, "steered": steered}
	if queued != "" {
		r["queued"] = queued
	}
	return r, nil
}

func (l *live) editQueue(clear bool, ids []string) ([]map[string]any, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	var err error
	if clear {
		err = l.srv.st.QueueClear(context.Background(), l.id)
	} else {
		err = l.srv.st.QueueRemove(context.Background(), l.id, ids...)
	}
	if err != nil {
		return nil, err
	}
	l.emitQueue()
	items, err := l.srv.st.QueueItems(context.Background(), l.id)
	return queueObjs(items), err
}

func (l *live) killBackground() int {
	n := l.sess.KillBackground()
	l.mu.Lock()
	defer l.mu.Unlock()
	l.emitSession()
	return n
}

func (s *Server) attach(c *conn, req *request, bad func(error) (any, *rpcError)) (any, *rpcError) {
	var p struct {
		Session  string `json:"session"`
		As       string `json:"as"`
		AfterSeq *int64 `json:"after_seq"`
		Limit    int    `json:"limit"`
	}
	if err := json.Unmarshal(req.Params, &p); err != nil {
		return bad(err)
	}
	if p.As != "driver" && p.As != "viewer" {
		return nil, errf(codeBadParams, "as must be driver or viewer")
	}
	if _, e := s.socketSession(p.Session); e != nil {
		return nil, e
	}
	for range 3 {
		l, err := s.load(s.base, p.Session)
		if err != nil {
			return nil, notFound(err, p.Session)
		}
		err = l.attach(c, p.As == "driver", p.AfterSeq, clampLimit(p.Limit), func(r any) { c.reply(req.ID, r, nil) })
		if errors.Is(err, errUnloaded) {
			continue // another connection's detach closed it first
		}
		if err != nil {
			return nil, errf(codeInternal, "%v", err)
		}
		c.attached[p.Session] = l
		return alreadyReplied{}, nil
	}
	return nil, errf(codeInternal, "the session kept unloading")
}

func (s *Server) list(params json.RawMessage, bad func(error) (any, *rpcError)) (any, *rpcError) {
	p := struct {
		Kinds []string `json:"kinds"`
		Limit int      `json:"limit"`
		Cwd   string   `json:"cwd"` // only sessions of this directory's workspace
	}{}
	if err := json.Unmarshal(params, &p); err != nil {
		return bad(err)
	}
	if len(p.Kinds) == 0 {
		p.Kinds = []string{"interactive"}
	}
	if p.Limit <= 0 {
		p.Limit = 50
	}
	workspace := ""
	if p.Cwd != "" {
		if !filepath.IsAbs(p.Cwd) {
			return nil, errf(codeBadParams, "cwd must be absolute")
		}
		workspace = paths.Workspace(p.Cwd)
	}
	sums, err := s.st.ListReadableIn(s.base, "common", "", workspace, p.Kinds, p.Limit)
	if err != nil {
		return nil, errf(codeInternal, "%v", err)
	}
	out := make([]map[string]any, len(sums))
	for i, sum := range sums {
		if l := s.loaded(sum.Session.ID); l != nil {
			out[i] = l.snapshot()
		} else {
			out[i] = summaryObj(sum)
		}
	}
	return map[string]any{"sessions": out}, nil
}

// summaryObj is the session object for a session that is not loaded.
func summaryObj(sum store.Summary) map[string]any {
	ss := sum.Session
	return map[string]any{
		"id": ss.ID, "kind": ss.Kind, "title": ss.Title, "cwd": ss.Cwd,
		"model": ss.Model, "reasoning_effort": ss.ReasoningEffort, "mode": ss.Mode,
		"status": "idle", "created": ss.Created.Format(time.RFC3339), "updated": sum.Updated.Format(time.RFC3339),
		"preview": sum.Preview, "context_tokens": ss.ContextTokens, "background_processes": 0, "parent_id": ss.ParentID, "agent": ss.Agent,
		"usage": map[string]any{"input_tokens": ss.Usage.Input, "output_tokens": ss.Usage.Output, "cached_tokens": ss.Usage.Cached,
			"reasoning_tokens": ss.Usage.Reasoning, "api_calls": ss.Usage.Calls},
	}
}

func messageObjs(rows []store.Message) []messageJSON {
	out := make([]messageJSON, 0, len(rows))
	for _, m := range rows {
		out = append(out, messageObj(m))
	}
	return out
}

// models is model.list: the configured routes plus the provider's /models.
func (s *Server) models(params json.RawMessage, bad func(error) (any, *rpcError)) (any, *rpcError) {
	var p sessionParams
	if err := json.Unmarshal(params, &p); err != nil {
		return bad(err)
	}
	current := s.cfg.Model.Default
	if p.Session != "" {
		rec, e := s.socketSession(p.Session)
		if e != nil {
			return nil, e
		}
		if rec.Model != "" {
			current = rec.Model
		}
	}
	routes := s.cfg.Routes()
	if ep, err := s.cfg.Endpoint(""); err == nil {
		ctx, cancel := context.WithTimeout(s.base, 5*time.Second)
		infos, err := provider.New(ep.BaseURL, ep.APIKey).ModelInfo(ctx)
		cancel()
		if err == nil {
			s.refreshMeta(ep, infos)
			for _, info := range infos {
				id := info.ID
				if _, ok := routes[id]; !ok {
					routes[id] = nil
				}
			}
		}
	}
	names := make([]string, 0, len(routes))
	for n := range routes {
		names = append(names, n)
	}
	sort.Strings(names)
	models := make([]map[string]any, len(names))
	for i, n := range names {
		efforts := routes[n]
		if efforts == nil {
			efforts = []string{}
		}
		models[i] = map[string]any{"name": n, "reasoning_efforts": efforts}
	}
	return map[string]any{"models": models, "current": current}, nil
}

// set is session.set: model and effort apply from the next turn, the mode at
// once.
func (s *Server) set(l *live, params json.RawMessage, bad func(error) (any, *rpcError)) (any, *rpcError) {
	var p struct {
		Model  *string `json:"model"`
		Effort *string `json:"reasoning_effort"`
		Mode   *string `json:"mode"`
		Title  *string `json:"title"`
	}
	if err := json.Unmarshal(params, &p); err != nil {
		return bad(err)
	}
	l.mu.Lock()
	model, effort, mode := l.model, l.effort, l.mode
	l.mu.Unlock()
	var ep config.Endpoint
	changedModel := p.Model != nil && *p.Model != "" && *p.Model != model
	if p.Model != nil && *p.Model != "" {
		model = *p.Model
	}
	ep, err := s.cfg.Endpoint(model)
	if err != nil {
		return nil, errf(codeBadParams, "model %q: %v", model, err)
	}
	if p.Effort != nil || changedModel {
		effort = ep.ReasoningEffort
		if p.Effort != nil && *p.Effort != "" {
			effort = *p.Effort
			if known := s.cfg.Routes()[model]; len(known) > 0 && !contains(known, effort) {
				return nil, errf(codeBadParams, "reasoning_effort %q is not one of %s for %s", effort, strings.Join(known, ", "), model)
			}
		}
	}
	m := approval.Mode(mode)
	if p.Mode != nil {
		if m, err = approval.ParseMode(*p.Mode); err != nil {
			return nil, errf(codeBadParams, "%v", err)
		}
	}
	if err := s.st.UpdateSessionSettings(s.base, l.id, model, effort, string(m)); err != nil {
		return nil, errf(codeInternal, "%v", err)
	}
	if p.Title != nil && strings.TrimSpace(*p.Title) != "" {
		if err := l.setTitle(strings.TrimSpace(*p.Title)); err != nil {
			return nil, errf(codeInternal, "%v", err)
		}
	}
	s.fetchMeta(s.base, ep, false)
	l.mu.Lock()
	defer l.mu.Unlock()
	l.model, l.effort, l.mode = model, effort, string(m)
	l.sess.SetModel(provider.New(ep.BaseURL, ep.APIKey), model, effort)
	l.sess.SetMode(m)
	l.sess.SetContextLength(s.contextLength(ep, l.id))
	l.sess.SetSummarizer(s.summarizer(ep))
	l.ctxKnown = l.sess.ContextLength()
	l.emitSession()
	return map[string]any{"session": l.sessionObj()}, nil
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// commitPrompt is /commit's turn text (../son_of_anton_cli/cli_commands_mixin.py:1454).
func (s *Server) commitPrompt(extra string) string {
	lines := []string{"You are a summarization agent. Your job is to review all uncommitted git " +
		"changes in the current repository, write an accurate commit message " +
		"matching the existing style, and then stage+commit them."}
	name, email := strings.TrimSpace(s.cfg.Git.AuthorName), strings.TrimSpace(s.cfg.Git.AuthorEmail)
	if name != "" && email != "" {
		lines = append(lines, "Author the commit as "+name+" <"+email+">: commit with "+
			"`git commit --author=\""+name+" <"+email+">\" -m \"<message>\"`. "+
			"Leave the committer untouched — it must stay whatever identity the "+
			"repository or your global git config already sets (do not override "+
			"the committer; no GIT_COMMITTER_NAME / GIT_COMMITTER_EMAIL, no "+
			"-c user.name / user.email).")
		lines = append(lines, "The commit is authored by "+name+", not by you, so end the message body "+
			"with one line `Model: <id>`, where <id> is the value of $SOA_MODEL in your "+
			"shell: the model writing this commit. Add no Co-authored-by trailer.")
	} else {
		lines = append(lines, "Commit with the repository's already-configured git identity; do not "+
			"override author or committer.")
	}
	if extra = strings.TrimSpace(extra); extra != "" {
		lines = append(lines, "Additional instructions from the user: "+extra)
	}
	return strings.Join(lines, "\n\n")
}
