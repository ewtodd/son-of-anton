// Package lsp asks a file's language server for diagnostics after an edit,
// so Write and Edit can report the errors the edit introduced. One
// server process runs per (server, project root), started on first use and
// stopped when idle or when the service closes. Nothing here may fail or
// hold up a write: every problem ends as "no report".
package lsp

import (
	"context"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// Config is the [lsp] table. Zero durations mean the defaults, except
// IdleTimeout, where a negative value means never.
type Config struct {
	Disabled    bool
	WaitTimeout time.Duration
	IdleTimeout time.Duration
	Servers     map[string]ServerConfig
}

// ServerConfig is one [lsp.servers.<id>] table. Extensions and RootMarkers
// define a server the built-in table lacks.
type ServerConfig struct {
	Disabled    bool
	Command     []string
	Env         map[string]string
	InitOptions any
	Extensions  []string
	RootMarkers []string
}

const (
	defaultWait = 5 * time.Second
	defaultIdle = 10 * time.Minute
)

// Service owns the language servers of one daemon. A nil *Service does
// nothing.
type Service struct {
	cfg  Config
	wait time.Duration
	ctx  context.Context
	stop context.CancelFunc
	done chan struct{} // closed when the reaper has ended

	mu      sync.Mutex
	closed  bool
	clients map[string]*client
	broken  map[string]bool
	wg      sync.WaitGroup // servers being stopped
}

// New returns the service, or nil when LSP is disabled.
func New(cfg Config) *Service {
	if cfg.Disabled {
		return nil
	}
	s := &Service{cfg: cfg, wait: cfg.WaitTimeout, clients: map[string]*client{}, broken: map[string]bool{}, done: make(chan struct{})}
	if s.wait <= 0 {
		s.wait = defaultWait
	}
	if s.cfg.IdleTimeout == 0 {
		s.cfg.IdleTimeout = defaultIdle
	}
	s.ctx, s.stop = context.WithCancel(context.Background())
	if s.cfg.IdleTimeout > 0 {
		go s.reap()
	} else {
		close(s.done)
	}
	return s
}

// Close stops every server and waits for them to end.
func (s *Service) Close() {
	if s == nil {
		return
	}
	s.mu.Lock()
	s.closed = true
	live := s.clients
	s.clients = map[string]*client{}
	s.mu.Unlock()
	s.stop()
	<-s.done
	for _, c := range live {
		s.wg.Add(1)
		go func() {
			defer s.wg.Done()
			c.stop()
		}()
	}
	s.wg.Wait()
}

// reap stops servers that have been unused for the idle timeout.
func (s *Service) reap() {
	defer close(s.done)
	tick := time.NewTicker(max(s.cfg.IdleTimeout/4, 50*time.Millisecond))
	defer tick.Stop()
	for {
		select {
		case <-s.ctx.Done():
			return
		case now := <-tick.C:
			s.mu.Lock()
			for k, c := range s.clients {
				if c.busy == 0 && now.Sub(c.last) > s.cfg.IdleTimeout {
					delete(s.clients, k)
					s.wg.Add(1)
					go func() {
						defer s.wg.Done()
						c.stop()
					}()
				}
			}
			s.mu.Unlock()
		}
	}
}

// Edit is one file edit being watched: the diagnostics before it, and what
// is needed to ask again after.
type Edit struct {
	s        *Service
	sp       *spec
	path     string
	baseline []Diagnostic
}

// Begin notes the file's diagnostics before an edit. It never starts a
// server and never waits. The result may be nil, and Finish accepts that.
func (s *Service) Begin(path string) *Edit {
	if s == nil {
		return nil
	}
	sp := s.resolve(path)
	if sp == nil {
		return nil
	}
	e := &Edit{s: s, sp: sp, path: path}
	s.mu.Lock()
	c := s.clients[sp.key()]
	s.mu.Unlock()
	if c != nil {
		e.baseline, _ = c.current(path)
	}
	return e
}

// Finish sends the file's new text to its language server and returns the
// report of the errors the edit introduced, or "" when there are none or
// the server did not answer in time. It returns within the wait timeout
// plus a second at the latest.
func (e *Edit) Finish(ctx context.Context, text string) string {
	if e == nil {
		return ""
	}
	s := e.s
	cctx, cancel := context.WithTimeout(ctx, s.wait)
	defer cancel()
	out := make(chan string, 1)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				log.Printf("lsp: %v", r)
				out <- ""
			}
		}()
		out <- e.run(cctx, text)
	}()
	timer := time.NewTimer(s.wait + time.Second)
	defer timer.Stop()
	select {
	case r := <-out:
		return r
	case <-timer.C:
	case <-ctx.Done():
	case <-s.ctx.Done():
	}
	return ""
}

func (e *Edit) run(ctx context.Context, text string) string {
	c := e.s.acquire(ctx, e.sp)
	if c == nil {
		return ""
	}
	defer e.s.release(c)
	ver, err := c.sync(e.path, e.sp.language, text)
	if err != nil {
		return ""
	}
	diags, ok := c.wait(ctx, e.path, ver)
	if !ok {
		return ""
	}
	return Report(e.path, newErrors(e.baseline, diags))
}

// acquire returns the ready client for a spec, starting it if need be, and
// marks it in use. It returns nil when there is no usable server.
func (s *Service) acquire(ctx context.Context, sp *spec) *client {
	key := sp.key()
	s.mu.Lock()
	if s.closed || s.broken[key] {
		s.mu.Unlock()
		return nil
	}
	c := s.clients[key]
	if c == nil {
		var err error
		if c, err = spawn(sp, s.exited); err != nil {
			s.broken[key] = true
			s.mu.Unlock()
			log.Printf("lsp: %s for %s did not start: %v", sp.id, sp.root, err)
			return nil
		}
		s.clients[key] = c
		go func() {
			c.initialize(sp)
			if c.err != nil {
				s.fail(sp, c, c.err)
			}
		}()
	}
	c.busy++
	c.last = time.Now()
	s.mu.Unlock()
	select {
	case <-c.ready:
		if c.err == nil {
			return c
		}
	case <-ctx.Done():
	}
	s.release(c)
	return nil
}

func (s *Service) release(c *client) {
	s.mu.Lock()
	c.busy--
	c.last = time.Now()
	s.mu.Unlock()
}

// fail retires a server that could not be initialized; it is not tried
// again for that root.
func (s *Service) fail(sp *spec, c *client, err error) {
	s.mu.Lock()
	first := !s.broken[sp.key()] && !s.closed
	s.broken[sp.key()] = true
	if s.clients[sp.key()] == c {
		delete(s.clients, sp.key())
	}
	// Close stops the clients it took, this one among them.
	stopIt := !s.closed
	if stopIt {
		s.wg.Add(1)
	}
	s.mu.Unlock()
	if first {
		log.Printf("lsp: %s for %s failed to start: %v", sp.id, sp.root, err)
	}
	if stopIt {
		go func() {
			defer s.wg.Done()
			c.stop()
		}()
	}
}

// exited forgets a server whose process has ended on its own.
func (s *Service) exited(c *client) {
	s.mu.Lock()
	defer s.mu.Unlock()
	key := c.id + "\x00" + c.root
	if s.clients[key] == c {
		delete(s.clients, key)
	}
}

// spec is a server chosen for a file.
type spec struct {
	id, root, language string
	argv, env          []string
	init               any
}

func (sp *spec) key() string { return sp.id + "\x00" + sp.root }

// resolve picks the server for a file, or nil when none applies: no server
// claims the extension, it is disabled, its command is missing, or the file
// is not inside a git worktree.
func (s *Service) resolve(path string) *spec {
	if !filepath.IsAbs(path) {
		return nil
	}
	ext := strings.ToLower(filepath.Ext(path))
	for _, id := range s.serverIDs() {
		def, over := s.def(id)
		if over.Disabled || !contains(def.exts, ext) {
			continue
		}
		argv := over.Command
		if len(argv) == 0 {
			argv = firstFound(def.commands)
		}
		if len(argv) == 0 {
			continue
		}
		bin, err := exec.LookPath(argv[0])
		if err != nil {
			continue
		}
		git := gitRoot(filepath.Dir(path))
		if git == "" {
			return nil
		}
		sp := &spec{id: id, root: nearestRoot(filepath.Dir(path), git, def.markers), language: languageByExt[ext],
			argv: append([]string{bin}, argv[1:]...), init: over.InitOptions}
		if sp.language == "" {
			sp.language = "plaintext"
		}
		keys := make([]string, 0, len(over.Env))
		for k := range over.Env {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			sp.env = append(sp.env, k+"="+over.Env[k])
		}
		return sp
	}
	return nil
}

// serverIDs lists the built-in servers in their table order, then the
// configured ones the table lacks, sorted.
func (s *Service) serverIDs() []string {
	ids := make([]string, 0, len(builtin)+len(s.cfg.Servers))
	known := map[string]bool{}
	for _, d := range builtin {
		ids = append(ids, d.id)
		known[d.id] = true
	}
	var extra []string
	for id := range s.cfg.Servers {
		if !known[id] {
			extra = append(extra, id)
		}
	}
	sort.Strings(extra)
	return append(ids, extra...)
}

// def merges a server's built-in definition with its configuration.
func (s *Service) def(id string) (serverDef, ServerConfig) {
	over := s.cfg.Servers[id]
	for _, d := range builtin {
		if d.id == id {
			return d, over
		}
	}
	d := serverDef{id: id, exts: over.Extensions, markers: over.RootMarkers}
	for i, e := range d.exts {
		d.exts[i] = strings.ToLower(e)
	}
	return d, over
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// firstFound is the first candidate command whose program is on PATH.
func firstFound(candidates [][]string) []string {
	for _, c := range candidates {
		if _, err := exec.LookPath(c[0]); err == nil {
			return c
		}
	}
	return nil
}

// gitRoot is the nearest ancestor of dir (itself included) holding a .git,
// a file or a directory; "" when there is none.
func gitRoot(dir string) string {
	for {
		if _, err := os.Lstat(filepath.Join(dir, ".git")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return ""
		}
		dir = parent
	}
}

// nearestRoot is the nearest ancestor of dir, no higher than the git root,
// holding one of the markers; the git root when none does.
func nearestRoot(dir, git string, markers []string) string {
	for {
		for _, m := range markers {
			if _, err := os.Stat(filepath.Join(dir, m)); err == nil {
				return dir
			}
		}
		if dir == git || dir == filepath.Dir(dir) {
			return git
		}
		dir = filepath.Dir(dir)
	}
}
