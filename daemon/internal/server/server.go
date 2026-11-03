// Package server is soa-d's unix-socket JSON-RPC server: it owns sessions,
// runs their turns, keeps the durable queue of pending sends, asks the
// attached driver for approvals and answers, and streams events to every
// attached client.
//
// Concurrency: each loaded session (live) changes its state only under its own
// mutex, which is never held across a call into the agent loop or a write to
// a socket (events are appended to a connection's outgoing queue, which never
// blocks). Lock order is Server.mu, then live.mu, then conn.omu.
package server

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/lsp"
	"github.com/ewtodd/son-of-anton/daemon/internal/mcp"
	"github.com/ewtodd/son-of-anton/daemon/internal/memory"
	"github.com/ewtodd/son-of-anton/daemon/internal/paths"
	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/recall"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/subagent"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// ErrRunning means another daemon is serving this home.
var ErrRunning = errors.New("server: another daemon is serving this home")

const shutdownWait = 10 * time.Second

// Options configure New.
type Options struct{ Home, Version string }

// Listen creates <home>/run (0700) and listens on <home>/run/soa-d.sock
// (0600). A live daemon behind an existing socket is ErrRunning; a stale file
// is replaced.
func Listen(home string) (net.Listener, error) {
	dir := filepath.Join(home, "run")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		return nil, err
	}
	sock := filepath.Join(dir, "soa-d.sock")
	if _, err := os.Lstat(sock); err == nil {
		if c, err := net.DialTimeout("unix", sock, 2*time.Second); err == nil {
			_ = c.Close()
			return nil, ErrRunning
		}
		if err := os.Remove(sock); err != nil {
			return nil, err
		}
	}
	ln, err := net.Listen("unix", sock)
	if err != nil {
		return nil, err
	}
	if err := os.Chmod(sock, 0o600); err != nil {
		_ = ln.Close()
		return nil, err
	}
	return ln, nil
}

// Server is one daemon.
type Server struct {
	opt     Options
	cfg     *config.Config
	st      *store.Store
	reg     *tools.Registry // the builtin tools (a child's are drawn from it)
	regTask *tools.Registry // reg plus the task tool: an interactive session's
	regChat *tools.Registry // a chat session's: notes, memory, recall, web, vision, cron, relay; no terminal
	regCron *tools.Registry // a cron job's: every builtin tool but clarify
	cron    *cronSched
	web     *tools.Web   // the web tools' settings, shared by every session
	mcp     *mcp.Manager // nil: no MCP server is enabled
	lsp     *lsp.Service // nil: LSP is disabled
	chats   *chatHub     // nil: Signal is off
	served  sync.Map     // session id -> the model that answered its last reply in this run
	noted   sync.Map     // chat stamp -> the models note last given to that chat in this run
	mem     *memory.Store
	idx     *recall.Index   // nil: recall is off
	base    context.Context // cancelled when shutdown starts
	stop    context.CancelFunc

	mu      sync.Mutex
	live    map[string]*live
	conns   map[*conn]struct{}
	closing bool

	metaMu  sync.Mutex
	meta    map[string]map[string]int // base URL -> model -> context length
	learned map[string]int            // session -> a limit an overflow error stated

	indexedMu sync.Mutex
	indexed   map[string]int64 // session -> last seq passed to the recall index

	turns  sync.WaitGroup // running turn goroutines
	connWG sync.WaitGroup // connection goroutines
}

// New loads the config and opens <home>/soa-d.db.
func New(o Options) (*Server, error) {
	cfg, err := config.Load(o.Home)
	if err != nil {
		return nil, err
	}
	st, err := store.Open(filepath.Join(o.Home, "soa-d.db"))
	if err != nil {
		return nil, err
	}
	mem, idx := recall.Memory(cfg, o.Home, true)
	mcpm := MCPManager(cfg, o.Home)
	lspS := LSPService(cfg)
	base := tools.Builtin()
	if mcpm != nil {
		base = append(base, tools.MCPTools(mcpm.Names())...)
	}
	s := &Server{opt: o, cfg: cfg, st: st, reg: tools.NewRegistry(base...), mcp: mcpm, lsp: lspS, mem: mem, idx: idx,
		live: map[string]*live{}, indexed: map[string]int64{}, conns: map[*conn]struct{}{},
		meta: map[string]map[string]int{}, learned: map[string]int{}}
	s.cron = s.newCron()
	cronTool := tools.CronJob(o.Home)
	s.regTask = tools.NewRegistry(append(base, subagent.Tool(subagent.Builtin(), true, s.spawnTask), cronTool)...)
	s.regChat = s.reg.Only("read_file", "write_file", "patch", "search_files", "todo", "clarify", "memory", "session_search", "web_search", "web_extract", "vision_analyze").With(cronTool, tools.RelayTool())
	var cronTools []string
	for _, n := range s.reg.Names() {
		if n != "clarify" {
			cronTools = append(cronTools, n)
		}
	}
	s.regCron = s.reg.Only(cronTools...)
	s.web = WebConfig(cfg, o.Home)
	s.chats = s.newChatHub()
	s.base, s.stop = context.WithCancel(context.Background())
	return s, nil
}

// Serve accepts connections until ctx is done, then shuts down cleanly.
func (s *Server) Serve(ctx context.Context, ln net.Listener) error {
	s.applyRenames()
	s.backfill()
	if s.cfg.Daemon.Cron {
		s.cron.start()
	}
	if s.chats != nil {
		s.chats.start()
	}
	s.watchWorkerHours()
	stopAccept := context.AfterFunc(ctx, func() { _ = ln.Close() })
	defer stopAccept()
	var acceptErr error
	for {
		nc, err := ln.Accept()
		if err != nil {
			if ctx.Err() == nil {
				acceptErr = err
			}
			break
		}
		s.mu.Lock()
		if s.closing {
			s.mu.Unlock()
			_ = nc.Close()
			continue
		}
		c := newConn(s, nc)
		s.conns[c] = struct{}{}
		s.connWG.Add(1)
		s.mu.Unlock()
		go c.run()
	}
	_ = ln.Close()
	s.shutdown()
	return acceptErr
}

func (s *Server) shutdown() {
	s.mu.Lock()
	s.closing = true
	conns := make([]*conn, 0, len(s.conns))
	for c := range s.conns {
		conns = append(conns, c)
	}
	s.mu.Unlock()
	s.stop()
	s.cron.stop()
	for _, c := range conns {
		c.close()
	}
	s.connWG.Wait()

	s.mu.Lock()
	lives := make([]*live, 0, len(s.live))
	for _, l := range s.live {
		lives = append(lives, l)
	}
	s.mu.Unlock()
	for _, l := range lives {
		l.shutdown()
	}
	done := make(chan struct{})
	go func() { s.turns.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(shutdownWait):
	}
	for _, l := range lives {
		_ = l.sess.Close()
	}
	if s.chats != nil {
		s.chats.stop()
	}
	if s.mcp != nil {
		s.mcp.Close()
	}
	s.lsp.Close()
	if s.idx != nil {
		_ = s.idx.Close()
	}
	_ = s.st.Close()
}

func (s *Server) userHome() string {
	h, _ := os.UserHomeDir()
	return h
}

// runner builds a per-session Runner: the shared pieces plus this session's
// approver and questioner.
func (s *Server) runner(l *live, ep config.Endpoint, mode approval.Mode, source string, warnings *[]string) *agent.Runner {
	r := &agent.Runner{
		Store:           s.st,
		Client:          provider.New(ep.BaseURL, ep.APIKey),
		Model:           ep.Model,
		ReasoningEffort: ep.ReasoningEffort,
		Source:          source,
		Tools:           s.regTask,
		Allowlist:       s.st.Allowlist(),
		Policy:          paths.Policy{Home: s.userHome(), SoaHome: s.opt.Home, WriteExceptions: s.cfg.Security.SensitivePathExceptions},
		CommandHome:     s.cfg.CommandHome(),
		Mode:            mode,
		Approver:        l.approve,
		AsyncDelivery:   true,
		Ask:             l.ask,
		Compaction:      s.compaction(ep, l.id),
		Memory:          s.mem,
		MemoryScope:     "common", // every soa-d session is common until phase 5
		MemoryApproval:  s.cfg.Memory.WriteApproval,
		Web:             s.web,
		MCP:             s.mcp,
		LSP:             s.lsp,
		Vision:          VisionConfig(s.cfg, ep),
		Cron:            s.cronHost(""),
		Default:         s.defaultRoute(""),
		OnDeadPin:       func(old string, def agent.Fallback) { s.deadPin(l, old, def) },
		OnReply:         func(served string) { s.served.Store(l.id, served) },
		Prompt: func(_ context.Context, ss store.Session) (string, error) {
			r, err := prompt.Build(prompt.Options{
				Home: s.opt.Home, Cwd: ss.Cwd, Scope: "common", Memory: true,
				Now: time.Now().In(prompt.LocalZone()), RulesCharLimit: s.cfg.Memory.RulesCharLimit,
			})
			if warnings != nil {
				*warnings = append(*warnings, r.Warnings...)
			}
			return r.Text, err
		},
	}
	if s.idx != nil {
		r.Recall = func(ctx context.Context, text string) string { return s.idx.Recall(ctx, "common", text) }
		r.SearchMessages = s.searchMessages("common")
	}
	return r
}

// create makes a new interactive session and returns its ID. It is not left
// loaded: attaching loads it.
func (s *Server) create(ctx context.Context, cwd, source string) (string, []string, error) {
	return s.createAs(ctx, "interactive", "", cwd, source)
}

// createAs is create for a session of another kind; owner is the chat that
// starts a worker.
func (s *Server) createAs(ctx context.Context, kind, owner, cwd, source string) (string, []string, error) {
	ep, err := s.cfg.Endpoint("")
	if err != nil {
		return "", nil, err
	}
	mode := s.cfg.PermissionMode()
	var warnings []string
	r := s.runner(&live{}, ep, mode, source, &warnings)
	r.Owner = owner
	sess, err := r.NewSession(ctx, kind, cwd)
	if err != nil {
		return "", nil, err
	}
	defer sess.Close()
	if err := s.st.UpdateSessionSettings(ctx, sess.ID(), ep.Model, ep.ReasoningEffort, string(mode)); err != nil {
		return "", nil, err
	}
	return sess.ID(), warnings, nil
}

// load returns the loaded session, loading it from the store if needed.
func (s *Server) load(ctx context.Context, id string) (*live, error) {
	// The provider's model metadata is fetched before the lock is taken.
	if rec, err := s.st.Session(ctx, id); err == nil {
		if ep, err := s.cfg.Endpoint(rec.Model); err == nil {
			s.fetchMeta(ctx, ep, false)
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closing {
		return nil, errors.New("the daemon is shutting down")
	}
	if l, ok := s.live[id]; ok {
		return l, nil
	}
	rec, err := s.st.Session(ctx, id)
	if err != nil {
		return nil, err
	}
	if rec.Kind == "chat" && rec.Model == "" {
		// The chat follows the instance's chat model until /model chooses one.
		rec.Model, rec.ReasoningEffort = s.chatModel(), ""
	}
	ep, err := s.cfg.Endpoint(rec.Model)
	if err != nil {
		return nil, err
	}
	if rec.ReasoningEffort != "" {
		ep.ReasoningEffort = rec.ReasoningEffort
	}
	mode, err := approval.ParseMode(rec.Mode)
	if err != nil {
		mode = s.cfg.PermissionMode()
	}
	sum, err := s.st.SessionSummary(ctx, id)
	if err != nil {
		return nil, err
	}
	last, _, err := s.st.MessagesBefore(ctx, id, 0, 1)
	if err != nil {
		return nil, err
	}
	l := newLive(s, rec, sum)
	l.title = rec.Title
	if rec.Kind == "child" {
		rs, err := s.st.RunStats(ctx, id)
		if err != nil {
			return nil, err
		}
		l.toolCalls = rs.ToolCalls
	}
	comps, err := s.st.Compactions(ctx, id)
	if err != nil {
		return nil, err
	}
	l.compactions = len(comps)
	if len(last) > 0 {
		l.announced = last[0].Seq
	}
	r := s.runner(l, ep, mode, rec.Source, nil)
	if rec.Kind == "child" {
		r = s.childRunner(r, l, rec)
	}
	if rec.Kind == "chat" {
		s.chatRunner(r, rec.Chat, l)
		if s.chats != nil {
			l.chat = &chatLink{cs: s.chats.state(rec.Chat), l: l}
		}
		l.waiting = false
	}
	if rec.Kind == "worker" {
		if s.chats != nil && rec.Owner != "" {
			l.chat = &workerLink{cs: s.chats.state(rec.Owner), l: l}
		}
		l.waiting = false
	}
	sess, err := r.Resume(ctx, id)
	if err != nil {
		return nil, err
	}
	l.sess = sess
	l.model, l.effort, l.mode = ep.Model, ep.ReasoningEffort, string(mode)
	l.ctxKnown = sess.ContextLength()
	sess.OnNotify(l.drainNotices)
	// The count of running processes is part of the session object: a kill
	// or a process ending on its own is news to the clients.
	sess.OnBackgroundExit(func() {
		l.mu.Lock()
		defer l.mu.Unlock()
		l.emitSession()
	})
	s.live[id] = l
	return l, nil
}

// maybeUnload closes a session nobody needs: no client attached, no turn, no
// background process.
func (s *Server) maybeUnload(l *live) {
	s.mu.Lock()
	l.mu.Lock()
	ok := !l.unloaded && !l.closing && len(l.viewers) == 0 && !l.running && !l.compacting && len(l.bg) == 0 && l.sess.BackgroundRunning() == 0
	if ok {
		l.unloaded = true
		delete(s.live, l.id)
	}
	l.mu.Unlock()
	s.mu.Unlock()
	if ok {
		_ = l.sess.Close()
	}
}

func (s *Server) loaded(id string) *live {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.live[id]
}

func errf(code int, format string, a ...any) *rpcError {
	return &rpcError{Code: code, Message: fmt.Sprintf(format, a...)}
}
