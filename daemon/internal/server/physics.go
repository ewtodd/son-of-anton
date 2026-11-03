package server

// Physics runs (phase 11c): the daemon methods, the setup of a run, and the
// sessions' runners. The controller that drives a run is in physrun.go.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/BurntSushi/toml"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/paths"
	"github.com/ewtodd/son-of-anton/daemon/internal/physics"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/sandbox"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

const (
	physicsMaxStepsDefault = 400
	defaultMaxSessions     = 8
	defaultSessionTokens   = 400_000
	defaultTotalTokens     = 3_000_000
	defaultHours           = 8.0
)

var modelSlugRe = regexp.MustCompile(`[^A-Za-z0-9._-]+`)

// physicsDir is where the specs, git directories and run logs live: never
// inside a run directory, which the sandboxed worker can write.
func (s *Server) physicsDir() string { return filepath.Join(s.opt.Home, "physics") }

func (s *Server) physicsLimits(p runParams) physics.Limits {
	c := s.cfg.Physics
	pick := func(flag, cfg, def int) int {
		switch {
		case flag > 0:
			return flag
		case cfg > 0:
			return cfg
		}
		return def
	}
	l := physics.Limits{
		MaxSessions:   pick(p.MaxSessions, c.MaxSessions, defaultMaxSessions),
		SessionTokens: pick(p.SessionTokens, c.SessionTokens, defaultSessionTokens),
		TotalTokens:   pick(p.TotalTokens, c.TotalTokens, defaultTotalTokens),
		Hours:         defaultHours,
	}
	switch {
	case p.Hours > 0:
		l.Hours = p.Hours
	case c.Hours > 0:
		l.Hours = c.Hours
	}
	return l
}

type runParams struct {
	Spec          string  `json:"spec"` // the spec file's text
	Model         string  `json:"model"`
	ManagerModel  string  `json:"manager_model"`
	MaxSessions   int     `json:"max_sessions"`
	SessionTokens int     `json:"session_tokens"`
	TotalTokens   int     `json:"total_tokens"`
	Hours         float64 `json:"hours"`
}

// physicsMethod answers the physics.* methods.
func (s *Server) physicsMethod(c *conn, req *request, bad func(error) (any, *rpcError)) (any, *rpcError) {
	switch req.Method {
	case "physics.run":
		var p runParams
		if err := json.Unmarshal(req.Params, &p); err != nil {
			return bad(err)
		}
		rec, err := s.physicsStart(p)
		if err != nil {
			return nil, physErr(err)
		}
		return runObj(rec), nil
	case "physics.list":
		recs, err := s.st.PhysicsRuns(s.base)
		if err != nil {
			return nil, errf(codeInternal, "%v", err)
		}
		out := make([]map[string]any, len(recs))
		for i, r := range recs {
			out[i] = runObj(s.physicsFresh(r))
		}
		return map[string]any{"runs": out}, nil
	}
	var p struct {
		Run string `json:"run"`
	}
	if err := json.Unmarshal(req.Params, &p); err != nil {
		return bad(err)
	}
	rec, err := s.st.PhysicsRun(s.base, p.Run)
	if errors.Is(err, store.ErrNotFound) {
		return nil, errf(codeNotFound, "no such run %q", p.Run)
	}
	if err != nil {
		return nil, errf(codeInternal, "%v", err)
	}
	switch req.Method {
	case "physics.watch":
		return s.physicsWatch(c, rec)
	case "physics.stop":
		out, err := s.physicsStop(rec)
		if err != nil {
			return nil, physErr(err)
		}
		return runObj(out), nil
	default: // physics.resume
		out, err := s.physicsResume(rec)
		if err != nil {
			return nil, physErr(err)
		}
		return runObj(out), nil
	}
}

// busyError is a refusal because a run is going.
type busyError struct{ id string }

func (e busyError) Error() string { return "a physics run is already going: " + e.id }

// badRunError is a refusal of what was asked.
type badRunError struct{ msg string }

func (e badRunError) Error() string { return e.msg }

func physErr(err error) *rpcError {
	var b busyError
	var r badRunError
	switch {
	case errors.As(err, &b):
		return errf(codeBusy, "%v", err)
	case errors.As(err, &r):
		return errf(codeBadParams, "%v", err)
	}
	return errf(codeInternal, "%v", err)
}

func runObj(r store.PhysicsRun) map[string]any {
	o := map[string]any{
		"id": r.ID, "name": r.Name, "dir": r.RunDir, "state": r.State, "sessions_done": r.SessionsDone, "tokens": r.Tokens,
		"worker_session": r.Worker, "manager_session": r.Manager, "worker_model": r.WorkerModel, "manager_model": r.ManagerModel,
		"started": r.Started.Format(time.RFC3339),
	}
	if !r.Ended.IsZero() {
		o["ended"] = r.Ended.Format(time.RFC3339)
	}
	return o
}

// physicsFresh is the run as the controller has it now (the stored row lags
// the running counters).
func (s *Server) physicsFresh(r store.PhysicsRun) store.PhysicsRun {
	s.physMu.Lock()
	a := s.physActive
	s.physMu.Unlock()
	if a != nil && a.id == r.ID {
		return a.snapshot()
	}
	return r
}

// physicsRecover marks the runs a crashed daemon left going as interrupted.
func (s *Server) physicsRecover() {
	recs, err := s.st.PhysicsRuns(s.base)
	if err != nil {
		return
	}
	for _, r := range recs {
		if r.PhysicsActive() {
			r.Phase, r.State = r.State, "interrupted"
			_ = s.st.SavePhysicsRun(s.base, r)
		}
	}
}

// ---- starting a run ----

// physicsStart validates the spec and the settings, makes the run directory
// and the two sessions, and starts the controller.
func (s *Server) physicsStart(p runParams) (store.PhysicsRun, error) {
	if s.cfg.Physics.Python == "" {
		return store.PhysicsRun{}, badRunError{"physics.python is not set, so there is no runtime for the worker's commands"}
	}
	runtime := filepath.Dir(s.cfg.Physics.Python)
	if fi, err := os.Stat(runtime); err != nil || !fi.IsDir() {
		return store.PhysicsRun{}, badRunError{"the physics runtime " + runtime + " does not exist"}
	}
	spec, err := physics.ParseSpec(p.Spec, s.cfg.Physics.DataDirs)
	if err != nil {
		return store.PhysicsRun{}, badRunError{err.Error()}
	}
	worker := firstNonEmpty(p.Model, s.cfg.Physics.Model, s.cfg.Model.Default)
	manager := firstNonEmpty(p.ManagerModel, s.cfg.Physics.ManagerModel, worker)
	for _, m := range []string{worker, manager} {
		if _, err := s.cfg.Endpoint(m); err != nil {
			return store.PhysicsRun{}, badRunError{fmt.Sprintf("model %q: %v", m, err)}
		}
	}
	lim := s.physicsLimits(p)

	root := s.cfg.Physics.WorkspaceRoot
	if root == "" {
		root = filepath.Join(s.opt.Home, "workspaces")
	}
	if err := os.MkdirAll(root, 0o755); err != nil {
		return store.PhysicsRun{}, err
	}
	base := time.Now().Format("20060102_150405") + "_" + spec.Name + "_" + modelSlugRe.ReplaceAllString(worker, "-")
	r := &physRun{s: s, spec: spec, specText: p.Spec, lim: lim, watchers: map[*conn]bool{}, done: make(chan struct{})}

	s.physMu.Lock()
	if s.physActive != nil || s.isClosing() {
		busy := s.physActive
		s.physMu.Unlock()
		if busy != nil {
			return store.PhysicsRun{}, busyError{busy.id}
		}
		return store.PhysicsRun{}, errors.New("the daemon is shutting down")
	}
	// The run directory is made while the slot is held, so its name is unique.
	id, dir := base, ""
	for i := 2; ; i++ {
		dir = filepath.Join(root, id)
		if err := os.Mkdir(dir, 0o755); err == nil {
			break
		} else if !os.IsExist(err) {
			s.physMu.Unlock()
			return store.PhysicsRun{}, err
		}
		id = fmt.Sprintf("%s-%d", base, i)
	}
	r.id = id
	s.physActive = r
	s.physMu.Unlock()

	rec, err := s.physicsSetup(r, dir, worker, manager)
	if err == nil {
		r.init(rec)
		err = s.st.CreatePhysicsRun(s.base, rec)
	}
	if err != nil {
		_ = os.RemoveAll(dir)
		s.physMu.Lock()
		s.physActive = nil
		s.physMu.Unlock()
		close(r.done)
		return store.PhysicsRun{}, err
	}
	r.begin()
	return rec, nil
}

func firstNonEmpty(v ...string) string {
	for _, s := range v {
		if strings.TrimSpace(s) != "" {
			return strings.TrimSpace(s)
		}
	}
	return ""
}

// physicsSetup writes the run's files, its spec and its sessions.
func (s *Server) physicsSetup(r *physRun, dir, worker, manager string) (store.PhysicsRun, error) {
	ctx := s.base
	real, err := filepath.EvalSymlinks(dir)
	if err != nil {
		return store.PhysicsRun{}, err
	}
	dir = real
	if err := os.MkdirAll(s.physicsDir(), 0o700); err != nil {
		return store.PhysicsRun{}, err
	}
	specPath := filepath.Join(s.physicsDir(), r.id+".toml")
	if err := os.WriteFile(specPath, []byte(r.specText), 0o600); err != nil {
		return store.PhysicsRun{}, err
	}
	name, email := s.cfg.Git.AuthorName, s.cfg.Git.AuthorEmail
	if name == "" || email == "" {
		name, email = "son-of-anton", "son-of-anton@localhost"
	}
	r.repo = physics.Repo{Dir: dir, GitDir: filepath.Join(s.physicsDir(), r.id+".git"), Name: name, Email: email}
	if err := r.repo.Init(); err != nil {
		return store.PhysicsRun{}, err
	}
	keys := r.spec.Keys()
	problemMD := physics.ProblemMD(r.spec.Problem, keys)
	files := map[string]string{".gitignore": physics.Gitignore, "PROBLEM.md": problemMD}
	for n, text := range files {
		if err := os.WriteFile(filepath.Join(dir, n), []byte(text), 0o644); err != nil {
			return store.PhysicsRun{}, err
		}
	}
	cfgJSON, _ := json.MarshalIndent(map[string]any{
		"name": r.spec.Name, "data": r.spec.Data, "worker_model": worker, "manager_model": manager, "limits": r.lim,
	}, "", "  ")
	if err := os.WriteFile(filepath.Join(dir, "config.json"), append(cfgJSON, '\n'), 0o644); err != nil {
		return store.PhysicsRun{}, err
	}
	if err := r.repo.Commit("start: " + r.spec.Name); err != nil {
		return store.PhysicsRun{}, err
	}

	mk := func(agentName, model, text string) (string, error) {
		hash, err := s.st.PutPrompt(ctx, text)
		if err != nil {
			return "", err
		}
		sess, err := s.st.CreateSession(ctx, store.Session{
			Kind: "physics", Agent: agentName, Source: "physics", Title: r.id + " (" + agentName + ")", TitleSource: "user",
			Cwd: dir, Workspace: dir, Model: model, PromptHash: hash, Mode: string(approval.Yolo),
		})
		return sess.ID, err
	}
	data := r.spec.DataReal
	if len(data) == 0 {
		data = []string{"(no data paths)"}
	}
	wid, err := mk("worker", worker, physics.WorkerPrompt(dir, data, keys))
	if err != nil {
		return store.PhysicsRun{}, err
	}
	mid, err := mk("manager", manager, physics.ManagerPrompt(dir, keys, r.spec.Problem))
	if err != nil {
		return store.PhysicsRun{}, err
	}
	limits, _ := json.Marshal(r.lim)
	rec := store.PhysicsRun{
		ID: r.id, Name: r.spec.Name, SpecPath: specPath, RunDir: dir, Worker: wid, Manager: mid, State: "working",
		WorkerModel: worker, ManagerModel: manager, Limits: string(limits), Started: time.Now(),
		NextMessage: physics.BeginMessage(problemMD),
	}
	return rec, nil
}

// ---- resume, stop, watch ----

func (s *Server) physicsResume(rec store.PhysicsRun) (store.PhysicsRun, error) {
	if rec.State != "interrupted" {
		return rec, badRunError{fmt.Sprintf("run %s is %s; only an interrupted run can be resumed", rec.ID, rec.State)}
	}
	r, err := s.physicsAdopt(rec)
	if err != nil {
		return rec, err
	}
	rec.State, rec.Phase = rec.Phase, ""
	if rec.State != "reviewing" {
		rec.State = "working"
	}
	r.init(rec)
	if err := s.st.SavePhysicsRun(s.base, rec); err != nil {
		s.physMu.Lock()
		s.physActive = nil
		s.physMu.Unlock()
		close(r.done)
		return rec, err
	}
	r.begin()
	return rec, nil
}

// physicsAdopt makes the controller of a stored run and takes the one run slot.
func (s *Server) physicsAdopt(rec store.PhysicsRun) (*physRun, error) {
	text, err := os.ReadFile(rec.SpecPath)
	if err != nil {
		return nil, fmt.Errorf("reading the run's spec: %v", err)
	}
	spec, err := physics.ParseSpec(string(text), s.cfg.Physics.DataDirs)
	if err != nil {
		return nil, badRunError{err.Error()}
	}
	var lim physics.Limits
	if err := json.Unmarshal([]byte(rec.Limits), &lim); err != nil {
		return nil, err
	}
	name, email := s.cfg.Git.AuthorName, s.cfg.Git.AuthorEmail
	if name == "" || email == "" {
		name, email = "son-of-anton", "son-of-anton@localhost"
	}
	r := &physRun{s: s, id: rec.ID, spec: spec, specText: string(text), lim: lim, watchers: map[*conn]bool{}, done: make(chan struct{}),
		repo: physics.Repo{Dir: rec.RunDir, GitDir: filepath.Join(s.physicsDir(), rec.ID+".git"), Name: name, Email: email}}
	s.physMu.Lock()
	defer s.physMu.Unlock()
	if s.physActive != nil || s.isClosing() {
		if s.physActive != nil {
			return nil, busyError{s.physActive.id}
		}
		return nil, errors.New("the daemon is shutting down")
	}
	s.physActive = r
	return r, nil
}

// physicsStop interrupts a going run and scores what is there; an interrupted
// run is just scored. The state is stopped.
func (s *Server) physicsStop(rec store.PhysicsRun) (store.PhysicsRun, error) {
	s.physMu.Lock()
	a := s.physActive
	s.physMu.Unlock()
	if a != nil && a.id == rec.ID {
		a.requestStop()
		<-a.done
		return s.st.PhysicsRun(s.base, rec.ID)
	}
	if rec.State != "interrupted" {
		return rec, badRunError{fmt.Sprintf("run %s is %s, not going", rec.ID, rec.State)}
	}
	r, err := s.physicsAdopt(rec)
	if err != nil {
		return rec, err
	}
	r.init(rec)
	r.finalize("stopped")
	return s.st.PhysicsRun(s.base, rec.ID)
}

// physicsWatch registers c for the run's lines and replies with those so far.
func (s *Server) physicsWatch(c *conn, rec store.PhysicsRun) (any, *rpcError) {
	s.physMu.Lock()
	a := s.physActive
	s.physMu.Unlock()
	if a != nil && a.id == rec.ID {
		lines, state := a.watch(c)
		return map[string]any{"state": state, "lines": lines}, nil
	}
	var lines []string
	if b, err := os.ReadFile(filepath.Join(s.physicsDir(), rec.ID+".log")); err == nil {
		lines = strings.Split(strings.TrimRight(string(b), "\n"), "\n")
	}
	if lines == nil {
		lines = []string{}
	}
	return map[string]any{"state": rec.State, "lines": lines}, nil
}

// ---- the sessions' runners ----

// physicsRunner turns a session runner into a physics session's: the worker
// sandboxed in its run directory, the manager reading it and nothing else.
// Neither has memory, recall, web, MCP, a questioner or an approver.
func (s *Server) physicsRunner(r *agent.Runner, rec store.Session) error {
	run, err := s.st.PhysicsRunBySession(s.base, rec.ID)
	if err != nil {
		return fmt.Errorf("session %s belongs to no physics run: %v", rec.ID, err)
	}
	r.Recall, r.SearchMessages, r.Memory, r.MCP, r.Web, r.LSP, r.Cron, r.Vision = nil, nil, nil, nil, nil, nil, nil, nil
	r.Ask, r.Approver, r.AsyncDelivery, r.Relay = nil, nil, false, nil
	r.Mode = approval.Yolo
	r.MaxSteps = physicsMaxStepsDefault
	if s.cfg.Physics.MaxSteps > 0 {
		r.MaxSteps = s.cfg.Physics.MaxSteps
	}
	if rec.Agent == "worker" {
		var data []string
		if text, err := os.ReadFile(run.SpecPath); err == nil {
			data = specData(string(text))
		}
		r.Sandbox = &sandbox.Policy{
			RunDir: run.RunDir, Data: data, Runtime: filepath.Dir(s.cfg.Physics.Python), Home: filepath.Join(run.RunDir, ".home"),
		}
		return nil
	}
	r.Tools = s.reg.Only("read_file", "search_files").With(reviewTool{srv: s, run: run.ID})
	r.Policy = paths.Policy{Home: s.userHome(), SoaHome: s.opt.Home, Confine: run.RunDir, ConfineNote: "read: " + run.RunDir}
	return nil
}

// specData is the resolved data paths of a spec, read leniently (a run's
// sessions can be loaded to be watched after its data has moved).
func specData(text string) []string {
	var raw struct {
		Data []string `toml:"data"`
	}
	if _, err := toml.Decode(text, &raw); err != nil {
		return nil
	}
	var out []string
	for _, d := range raw.Data {
		if real, err := filepath.EvalSymlinks(d); err == nil {
			out = append(out, real)
		}
	}
	return out
}

// reviewTool is the manager's way to give its verdict.
type reviewTool struct {
	srv *Server
	run string
}

func (reviewTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "review",
		Description: "Give your verdict on the work session you were asked to review. accept: the work is finished. " +
			"continue: the agent goes back to work with your feedback, which must say what is wrong and what to do about it.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "verdict": {"type": "string", "enum": ["accept", "continue"]},
    "feedback": {"type": "string", "description": "What is wrong and what to do about it, most important first; with accept, what you checked."}
  },
  "required": ["verdict", "feedback"]
}`),
	}}
}

func (t reviewTool) Run(_ context.Context, _ *tools.Env, raw json.RawMessage) (string, error) {
	fail := func(msg string) (string, error) {
		b, _ := json.Marshal(map[string]string{"error": msg})
		return string(b), nil
	}
	var a struct {
		Verdict  string `json:"verdict"`
		Feedback string `json:"feedback"`
	}
	if err := json.Unmarshal(raw, &a); err != nil {
		return fail("invalid arguments: " + err.Error())
	}
	if a.Verdict != "accept" && a.Verdict != "continue" {
		return fail("verdict must be accept or continue")
	}
	s := t.srv
	s.physMu.Lock()
	r := s.physActive
	s.physMu.Unlock()
	if r == nil || r.id != t.run {
		return fail("this run is not going, so there is nothing to review")
	}
	r.setVerdict(a.Verdict, a.Feedback)
	return `{"recorded": true}`, nil
}

func (s *Server) isClosing() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.closing
}
