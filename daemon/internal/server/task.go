package server

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/paths"
	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/subagent"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// children is session.children: a session's child sessions in creation order.
func (s *Server) children(id string) (any, *rpcError) {
	if _, e := s.socketSession(id); e != nil {
		return nil, e
	}
	kids, err := s.st.Children(s.base, id)
	if err != nil {
		return nil, errf(codeInternal, "%v", err)
	}
	out := make([]map[string]any, len(kids))
	for i, k := range kids {
		if l := s.loaded(k.ID); l != nil {
			out[i] = l.snapshot()
		} else {
			sum, err := s.st.SessionSummary(s.base, k.ID)
			if err != nil {
				return nil, errf(codeInternal, "%v", err)
			}
			out[i] = summaryObj(sum)
		}
		rs, err := s.st.RunStats(s.base, k.ID)
		if err != nil {
			return nil, errf(codeInternal, "%v", err)
		}
		out[i]["tool_calls"], out[i]["duration_ms"] = rs.ToolCalls, rs.Duration.Milliseconds()
	}
	return map[string]any{"children": out}, nil
}

// childRunner turns a session runner into a subagent's: its type's tools, no
// questioner and no recall; an approval it needs goes to the parent's driver.
func (s *Server) childRunner(r *agent.Runner, l *live, rec store.Session) *agent.Runner {
	var names []string
	if typ, ok := subagent.Lookup(subagent.Builtin(), rec.Agent); ok && typ.Tools != nil {
		names = typ.Tools
	} else {
		names = s.reg.Names()
	}
	var allowed []string
	for _, n := range names {
		if subagent.ChildMay(n) {
			allowed = append(allowed, n)
		}
	}
	r.Tools = s.reg.Only(allowed...)
	r.Ask, r.Recall, r.SearchMessages = nil, nil, nil
	r.AsyncDelivery = false // nobody is waiting for a background process's notice
	r.Approver = func(ctx context.Context, req approval.Request) (approval.Choice, error) {
		if p := s.loaded(rec.ParentID); p != nil {
			return p.approveAs(ctx, req, rec.ID)
		}
		return l.approve(ctx, req)
	}
	return r
}

// childEnd is how a child's run ended.
type childEnd struct{ status, text, errMsg string }

// spawnTask is the task tool's Spawner: it makes (or continues) a child
// session of the calling session, runs it, and reports how it ended. No lock
// is held while the child runs.
func (s *Server) spawnTask(ctx context.Context, env *tools.Env, req subagent.Request) (subagent.Result, error) {
	parent := s.loaded(env.SessionID)
	if parent == nil {
		return subagent.Result{}, errors.New("the calling session is not loaded")
	}
	id := req.TaskID
	if id == "" {
		var err error
		if id, err = s.makeChild(ctx, env, req); err != nil {
			return subagent.Result{}, err
		}
	} else if rec, err := s.st.Session(ctx, id); errors.Is(err, store.ErrNotFound) || (err == nil && rec.ParentID != env.SessionID) {
		return subagent.Result{ID: id}, fmt.Errorf("no task %s in this session", id)
	} else if err != nil {
		return subagent.Result{ID: id}, err
	}

	ended := make(chan childEnd, 1)
	watch := func(status, text, errMsg string) {
		select {
		case ended <- childEnd{status, text, errMsg}:
		default:
		}
	}
	var child *live
	var queued bool
	for range 3 {
		var err error
		if child, err = s.load(ctx, id); err != nil {
			return subagent.Result{ID: id}, err
		}
		if queued, err = child.runTask(req.Prompt, watch); errors.Is(err, errUnloaded) {
			continue // another connection's detach closed it first
		} else if err != nil {
			return subagent.Result{ID: id}, err
		}
		break
	}
	if queued {
		return subagent.Result{ID: id, State: "running", Summary: "Background task updated", Text: subagent.BackgroundUpdated}, nil
	}
	if req.TaskID == "" {
		obj := child.snapshot()
		parent.mu.Lock()
		parent.emit("child.created", map[string]any{"call_id": req.CallID, "child": obj})
		parent.mu.Unlock()
	}

	if req.Background {
		bctx, cancel := context.WithCancel(s.base)
		parent.mu.Lock()
		parent.bg[id] = cancel
		parent.mu.Unlock()
		s.turns.Add(1)
		go func() {
			defer s.turns.Done()
			defer cancel()
			e := waitChild(bctx, child, ended)
			parent.mu.Lock()
			delete(parent.bg, id)
			parent.mu.Unlock()
			parent.childDone(id, e.status)
			if e.status != "interrupted" {
				state, summary, text := "completed", "Background task completed: "+req.Description, e.text
				if e.status == "error" {
					state, summary, text = "error", "Background task failed: "+req.Description, e.errMsg
				}
				parent.sess.Notify(subagent.Render(id, state, summary, text))
			}
			s.maybeUnload(parent)
		}()
		return subagent.Result{ID: id, State: "running", Summary: "Background task started", Text: subagent.BackgroundStarted}, nil
	}

	e := waitChild(ctx, child, ended)
	parent.childDone(id, e.status)
	switch e.status {
	case "done":
		return subagent.Result{ID: id, State: "completed", Text: e.text}, nil
	case "interrupted":
		return subagent.Result{ID: id, State: "error", Text: "Task cancelled"}, nil
	}
	return subagent.Result{ID: id, State: "error", Text: fmt.Sprintf("Subagent failed (task_id: %s): %s", id, e.errMsg)}, nil
}

// waitChild waits for the child's run to end; if ctx is cancelled first, the
// child is interrupted and its end awaited.
func waitChild(ctx context.Context, child *live, ended <-chan childEnd) childEnd {
	select {
	case e := <-ended:
		return e
	case <-ctx.Done():
		child.interrupt()
		return <-ended
	}
}

// childDone announces a child's end on its parent. A steered turn counts as
// interrupted.
func (l *live) childDone(child, status string) {
	rs, _ := l.srv.st.RunStats(context.Background(), child)
	if status == "steered" {
		status = "interrupted"
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.emit("child.done", map[string]any{"child_id": child, "status": status,
		"tool_calls": rs.ToolCalls, "duration_ms": rs.Duration.Milliseconds()})
}

// runTask starts a turn with text on an idle session, calling w when the
// session is next idle (every queued follow-up included). A running session
// takes the text as a queued message instead and reports queued.
func (l *live) runTask(text string, w func(status, text, errMsg string)) (queued bool, err error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.unloaded {
		return false, errUnloaded
	}
	if l.closing {
		return false, errors.New("the daemon is shutting down")
	}
	if l.running || l.compacting {
		if _, err := l.srv.st.QueuePush(context.Background(), l.id, "user", text); err != nil {
			return false, err
		}
		l.emitQueue()
		return true, nil
	}
	l.watchers = append(l.watchers, w)
	l.startTurn(text)
	return false, nil
}

// makeChild stores a new child session of the calling session and returns its
// id. The child takes the parent's directory, model, effort, scope and
// permission mode, and a prompt of its type's identity, guidance, project
// context and delegated rules.
func (s *Server) makeChild(ctx context.Context, env *tools.Env, req subagent.Request) (string, error) {
	rec, err := s.st.Session(ctx, env.SessionID)
	if err != nil {
		return "", err
	}
	cwd := env.State.Cwd()
	if cwd == "" {
		cwd = rec.Cwd
	}
	built, err := prompt.Build(prompt.Options{
		Home: s.opt.Home, Cwd: cwd, Scope: rec.Scope, Memory: false, Identity: req.Type.Prompt,
		Now: time.Now().In(prompt.LocalZone()), RulesCharLimit: s.cfg.Memory.RulesCharLimit,
	})
	if err != nil {
		return "", err
	}
	hash, err := s.st.PutPrompt(ctx, built.Text)
	if err != nil {
		return "", err
	}
	child, err := s.st.CreateSession(ctx, store.Session{
		Kind: "child", ParentID: rec.ID, Source: "task", Agent: req.Type.Name,
		Title: req.Description + " (@" + req.Type.Name + " subagent)", TitleSource: "user",
		Cwd: cwd, Workspace: paths.Workspace(cwd), Model: rec.Model, PromptHash: hash,
		ReasoningEffort: rec.ReasoningEffort, Mode: rec.Mode, Scope: rec.Scope, Chat: rec.Chat,
	})
	if err != nil {
		return "", err
	}
	return child.ID, nil
}
