// Package client speaks the daemon's socket protocol (daemon/phases/02a-server.md):
// newline-delimited JSON-RPC 2.0 with events as notifications.
package client

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/config"
)

const maxLine = 16 << 20

// Error is a JSON-RPC error answered by the daemon.
type Error struct {
	Code    int
	Message string
}

func (e *Error) Error() string { return e.Message }

// Event is one event notification; Params is its whole params object.
type Event struct {
	Session, Kind string
	Params        map[string]any
}

var errGone = errors.New("the connection to soa-d is gone")

type response struct {
	result json.RawMessage
	err    error
}

// Client is one connection to the daemon.
type Client struct {
	conn  net.Conn
	hello map[string]any

	wmu sync.Mutex // serializes writes

	mu      sync.Mutex // pending, nextID, gone
	nextID  int64
	pending map[int64]chan response
	gone    bool

	qmu    sync.Mutex // the event queue
	queue  []Event
	ended  bool
	wake   chan struct{}
	events chan Event

	done      chan struct{}
	closed    chan struct{}
	closeOnce sync.Once
}

// SocketPath is the daemon's socket under home.
func SocketPath(home string) string { return filepath.Join(home, "run", "soa-d.sock") }

func dialSocket(home string) (net.Conn, error) {
	return net.DialTimeout("unix", SocketPath(home), 5*time.Second)
}

// Dial connects to the daemon under home and says hello as name.
func Dial(home, name string) (*Client, error) {
	conn, err := dialSocket(home)
	if err != nil {
		return nil, err
	}
	return handshake(conn, name)
}

func handshake(conn net.Conn, name string) (*Client, error) {
	c := &Client{
		conn:    conn,
		pending: map[int64]chan response{},
		wake:    make(chan struct{}, 1),
		events:  make(chan Event),
		done:    make(chan struct{}),
		closed:  make(chan struct{}),
	}
	go c.read()
	go c.pump()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := c.Call(ctx, "hello", map[string]any{"protocol": 1, "client": name}, &c.hello); err != nil {
		_ = c.Close()
		return nil, err
	}
	return c, nil
}

// Connect is Dial, starting the daemon when nothing listens.
func Connect(home, name string) (*Client, error) {
	conn, err := dialSocket(home)
	if err == nil {
		return handshake(conn, name)
	}
	if served, unit := servedByUnit(home); served {
		return nil, fmt.Errorf("soa-d is not running for %s, and %s serves it: see systemctl status %s", home, unit, unit)
	}
	if err := startDaemon(home); err != nil {
		return nil, err
	}
	deadline := time.Now().Add(10 * time.Second)
	for {
		conn, derr := dialSocket(home)
		if derr == nil {
			return handshake(conn, name)
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("soa-d did not start: %w", derr)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

// servedByUnit reports whether config.toml says a system unit serves home
// ([daemon] autostart = false), and that unit's name. A config that does not
// load leaves the decision to the daemon, which reports it in its log.
func servedByUnit(home string) (bool, string) {
	cfg, err := config.Load(home)
	if err != nil || cfg.Daemon.Autostart == nil || *cfg.Daemon.Autostart {
		return false, ""
	}
	if cfg.Daemon.Instance != "" {
		return true, "son-of-anton-" + cfg.Daemon.Instance
	}
	return true, "its son-of-anton unit"
}

// startDaemon runs `<bin> serve --home <home>` in its own session, its output
// appended to <home>/logs/soa-d.log, and does not wait for it.
func startDaemon(home string) error {
	bin := os.Getenv("SOA_D_BIN")
	if bin == "" {
		var err error
		if bin, err = os.Executable(); err != nil {
			return err
		}
	}
	logs := filepath.Join(home, "logs")
	if err := os.MkdirAll(logs, 0o700); err != nil {
		return err
	}
	logf, err := os.OpenFile(filepath.Join(logs, "soa-d.log"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer logf.Close()
	cmd := exec.Command(bin, "serve", "--home", home)
	cmd.Stdin = nil // /dev/null
	cmd.Stdout, cmd.Stderr = logf, logf
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("starting soa-d: %w", err)
	}
	return cmd.Process.Release()
}

// Hello is the hello result.
func (c *Client) Hello() map[string]any { return c.hello }

// Events delivers every event in order; it closes after Done.
func (c *Client) Events() <-chan Event { return c.events }

// Done is closed once the connection is gone.
func (c *Client) Done() <-chan struct{} { return c.done }

// Close closes the connection; it may be called more than once.
func (c *Client) Close() error {
	var err error
	c.closeOnce.Do(func() {
		close(c.closed)
		err = c.conn.Close()
	})
	return err
}

// Call makes one request and decodes its result into result (nil: discard).
func (c *Client) Call(ctx context.Context, method string, params, result any) error {
	if params == nil {
		params = map[string]any{}
	}
	ch := make(chan response, 1)
	c.mu.Lock()
	if c.gone {
		c.mu.Unlock()
		return errGone
	}
	c.nextID++
	id := c.nextID
	c.pending[id] = ch
	c.mu.Unlock()
	forget := func() {
		c.mu.Lock()
		delete(c.pending, id)
		c.mu.Unlock()
	}
	line, err := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params})
	if err != nil {
		forget()
		return err
	}
	c.wmu.Lock()
	_ = c.conn.SetWriteDeadline(time.Now().Add(30 * time.Second))
	_, err = c.conn.Write(append(line, '\n'))
	c.wmu.Unlock()
	if err != nil {
		forget()
		return errGone
	}
	select {
	case r := <-ch:
		if r.err != nil {
			return r.err
		}
		if result == nil || len(r.result) == 0 {
			return nil
		}
		return json.Unmarshal(r.result, result)
	case <-ctx.Done():
		forget()
		return ctx.Err()
	case <-c.done:
		forget()
		select { // an answer that arrived just before the end still counts
		case r := <-ch:
			if r.err != nil {
				return r.err
			}
			if result == nil || len(r.result) == 0 {
				return nil
			}
			return json.Unmarshal(r.result, result)
		default:
		}
		return errGone
	}
}

// read dispatches every incoming line until the connection ends.
func (c *Client) read() {
	sc := bufio.NewScanner(c.conn)
	sc.Buffer(make([]byte, 64<<10), maxLine)
	for sc.Scan() {
		var m struct {
			ID     *int64          `json:"id"`
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
			Result json.RawMessage `json:"result"`
			Error  *struct {
				Code    int    `json:"code"`
				Message string `json:"message"`
			} `json:"error"`
		}
		if json.Unmarshal(sc.Bytes(), &m) != nil {
			continue
		}
		switch {
		case m.Method == "event":
			var p map[string]any
			if json.Unmarshal(m.Params, &p) != nil {
				continue
			}
			ev := Event{Params: p}
			ev.Session, _ = p["session"].(string)
			ev.Kind, _ = p["kind"].(string)
			c.qmu.Lock()
			c.queue = append(c.queue, ev)
			c.qmu.Unlock()
			select {
			case c.wake <- struct{}{}:
			default:
			}
		case m.ID != nil:
			c.mu.Lock()
			ch := c.pending[*m.ID]
			delete(c.pending, *m.ID)
			c.mu.Unlock()
			if ch == nil {
				continue
			}
			if m.Error != nil {
				ch <- response{err: &Error{Code: m.Error.Code, Message: m.Error.Message}}
			} else {
				ch <- response{result: m.Result}
			}
		}
	}
	c.mu.Lock()
	c.gone = true
	for id, ch := range c.pending {
		ch <- response{err: errGone}
		delete(c.pending, id)
	}
	c.mu.Unlock()
	_ = c.conn.Close()
	close(c.done)
	c.qmu.Lock()
	c.ended = true
	c.qmu.Unlock()
	select {
	case c.wake <- struct{}{}:
	default:
	}
}

// pump moves queued events to Events() without ever holding up the reader.
func (c *Client) pump() {
	defer close(c.events)
	for {
		c.qmu.Lock()
		if len(c.queue) > 0 {
			ev := c.queue[0]
			c.queue[0] = Event{}
			c.queue = c.queue[1:]
			c.qmu.Unlock()
			select {
			case c.events <- ev:
			case <-c.closed:
				return
			}
			continue
		}
		ended := c.ended
		c.qmu.Unlock()
		if ended {
			return
		}
		select {
		case <-c.wake:
		case <-c.closed:
			return
		}
	}
}
