package lsp

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	initializeTimeout = 45 * time.Second
	shutdownTimeout   = 2 * time.Second
	killAfter         = 3 * time.Second
	maxFrame          = 256 << 20
)

// message is one JSON-RPC message of any kind.
type message struct {
	ID     json.RawMessage `json:"id,omitempty"`
	Method string          `json:"method,omitempty"`
	Params json.RawMessage `json:"params,omitempty"`
	Result json.RawMessage `json:"result,omitempty"`
	Error  *struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	} `json:"error,omitempty"`
}

// doc is what a client knows of one open file. A push or pull result is
// tagged with the document version it describes (-1: none yet).
type doc struct {
	version     int
	pushVersion int
	push        []Diagnostic
	pullVersion int
	pull        []Diagnostic
}

// latest is the newest set of diagnostics the server gave for the file; a
// push wins a tie.
func (d *doc) latest() ([]Diagnostic, bool) {
	switch {
	case d.pushVersion < 0 && d.pullVersion < 0:
		return nil, false
	case d.pullVersion > d.pushVersion:
		return d.pull, true
	}
	return d.push, true
}

// fresh is the diagnostics for version ver or newer, if the server has given
// them.
func (d *doc) fresh(ver int) ([]Diagnostic, bool) {
	if d.pushVersion < ver && d.pullVersion < ver {
		return nil, false
	}
	return d.latest()
}

// client is one language server process and the JSON-RPC conversation with
// it.
type client struct {
	id, root string
	cmd      *exec.Cmd
	stdin    io.WriteCloser
	exited   chan struct{} // closed when the process has been waited for

	ready chan struct{} // closed when initialize has ended
	err   error         // why initialize failed; read after ready is closed

	wmu sync.Mutex // one write at a time; also orders version bumps with their sends

	mu      sync.Mutex
	docs    map[string]*doc // by path
	pending map[int64]chan message
	nextID  int64
	wake    chan struct{} // closed and replaced whenever a doc changes
	pullOK  bool          // the server answers textDocument/diagnostic
	dead    bool

	busy int       // calls in flight, guarded by the service's lock
	last time.Time // when it was last used, guarded by the service's lock

	stopOnce sync.Once
}

// spawn starts the server process. onExit runs when its output ends.
func spawn(sp *spec, onExit func(*client)) (*client, error) {
	cmd := exec.Command(sp.argv[0], sp.argv[1:]...)
	cmd.Env = append(os.Environ(), sp.env...)
	cmd.Dir = sp.root
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGTERM}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	c := &client{id: sp.id, root: sp.root, cmd: cmd, stdin: stdin, exited: make(chan struct{}),
		ready: make(chan struct{}), docs: map[string]*doc{}, pending: map[int64]chan message{}, wake: make(chan struct{})}
	go c.readLoop(bufio.NewReader(stdout), onExit)
	return c, nil
}

// initialize runs the handshake. It ends by closing c.ready, with c.err set
// when it failed.
func (c *client) initialize(sp *spec) {
	ctx, cancel := context.WithTimeout(context.Background(), initializeTimeout)
	defer cancel()
	uri := fileURI(sp.root)
	params := map[string]any{
		"processId":        os.Getpid(),
		"clientInfo":       map[string]any{"name": "soa-d"},
		"rootUri":          uri,
		"workspaceFolders": []any{map[string]any{"uri": uri, "name": filepath.Base(sp.root)}},
		"capabilities": map[string]any{
			"workspace": map[string]any{"configuration": true, "workspaceFolders": true},
			"textDocument": map[string]any{
				"synchronization":    map[string]any{"didSave": true},
				"diagnostic":         map[string]any{"dynamicRegistration": false, "relatedDocumentSupport": false},
				"publishDiagnostics": map[string]any{"versionSupport": true},
			},
			"general": map[string]any{"positionEncodings": []string{"utf-16"}},
		},
	}
	if sp.init != nil {
		params["initializationOptions"] = sp.init
	}
	res, err := c.request(ctx, "initialize", params)
	if err == nil {
		var r struct {
			Capabilities struct {
				DiagnosticProvider any `json:"diagnosticProvider"`
			} `json:"capabilities"`
		}
		_ = json.Unmarshal(res, &r)
		c.mu.Lock()
		c.pullOK = r.Capabilities.DiagnosticProvider != nil && r.Capabilities.DiagnosticProvider != false
		c.mu.Unlock()
		err = c.notify("initialized", map[string]any{})
	}
	c.err = err
	close(c.ready)
}

// sync tells the server the file now has this text and returns the document
// version the text has. The first touch opens the file, later ones replace
// its text.
func (c *client) sync(path, language, text string) (int, error) {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	c.mu.Lock()
	d, open := c.docs[path]
	if open {
		d.version++
	} else {
		d = &doc{pushVersion: -1, pullVersion: -1}
		c.docs[path] = d
	}
	ver := d.version
	c.mu.Unlock()
	id := map[string]any{"uri": fileURI(path), "version": ver}
	var err error
	if open {
		err = c.notifyLocked("textDocument/didChange", map[string]any{"textDocument": id,
			"contentChanges": []any{map[string]any{"text": text}}})
	} else {
		id["languageId"], id["text"] = language, text
		err = c.notifyLocked("textDocument/didOpen", map[string]any{"textDocument": id})
	}
	if err == nil {
		err = c.notifyLocked("textDocument/didSave", map[string]any{"textDocument": map[string]any{"uri": fileURI(path)}})
	}
	return ver, err
}

// current is the newest diagnostics the server gave for a file it has open.
func (c *client) current(path string) ([]Diagnostic, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	d := c.docs[path]
	if d == nil {
		return nil, false
	}
	return d.latest()
}

// wait returns the file's diagnostics for version ver or newer, from a push
// or, when the server offers it, a pull, whichever comes first.
func (c *client) wait(ctx context.Context, path string, ver int) ([]Diagnostic, bool) {
	c.mu.Lock()
	pull := c.pullOK
	c.mu.Unlock()
	if pull {
		go c.pullDiagnostics(ctx, path, ver)
	}
	for {
		c.mu.Lock()
		var diags []Diagnostic
		ok := false
		if d := c.docs[path]; d != nil {
			diags, ok = d.fresh(ver)
		}
		wake, dead := c.wake, c.dead
		c.mu.Unlock()
		if ok {
			return diags, true
		}
		if dead {
			return nil, false
		}
		select {
		case <-wake:
		case <-ctx.Done():
			return nil, false
		}
	}
}

// pullDiagnostics asks for the file's diagnostics with textDocument/diagnostic
// and stores the answer for version ver.
func (c *client) pullDiagnostics(ctx context.Context, path string, ver int) {
	res, err := c.request(ctx, "textDocument/diagnostic", map[string]any{"textDocument": map[string]any{"uri": fileURI(path)}})
	if err != nil {
		return
	}
	var r struct {
		Kind  string       `json:"kind"`
		Items []Diagnostic `json:"items"`
	}
	if json.Unmarshal(res, &r) != nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	d := c.docs[path]
	if d == nil || ver < d.pullVersion {
		return
	}
	if r.Kind == "unchanged" {
		r.Items, _ = d.latest()
	}
	d.pull, d.pullVersion = r.Items, ver
	c.broadcastLocked()
}

func (c *client) broadcastLocked() {
	close(c.wake)
	c.wake = make(chan struct{})
}

// request sends a request and waits for its response.
func (c *client) request(ctx context.Context, method string, params any) (json.RawMessage, error) {
	c.mu.Lock()
	if c.dead {
		c.mu.Unlock()
		return nil, errors.New("the server has exited")
	}
	c.nextID++
	id := c.nextID
	ch := make(chan message, 1)
	c.pending[id] = ch
	c.mu.Unlock()
	forget := func() {
		c.mu.Lock()
		delete(c.pending, id)
		c.mu.Unlock()
	}
	if err := c.write(map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params}); err != nil {
		forget()
		return nil, err
	}
	select {
	case m, ok := <-ch:
		if !ok {
			return nil, errors.New("the server has exited")
		}
		if m.Error != nil {
			return nil, fmt.Errorf("%s: %s", method, m.Error.Message)
		}
		return m.Result, nil
	case <-ctx.Done():
		forget()
		return nil, ctx.Err()
	}
}

func (c *client) notify(method string, params any) error {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	return c.notifyLocked(method, params)
}

// notifyLocked sends a notification; the caller holds the write lock.
func (c *client) notifyLocked(method string, params any) error {
	return c.writeRaw(map[string]any{"jsonrpc": "2.0", "method": method, "params": params})
}

// write sends one message; it takes the write lock itself.
func (c *client) write(v any) error {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	return c.writeRaw(v)
}

func (c *client) writeRaw(v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(c.stdin, "Content-Length: %d\r\n\r\n%s", len(b), b)
	return err
}

// readLoop reads the server's messages until its output ends, then reaps the
// process.
func (c *client) readLoop(r *bufio.Reader, onExit func(*client)) {
	for {
		m, err := readMessage(r)
		if err != nil {
			break
		}
		c.handle(m)
	}
	c.mu.Lock()
	c.dead = true
	for id, ch := range c.pending {
		close(ch)
		delete(c.pending, id)
	}
	c.broadcastLocked()
	c.mu.Unlock()
	_ = c.cmd.Wait()
	close(c.exited)
	onExit(c)
}

// readMessage reads one Content-Length framed message. A body that is not
// JSON comes back as an empty message.
func readMessage(r *bufio.Reader) (message, error) {
	length := -1
	for {
		line, err := r.ReadString('\n')
		if err != nil {
			return message{}, err
		}
		line = strings.TrimSpace(line)
		if line == "" {
			break
		}
		if v, ok := strings.CutPrefix(strings.ToLower(line), "content-length:"); ok {
			if length, err = strconv.Atoi(strings.TrimSpace(v)); err != nil {
				return message{}, err
			}
		}
	}
	if length < 0 || length > maxFrame {
		return message{}, errors.New("bad frame")
	}
	body := make([]byte, length)
	if _, err := io.ReadFull(r, body); err != nil {
		return message{}, err
	}
	var m message
	_ = json.Unmarshal(body, &m)
	return m, nil
}

// handle routes a response to its request, stores published diagnostics and
// answers the server's own requests with a null result.
func (c *client) handle(m message) {
	switch {
	case m.Method == "" && len(m.ID) > 0:
		id, err := strconv.ParseInt(string(m.ID), 10, 64)
		if err != nil {
			return
		}
		c.mu.Lock()
		ch := c.pending[id]
		delete(c.pending, id)
		c.mu.Unlock()
		if ch != nil {
			ch <- m
		}
	case m.Method != "" && len(m.ID) > 0:
		var result any
		if m.Method == "workspace/configuration" {
			var p struct {
				Items []json.RawMessage `json:"items"`
			}
			_ = json.Unmarshal(m.Params, &p)
			result = make([]any, len(p.Items))
		}
		go func() {
			_ = c.write(map[string]any{"jsonrpc": "2.0", "id": m.ID, "result": result})
		}()
	case m.Method == "textDocument/publishDiagnostics":
		c.publish(m.Params)
	}
}

func (c *client) publish(params json.RawMessage) {
	var p struct {
		URI         string       `json:"uri"`
		Version     *int         `json:"version"`
		Diagnostics []Diagnostic `json:"diagnostics"`
	}
	if json.Unmarshal(params, &p) != nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	d := c.docs[uriPath(p.URI)]
	if d == nil {
		return
	}
	// A server that does not echo the version describes the text it has now.
	ver := d.version
	if p.Version != nil {
		ver = *p.Version
	}
	if ver < d.pushVersion {
		return
	}
	d.push, d.pushVersion = p.Diagnostics, ver
	c.broadcastLocked()
}

// stop shuts the server down: shutdown and exit, then SIGTERM to its group,
// SIGKILL if anything is left killAfter later.
func (c *client) stop() {
	c.stopOnce.Do(func() {
		select {
		case <-c.ready:
			if c.err == nil {
				ctx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
				_, _ = c.request(ctx, "shutdown", nil)
				cancel()
				_ = c.notify("exit", nil)
			}
		default:
		}
		_ = c.stdin.Close()
		pid := c.cmd.Process.Pid
		_ = syscall.Kill(-pid, syscall.SIGTERM)
		select {
		case <-c.exited:
		case <-time.After(killAfter):
			_ = syscall.Kill(-pid, syscall.SIGKILL)
			select {
			case <-c.exited:
			case <-time.After(2 * time.Second):
			}
		}
	})
}

// fileURI is the file:// URI of an absolute path.
func fileURI(path string) string {
	return (&url.URL{Scheme: "file", Path: path}).String()
}

// uriPath is the path of a file:// URI ("" for any other).
func uriPath(uri string) string {
	u, err := url.Parse(uri)
	if err != nil || u.Scheme != "file" {
		return ""
	}
	return u.Path
}
