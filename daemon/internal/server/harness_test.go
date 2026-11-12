package server_test

// Test harness: a real server on a real unix socket in a temp home, against
// internal/fakellm, driven by a small JSON-RPC client.

import (
	"bufio"
	"context"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/server"
)

const waitFor = 10 * time.Second

type harness struct {
	t      *testing.T
	home   string
	view   string // the view socket (phases/12-view-socket.md), outside the home
	fake   *fakellm.Server
	cancel context.CancelFunc
	done   chan error

	nmu    sync.Mutex
	nudges []string // the desktop nudges, "title|body"
}

// nudged returns the desktop nudges so far ("title|body").
func (h *harness) nudged() []string {
	h.nmu.Lock()
	defer h.nmu.Unlock()
	return append([]string(nil), h.nudges...)
}

// writeHome writes a config.toml pointing at url, with two configured model
// routes; extra is appended verbatim.
func writeHome(t *testing.T, home, url, extra string) {
	t.Helper()
	cfg := "[model]\ndefault = \"test-model\"\nprovider = \"custom\"\n\n" +
		"[custom_providers.custom]\nbase_url = \"" + url + "\"\nkey_env = \"SOA_D_TEST_KEY\"\n\n" +
		"[custom_providers.custom.models.\"test-model\"]\nreasoning_efforts = [\"low\", \"high\"]\n\n" +
		"[custom_providers.custom.models.\"other-model\"]\n\n" + extra
	if err := os.WriteFile(filepath.Join(home, "config.toml"), []byte(cfg), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(home, ".env"), []byte("SOA_D_TEST_KEY=s3cret\n"), 0o600); err != nil {
		t.Fatal(err)
	}
}

// newHarness starts a server in a fresh home. The home is made with a short
// path: a unix socket path must fit in 107 bytes.
func newHarness(t *testing.T, fake *fakellm.Server, extraConfig string) *harness {
	t.Helper()
	home, err := os.MkdirTemp("", "soad")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(home) })
	t.Setenv("SOA_D_TEST_KEY", "")
	t.Setenv("SON_OF_ANTON_HOME", "")
	writeHome(t, home, fake.URL, extraConfig)
	viewDir, err := os.MkdirTemp("", "soav")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(viewDir) })
	h := &harness{t: t, home: home, view: filepath.Join(viewDir, "view.sock"), fake: fake}
	h.start()
	t.Cleanup(func() { _ = h.stop() })
	return h
}

func (h *harness) start() {
	h.t.Helper()
	ln, err := server.Listen(h.home)
	if err != nil {
		h.t.Fatalf("Listen: %v", err)
	}
	// A newHarness daemon also serves a view socket, as an instance with a
	// viewerGroup does; its group is the test's own.
	var views []net.Listener
	if h.view != "" {
		vln, err := server.ListenView(h.view, os.Getgid())
		if err != nil {
			_ = ln.Close()
			h.t.Fatalf("ListenView: %v", err)
		}
		views = append(views, vln)
	}
	srv, err := server.New(server.Options{Home: h.home, Version: "test", NotifySend: func(title, body string) {
		h.nmu.Lock()
		h.nudges = append(h.nudges, title+"|"+body)
		h.nmu.Unlock()
	}})
	if err != nil {
		_ = ln.Close()
		for _, v := range views {
			_ = v.Close()
		}
		h.t.Fatalf("New: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	h.cancel = cancel
	h.done = make(chan error, 1)
	go func() { h.done <- srv.Serve(ctx, ln, views...) }()
}

// stop shuts the server down and waits for Serve to return. Calling it again
// does nothing.
func (h *harness) stop() error {
	if h.cancel == nil {
		return nil
	}
	h.cancel()
	h.cancel = nil
	select {
	case err := <-h.done:
		return err
	case <-time.After(15 * time.Second):
		h.t.Error("Serve did not return within 15s of shutdown")
		return nil
	}
}

func (h *harness) restart() {
	h.t.Helper()
	if err := h.stop(); err != nil {
		h.t.Fatalf("Serve returned %v", err)
	}
	h.start()
}

func (h *harness) sock() string { return filepath.Join(h.home, "run", "soa-d.sock") }

// ---- client ----

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type frame struct {
	ID     *int            `json:"id"`
	Method string          `json:"method"`
	Params map[string]any  `json:"params"`
	Result json.RawMessage `json:"result"`
	Error  *rpcError       `json:"error"`
}

type client struct {
	t       *testing.T
	conn    net.Conn
	mu      sync.Mutex
	nextID  int
	waiting map[int]chan frame
	events  chan map[string]any
	orphans chan frame // responses with a null id (parse errors)
	skipped []string
}

// dialRaw connects without the hello handshake.
func (h *harness) dialRaw() *client { h.t.Helper(); return h.dialRawAt(h.sock()) }

// dialView connects to the view socket and says hello.
func (h *harness) dialView() *client {
	h.t.Helper()
	c := h.dialRawAt(h.view)
	c.ok("hello", map[string]any{"protocol": 1, "client": "test"})
	return c
}

func (h *harness) dialRawAt(sock string) *client {
	h.t.Helper()
	conn, err := net.Dial("unix", sock)
	if err != nil {
		h.t.Fatalf("dial: %v", err)
	}
	c := &client{
		t: h.t, conn: conn, waiting: map[int]chan frame{},
		events: make(chan map[string]any, 100000), orphans: make(chan frame, 16),
	}
	h.t.Cleanup(func() { _ = conn.Close() })
	go c.readLoop()
	return c
}

// dial connects and says hello.
func (h *harness) dial() *client {
	h.t.Helper()
	c := h.dialRaw()
	c.ok("hello", map[string]any{"protocol": 1, "client": "test"})
	return c
}

func (c *client) close() { _ = c.conn.Close() }

func (c *client) readLoop() {
	sc := bufio.NewScanner(c.conn)
	sc.Buffer(make([]byte, 1<<20), 64<<20)
	for sc.Scan() {
		var f frame
		if err := json.Unmarshal(sc.Bytes(), &f); err != nil {
			c.t.Errorf("the daemon sent a line that is not JSON: %q", sc.Bytes())
			continue
		}
		switch {
		case f.ID == nil && f.Method == "event":
			c.events <- f.Params
		case f.ID == nil:
			c.orphans <- f
		default:
			c.mu.Lock()
			ch := c.waiting[*f.ID]
			delete(c.waiting, *f.ID)
			c.mu.Unlock()
			if ch != nil {
				ch <- f
			}
		}
	}
	close(c.events)
}

func (c *client) raw(line string) {
	c.t.Helper()
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, err := c.conn.Write([]byte(line)); err != nil {
		c.t.Fatal(err)
	}
}

func (c *client) call(method string, params any) (map[string]any, *rpcError) {
	c.t.Helper()
	c.mu.Lock()
	c.nextID++
	id := c.nextID
	ch := make(chan frame, 1)
	c.waiting[id] = ch
	b, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params})
	_, err := c.conn.Write(append(b, '\n'))
	c.mu.Unlock()
	if err != nil {
		c.t.Fatalf("%s: write: %v", method, err)
	}
	select {
	case f := <-ch:
		if f.Error != nil {
			return nil, f.Error
		}
		m := map[string]any{}
		if len(f.Result) > 0 && string(f.Result) != "null" {
			if err := json.Unmarshal(f.Result, &m); err != nil {
				c.t.Fatalf("%s: result is not an object: %s", method, f.Result)
			}
		}
		return m, nil
	case <-time.After(waitFor):
		c.t.Fatalf("%s: no response within %v", method, waitFor)
		return nil, nil
	}
}

func (c *client) ok(method string, params any) map[string]any {
	c.t.Helper()
	r, e := c.call(method, params)
	if e != nil {
		c.t.Fatalf("%s(%v): error %d %s", method, params, e.Code, e.Message)
	}
	return r
}

func (c *client) fails(method string, params any, code int) {
	c.t.Helper()
	_, e := c.call(method, params)
	if e == nil || e.Code != code {
		c.t.Fatalf("%s(%v): got error %+v, want code %d", method, params, e, code)
	}
}

// next returns the next event of any kind.
func (c *client) next() map[string]any {
	c.t.Helper()
	select {
	case e, ok := <-c.events:
		if !ok {
			c.t.Fatal("the connection closed")
		}
		return e
	case <-time.After(waitFor):
		c.t.Fatalf("no event within %v (skipped so far: %v)", waitFor, c.skipped)
		return nil
	}
}

// await returns the next event of this kind, discarding the ones before it.
func (c *client) await(kind string) map[string]any {
	c.t.Helper()
	return c.awaitWhere(kind, func(map[string]any) bool { return true })
}

func (c *client) awaitWhere(kind string, pred func(map[string]any) bool) map[string]any {
	c.t.Helper()
	for {
		e := c.next()
		if e["kind"] == kind && pred(e) {
			return e
		}
		c.skipped = append(c.skipped, str(e, "kind"))
	}
}

// turns collects every event up to and including the n-th turn.end.
func (c *client) turns(n int) []map[string]any {
	c.t.Helper()
	var evs []map[string]any
	for n > 0 {
		e := c.next()
		evs = append(evs, e)
		if e["kind"] == "turn.end" {
			n--
		}
	}
	return evs
}

func (c *client) turn() []map[string]any { c.t.Helper(); return c.turns(1) }

// none asserts that no event of this kind arrives within d.
func (c *client) none(kind string, d time.Duration) {
	c.t.Helper()
	deadline := time.After(d)
	for {
		select {
		case e, ok := <-c.events:
			if !ok {
				return
			}
			if e["kind"] == kind {
				c.t.Fatalf("unexpected %s event: %v", kind, e)
			}
		case <-deadline:
			return
		}
	}
}

// ---- session helpers ----

func (c *client) create(cwd string) string {
	c.t.Helper()
	return str(obj(c.ok("session.create", map[string]any{"cwd": cwd}), "session"), "id")
}

func (c *client) attach(id, as string) map[string]any {
	c.t.Helper()
	return c.ok("session.attach", map[string]any{"session": id, "as": as})
}

func (c *client) send(id, text string) map[string]any {
	c.t.Helper()
	return c.ok("turn.send", map[string]any{"session": id, "text": text})
}

// ---- JSON helpers ----

func str(m map[string]any, k string) string {
	s, _ := m[k].(string)
	return s
}

func obj(m map[string]any, k string) map[string]any {
	o, _ := m[k].(map[string]any)
	return o
}

func objs(m map[string]any, k string) []map[string]any {
	raw, _ := m[k].([]any)
	out := make([]map[string]any, 0, len(raw))
	for _, r := range raw {
		if o, ok := r.(map[string]any); ok {
			out = append(out, o)
		}
	}
	return out
}

func strs(m map[string]any, k string) []string {
	raw, _ := m[k].([]any)
	out := make([]string, 0, len(raw))
	for _, r := range raw {
		s, _ := r.(string)
		out = append(out, s)
	}
	return out
}

func kinds(evs []map[string]any) []string {
	out := make([]string, len(evs))
	for i, e := range evs {
		out[i] = str(e, "kind")
	}
	return out
}

// of returns the events of one kind, in order.
func of(evs []map[string]any, kind string) []map[string]any {
	var out []map[string]any
	for _, e := range evs {
		if e["kind"] == kind {
			out = append(out, e)
		}
	}
	return out
}

func indexOf(evs []map[string]any, kind string) int {
	for i, e := range evs {
		if e["kind"] == kind {
			return i
		}
	}
	return -1
}

// text concatenates the text of every event of this kind.
func text(evs []map[string]any, kind string) string {
	var b strings.Builder
	for _, e := range of(evs, kind) {
		b.WriteString(str(e, "text"))
	}
	return b.String()
}

// committed returns the message objects of the message events, in order.
func committed(evs []map[string]any) []map[string]any {
	var out []map[string]any
	for _, e := range of(evs, "message") {
		out = append(out, obj(e, "message"))
	}
	return out
}

func roleList(ms []map[string]any) string {
	rs := make([]string, len(ms))
	for i, m := range ms {
		rs[i] = str(m, "role")
	}
	return strings.Join(rs, ",")
}

func seqList(ms []map[string]any) []float64 {
	out := make([]float64, len(ms))
	for i, m := range ms {
		out[i], _ = m["seq"].(float64)
	}
	return out
}

// sentTimePrefix is the form a stamped user message's API copy starts with.
var sentTimePrefix = regexp.MustCompile(`^\[[A-Za-z]{3} \d{4}-\d{2}-\d{2} \d{2}:\d{2} [^]]+\] `)

// withoutSentTime drops a leading send-time prefix for tests that compare the
// clean user text. Callers that need the exact wire bytes use rawWire.
func withoutSentTime(s string) string { return sentTimePrefix.ReplaceAllString(s, "") }

// wire decodes request i's messages into role/content pairs, with each user
// message's send-time prefix removed.
func wire(t *testing.T, fake *fakellm.Server, i int) []map[string]any {
	t.Helper()
	out := rawWire(t, fake, i)
	for _, m := range out {
		if str(m, "role") == "user" {
			m["content"] = withoutSentTime(str(m, "content"))
		}
	}
	return out
}

// rawWire decodes request i's messages without stripping the send time.
func rawWire(t *testing.T, fake *fakellm.Server, i int) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, raw := range fake.Messages(i) {
		var m map[string]any
		if err := json.Unmarshal(raw, &m); err != nil {
			t.Fatal(err)
		}
		out = append(out, m)
	}
	return out
}

func call(id, name, args string) fakellm.ToolCall {
	return fakellm.ToolCall{ID: id, Name: name, Arguments: args}
}

func bashCall(id, command string) fakellm.ToolCall {
	b, _ := json.Marshal(map[string]any{"command": command})
	return call(id, "Bash", string(b))
}

// waitUntil polls cond until it holds or the wait runs out.
func waitUntil(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(waitFor)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}
