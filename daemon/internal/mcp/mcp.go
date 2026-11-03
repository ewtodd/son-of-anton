// Package mcp is the daemon's MCP client: it connects to the instance's
// servers (stdio children and streamable HTTP endpoints), keeps a catalog of
// their tools and runs calls. The model reaches the catalog through three
// fixed tools (tools/mcp.go), so no MCP schema rides a request.
package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/modelcontextprotocol/go-sdk/jsonrpc"
	sdk "github.com/modelcontextprotocol/go-sdk/mcp"
)

// Server is one [mcp_servers.<name>] table, with the defaults applied.
type Server struct {
	Name string
	// Command, Args, Env and Cwd start a stdio server; URL and Headers reach
	// an HTTP one (URL wins when both are set).
	Command string
	Args    []string
	Env     map[string]string
	Cwd     string
	URL     string
	Headers map[string]string
	// Timeout bounds one call, ConnectTimeout one connection attempt.
	Timeout, ConnectTimeout time.Duration
	// Include and Exclude are globs on the server's own tool names; Include
	// wins.
	Include, Exclude []string
}

const (
	firstRetry = 5 * time.Second
	lastRetry  = 5 * time.Minute
	killAfter  = 3 * time.Second
)

// Manager owns the connections to the configured servers.
type Manager struct {
	home   string
	getenv func(string) string
	srvs   []*srv // sorted by name
	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup
}

// New starts connecting to every server in the background. home holds the
// stderr log and the saved results; getenv resolves ${VAR} in the configs.
func New(home string, servers []Server, getenv func(string) string) *Manager {
	m := &Manager{home: home, getenv: getenv}
	m.ctx, m.cancel = context.WithCancel(context.Background())
	sorted := append([]Server(nil), servers...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Name < sorted[j].Name })
	for _, cfg := range sorted {
		s := &srv{m: m, cfg: cfg, ready: make(chan struct{}), why: "not connected yet"}
		m.srvs = append(m.srvs, s)
		m.wg.Add(1)
		go s.run()
	}
	return m
}

// Names lists the servers, sorted.
func (m *Manager) Names() []string {
	out := make([]string, len(m.srvs))
	for i, s := range m.srvs {
		out[i] = s.cfg.Name
	}
	return out
}

// Close stops the connections and the stdio children: SIGTERM to each
// child's process group, SIGKILL after three seconds.
func (m *Manager) Close() {
	m.cancel()
	m.wg.Wait()
	var wg sync.WaitGroup
	for _, s := range m.srvs {
		s.mu.Lock()
		c := s.cur
		s.cur = nil
		s.mu.Unlock()
		if c != nil {
			wg.Add(1)
			go func() {
				defer wg.Done()
				c.close()
			}()
		}
	}
	wg.Wait()
}

// srv is one server and its current connection.
type srv struct {
	m     *Manager
	cfg   Server
	ready chan struct{} // closed when the first connection attempt has ended
	first sync.Once

	dialMu sync.Mutex // one dial at a time; held while connecting
	mu     sync.Mutex
	cur    *conn  // nil while the server is down
	why    string // why it is down
}

// conn is one live connection and the catalog it served.
type conn struct {
	cs   *sdk.ClientSession
	cmd  *exec.Cmd // the stdio child; nil for HTTP
	done chan struct{}

	mu    sync.Mutex
	tools []entry

	closeOnce sync.Once
}

func (c *conn) catalog() []entry {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.tools
}

// close ends the connection. The child's group gets SIGTERM at once and
// SIGKILL if anything in it is still alive killAfter later.
func (c *conn) close() {
	c.closeOnce.Do(func() {
		pid := 0
		if c.cmd != nil && c.cmd.Process != nil {
			pid = c.cmd.Process.Pid
			_ = syscall.Kill(-pid, syscall.SIGTERM)
		}
		closed := make(chan struct{})
		go func() {
			_ = c.cs.Close()
			close(closed)
		}()
		deadline := time.After(killAfter)
		for {
			select {
			case <-deadline:
				if pid != 0 {
					_ = syscall.Kill(-pid, syscall.SIGKILL)
				}
				select {
				case <-closed:
				case <-time.After(2 * time.Second):
				}
				return
			case <-time.After(25 * time.Millisecond):
			}
			select {
			case <-closed:
				if pid == 0 || syscall.Kill(-pid, 0) != nil {
					return
				}
			default:
			}
		}
	})
}

// run connects, and reconnects with a growing delay whenever the server is
// down.
func (s *srv) run() {
	defer s.m.wg.Done()
	delay := firstRetry
	for {
		c, err := s.connect()
		s.first.Do(func() { close(s.ready) })
		if s.m.ctx.Err() != nil {
			return
		}
		if err == nil {
			delay = firstRetry
			if !s.watch(c) {
				return
			}
		} else {
			log.Printf("mcp: server '%s': %v", s.cfg.Name, err)
			s.mu.Lock()
			s.why = err.Error()
			s.mu.Unlock()
		}
		select {
		case <-s.m.ctx.Done():
			return
		case <-time.After(delay):
		}
		if err != nil {
			delay = min(delay*2, lastRetry)
		}
	}
}

// watch waits until the server's current connection is gone and reports
// false when the manager is closing instead.
func (s *srv) watch(c *conn) bool {
	for {
		select {
		case <-c.done:
		case <-s.m.ctx.Done():
			return false
		}
		s.mu.Lock()
		if next := s.cur; next != nil && next != c {
			s.mu.Unlock()
			c = next // a call reconnected it
			continue
		}
		if s.cur == c {
			s.cur, s.why = nil, "the connection closed"
		}
		s.mu.Unlock()
		c.close()
		return true
	}
}

// connect makes the server's connection if it has none.
func (s *srv) connect() (*conn, error) {
	s.dialMu.Lock()
	defer s.dialMu.Unlock()
	s.mu.Lock()
	cur := s.cur
	s.mu.Unlock()
	if cur != nil {
		return cur, nil
	}
	ctx, cancel := context.WithTimeout(s.m.ctx, s.cfg.ConnectTimeout)
	defer cancel()
	c, err := s.open(ctx)
	if err != nil {
		return nil, err
	}
	if s.m.ctx.Err() != nil {
		c.close()
		return nil, s.m.ctx.Err()
	}
	s.mu.Lock()
	s.cur = c
	s.mu.Unlock()
	return c, nil
}

// reconnect replaces a connection a call found broken and returns the new
// one, nil when the server stays down.
func (s *srv) reconnect(failed *conn) *conn {
	s.dialMu.Lock()
	s.mu.Lock()
	if s.cur != failed {
		cur := s.cur
		s.mu.Unlock()
		s.dialMu.Unlock()
		return cur
	}
	s.cur, s.why = nil, "the connection broke"
	s.mu.Unlock()
	s.dialMu.Unlock()
	failed.close()
	c, err := s.connect()
	if err != nil {
		s.mu.Lock()
		s.why = err.Error()
		s.mu.Unlock()
		return nil
	}
	return c
}

// state is the live connection, or why there is none.
func (s *srv) state() (*conn, string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cur, s.why
}

var placeholder = regexp.MustCompile(`\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}`)

// expand resolves ${VAR} and ${env:VAR}; a variable that is unset or empty
// is an error naming it.
func (m *Manager) expand(s string) (string, error) {
	var missing string
	out := placeholder.ReplaceAllStringFunc(s, func(p string) string {
		name := placeholder.FindStringSubmatch(p)[1]
		v := m.getenv(name)
		if v == "" && missing == "" {
			missing = name
		}
		return v
	})
	if missing != "" {
		return "", fmt.Errorf("environment variable %s is not set (needed by the config)", missing)
	}
	return out, nil
}

func (m *Manager) expandAll(in map[string]string) (map[string]string, error) {
	out := make(map[string]string, len(in))
	for k, v := range in {
		var err error
		if out[k], err = m.expand(v); err != nil {
			return nil, err
		}
	}
	return out, nil
}

// open makes one connection and reads the server's catalog.
func (s *srv) open(ctx context.Context) (*conn, error) {
	c := &conn{done: make(chan struct{})}
	var t sdk.Transport
	var logFile *os.File
	if s.cfg.URL != "" {
		endpoint, err := s.m.expand(s.cfg.URL)
		if err != nil {
			return nil, err
		}
		headers, err := s.m.expandAll(s.cfg.Headers)
		if err != nil {
			return nil, err
		}
		u, err := url.Parse(endpoint)
		if err != nil || u.Host == "" {
			return nil, fmt.Errorf("bad url %q", endpoint)
		}
		t = &sdk.StreamableClientTransport{Endpoint: endpoint,
			HTTPClient: &http.Client{Transport: &headerTransport{base: http.DefaultTransport, origin: u.Scheme + "://" + u.Host, headers: headers}}}
	} else if s.cfg.Command != "" {
		command, err := s.m.expand(s.cfg.Command)
		if err != nil {
			return nil, err
		}
		args := make([]string, len(s.cfg.Args))
		for i, a := range s.cfg.Args {
			if args[i], err = s.m.expand(a); err != nil {
				return nil, err
			}
		}
		env, err := s.m.expandAll(s.cfg.Env)
		if err != nil {
			return nil, err
		}
		cmd := exec.Command(command, args...)
		cmd.Env = childEnv(env)
		cmd.Dir = s.cfg.Cwd
		cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGTERM}
		if logFile, err = s.m.openLog(s.cfg.Name); err == nil {
			cmd.Stderr = logFile
			defer logFile.Close()
		}
		c.cmd = cmd
		t = &sdk.CommandTransport{Command: cmd, TerminateDuration: killAfter}
	} else {
		return nil, errors.New("neither command nor url is set")
	}

	client := sdk.NewClient(&sdk.Implementation{Name: "soa-d", Version: "0"}, &sdk.ClientOptions{
		Capabilities:           &sdk.ClientCapabilities{}, // no roots, sampling or elicitation
		ToolListChangedHandler: func(context.Context, *sdk.ToolListChangedRequest) { go s.refresh(c) },
	})
	cs, err := client.Connect(ctx, t, &sdk.ClientSessionOptions{ProtocolVersion: "2025-11-25"})
	if err != nil {
		killGroup(c.cmd)
		return nil, err
	}
	c.cs = cs
	go func() {
		_ = cs.Wait()
		close(c.done)
	}()
	tools, err := s.list(ctx, cs)
	if err != nil {
		c.close()
		return nil, fmt.Errorf("listing tools: %w", err)
	}
	c.tools = tools
	return c, nil
}

// killGroup kills a stdio child that never became a connection.
func killGroup(cmd *exec.Cmd) {
	if cmd != nil && cmd.Process != nil {
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}
}

// refresh re-reads a connection's catalog after tools/list_changed.
func (s *srv) refresh(c *conn) {
	ctx, cancel := context.WithTimeout(s.m.ctx, s.cfg.ConnectTimeout)
	defer cancel()
	tools, err := s.list(ctx, c.cs)
	if err != nil {
		log.Printf("mcp: server '%s': refreshing tools: %v", s.cfg.Name, err)
		return
	}
	c.mu.Lock()
	c.tools = tools
	c.mu.Unlock()
}

// list reads every page of the server's tools and applies the filters. Two
// tools whose names collide once sanitized are both left out.
func (s *srv) list(ctx context.Context, cs *sdk.ClientSession) ([]entry, error) {
	var all []entry
	count := map[string]int{}
	for t, err := range cs.Tools(ctx, nil) {
		if err != nil {
			return nil, err
		}
		if !allowed(s.cfg, t.Name) {
			continue
		}
		e := newEntry(s.cfg.Name, t.Name, t.Description, t.InputSchema)
		count[e.name]++
		all = append(all, e)
	}
	out := all[:0]
	for _, e := range all {
		if count[e.name] > 1 {
			log.Printf("mcp: server '%s': tool %q is left out: its name collides with another's once sanitized", s.cfg.Name, e.raw)
			continue
		}
		out = append(out, e)
	}
	return out, nil
}

// childEnv is what a stdio server inherits from the daemon, then the
// configured variables.
func childEnv(extra map[string]string) []string {
	var env []string
	for _, kv := range os.Environ() {
		k, _, _ := strings.Cut(kv, "=")
		switch k {
		case "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR":
			env = append(env, kv)
		default:
			if strings.HasPrefix(k, "XDG_") {
				env = append(env, kv)
			}
		}
	}
	keys := make([]string, 0, len(extra))
	for k := range extra {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		env = append(env, k+"="+extra[k])
	}
	return env
}

// openLog opens <home>/logs/mcp-stderr.log for appending and starts the
// server's section in it.
func (m *Manager) openLog(name string) (*os.File, error) {
	dir := filepath.Join(m.home, "logs")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(filepath.Join(dir, "mcp-stderr.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return nil, err
	}
	fmt.Fprintf(f, "===== [%s] starting MCP server '%s' =====\n", time.Now().Format("2006-01-02 15:04:05"), name)
	return f, nil
}

// headerTransport adds the configured headers to every request. An
// Authorization header never goes to another origin than the configured URL's
// (a redirect carries it along otherwise).
type headerTransport struct {
	base    http.RoundTripper
	origin  string
	headers map[string]string
}

func (h *headerTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	r = r.Clone(r.Context())
	same := r.URL.Scheme+"://"+r.URL.Host == h.origin
	if !same {
		r.Header.Del("Authorization")
	}
	for k, v := range h.headers {
		if !same && strings.EqualFold(k, "Authorization") {
			continue
		}
		r.Header.Set(k, v)
	}
	return h.base.RoundTrip(r)
}

// await waits for every server's first connection attempt to end, each for
// at most its connect timeout.
func (m *Manager) await(ctx context.Context) {
	for _, s := range m.srvs {
		select {
		case <-s.ready:
		case <-time.After(s.cfg.ConnectTimeout):
		case <-ctx.Done():
			return
		}
	}
}

// Match is one search hit.
type Match struct{ Name, Server, Description string }

// Down is a server that is not connected, and why.
type Down struct{ Server, Error string }

// catalog is the tools of every server that is up.
func (m *Manager) catalog() []entry {
	var out []entry
	for _, s := range m.srvs {
		if c, _ := s.state(); c != nil {
			out = append(out, c.catalog()...)
		}
	}
	return out
}

// Search ranks the catalog against the query. With no match it also names
// the servers that are down.
func (m *Manager) Search(ctx context.Context, query string, limit int) (matches []Match, total int, down []Down) {
	m.await(ctx)
	cat := m.catalog()
	for _, e := range rank(cat, query, limit) {
		d := []rune(e.desc)
		matches = append(matches, Match{Name: e.name, Server: e.server, Description: string(d[:min(len(d), 400)])})
	}
	if len(matches) == 0 {
		down = m.down()
	}
	return matches, len(cat), down
}

func (m *Manager) down() []Down {
	var out []Down
	for _, s := range m.srvs {
		if c, why := s.state(); c == nil {
			out = append(out, Down{Server: s.cfg.Name, Error: why})
		}
	}
	return out
}

// Described is a tool's description and parameter schema.
type Described struct {
	Name, Server, Description string
	Parameters                any
}

// Describe finds a tool of the catalog by its name.
func (m *Manager) Describe(ctx context.Context, name string) (Described, bool) {
	m.await(ctx)
	for _, e := range m.catalog() {
		if e.name == name {
			return Described{Name: e.name, Server: e.server, Description: e.desc, Parameters: e.schema}, true
		}
	}
	return Described{}, false
}

// Unavailable says why the tool's server is down when name belongs to a
// configured server that has no connection; "" otherwise.
func (m *Manager) Unavailable(name string) string {
	for _, s := range m.srvs {
		if c, why := s.state(); c == nil && strings.HasPrefix(name, "mcp__"+sanitize(s.cfg.Name)+"__") {
			return unavailable(s.cfg.Name, why)
		}
	}
	return ""
}

func unavailable(server, why string) string {
	return fmt.Sprintf("MCP server '%s' is unavailable (%s); try again later or continue without it", server, why)
}

// Call runs the catalog's tool name and returns the JSON result. found is
// false when there is no such tool in the catalog; reached says the server
// got the call, so its answer is outsider text.
func (m *Manager) Call(ctx context.Context, name string, args map[string]any, callID string) (result string, found, reached bool) {
	m.await(ctx)
	var s *srv
	var c *conn
	var e entry
	for _, sv := range m.srvs {
		cc, _ := sv.state()
		if cc == nil {
			continue
		}
		for _, x := range cc.catalog() {
			if x.name == name {
				s, c, e = sv, cc, x
			}
		}
	}
	if s == nil {
		return "", false, false
	}
	call := func(c *conn) (*sdk.CallToolResult, error) {
		cctx, cancel := context.WithTimeout(ctx, s.cfg.Timeout)
		defer cancel()
		res, err := c.cs.CallTool(cctx, &sdk.CallToolParams{Name: e.raw, Arguments: args})
		if err != nil && cctx.Err() != nil && ctx.Err() == nil {
			err = fmt.Errorf("MCP call timed out after %s", s.cfg.Timeout)
			return nil, &timeoutError{err}
		}
		return res, err
	}
	res, err := call(c)
	if err != nil && ctx.Err() == nil && isTransport(err) {
		if c2 := s.reconnect(c); c2 != nil {
			res, err = call(c2)
		} else {
			_, why := s.state()
			return errorJSON(unavailable(s.cfg.Name, why)), true, false
		}
	}
	if err != nil {
		return errorJSON(err.Error()), true, true
	}
	return m.format(res, callID), true, true
}

// timeoutError marks a call that ran out of time, which is not a transport
// failure.
type timeoutError struct{ error }

// isTransport says whether a call failed because the connection did, not
// because the server answered with an error.
func isTransport(err error) bool {
	var te *timeoutError
	var je *jsonrpc.Error
	return !errors.As(err, &te) && !errors.As(err, &je)
}

func errorJSON(msg string) string { return marshal(map[string]any{"error": msg}) }

// marshal is JSON without HTML escaping.
func marshal(v any) string {
	var b strings.Builder
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return `{"error":"internal error"}`
	}
	return strings.TrimSuffix(b.String(), "\n")
}

// format turns a call's result into the tool_call answer.
func (m *Manager) format(res *sdk.CallToolResult, callID string) string {
	dir := filepath.Join(m.home, "cache", "mcp")
	var parts []string
	for _, c := range res.Content {
		switch c := c.(type) {
		case *sdk.TextContent:
			parts = append(parts, c.Text)
		case *sdk.ImageContent:
			if path, err := saveImage(dir, c.MIMEType, c.Data); err == nil {
				parts = append(parts, "[image saved at "+path+"]")
			} else {
				parts = append(parts, "[image not saved: "+err.Error()+"]")
			}
		case *sdk.EmbeddedResource:
			if c.Resource != nil && c.Resource.Text != "" {
				parts = append(parts, c.Resource.Text)
			}
		}
	}
	id := callID
	if id == "" {
		id = fmt.Sprintf("%d", time.Now().UnixNano())
	}
	text := clip(dir, sanitize(id), strings.Join(parts, "\n"))
	if res.IsError {
		if text == "" {
			text = "MCP tool returned an error"
		}
		return errorJSON(text)
	}
	out := map[string]any{"result": text}
	if res.StructuredContent != nil {
		out["structuredContent"] = res.StructuredContent
	}
	return marshal(out)
}
