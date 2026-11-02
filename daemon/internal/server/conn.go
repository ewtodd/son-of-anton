package server

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"net"
	"sync"
	"time"
)

// Error codes (phases/02a-server.md, "Errors").
const (
	codeParse      = -32700
	codeInvalid    = -32600
	codeNoMethod   = -32601
	codeBadParams  = -32602
	codeInternal   = -32603
	codeHelloFirst = -32001
	codeProtocol   = -32002
	codeNotDriver  = -32010
	codeNotFound   = -32011
	codeNotPending = -32012
)

const (
	maxLine    = 16 << 20
	maxBacklog = 10_000 // events a client may fall behind before it is dropped
)

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

func newID(prefix string) string {
	b := make([]byte, 6)
	_, _ = rand.Read(b)
	return prefix + hex.EncodeToString(b)
}

// eventLine is one event notification, newline-terminated and immutable.
func eventLine(params map[string]any) []byte {
	b, err := json.Marshal(map[string]any{"jsonrpc": "2.0", "method": "event", "params": params})
	if err != nil {
		b = []byte(`{"jsonrpc":"2.0","method":"event","params":{"kind":"notice","level":"error","text":"unencodable event"}}`)
	}
	return append(b, '\n')
}

// conn is one client connection. Reads and dispatch run on one goroutine
// (requests of a connection are handled in order); a second goroutine writes
// the outgoing queue, so nothing that emits an event ever waits on a socket.
type conn struct {
	srv    *Server
	nc     net.Conn
	client string
	hello  bool

	omu     sync.Mutex // guards out, writing and dead; the innermost lock
	out     [][]byte
	writing bool // writeLoop holds a batch it has not finished writing
	dead    bool
	wake    chan struct{}
	done    chan struct{}

	attached map[string]*live // touched only by the read goroutine
}

func newConn(s *Server, nc net.Conn) *conn {
	return &conn{srv: s, nc: nc, wake: make(chan struct{}, 1), done: make(chan struct{}), attached: map[string]*live{}}
}

// send queues a line for the client; it never blocks. A client more than
// maxBacklog events behind is disconnected.
func (c *conn) send(line []byte) {
	c.omu.Lock()
	if c.dead {
		c.omu.Unlock()
		return
	}
	if len(c.out) >= maxBacklog {
		c.omu.Unlock()
		c.close()
		return
	}
	c.out = append(c.out, line)
	c.omu.Unlock()
	select {
	case c.wake <- struct{}{}:
	default:
	}
}

func (c *conn) close() {
	c.omu.Lock()
	if c.dead {
		c.omu.Unlock()
		return
	}
	c.dead = true
	c.out = nil
	c.omu.Unlock()
	close(c.done)
	_ = c.nc.Close()
}

func (c *conn) writeLoop() {
	for {
		select {
		case <-c.wake:
		case <-c.done:
			return
		}
		c.omu.Lock()
		batch := c.out
		c.out, c.writing = nil, true
		c.omu.Unlock()
		buf := bytes.Join(batch, nil)
		if len(buf) > 0 {
			if _, err := c.nc.Write(buf); err != nil {
				c.close()
				return
			}
		}
		c.omu.Lock()
		c.writing = false
		c.omu.Unlock()
	}
}

// drainThenClose closes the connection once everything queued has been
// written, or after 2s: the client stopped sending (EOF, possibly only its
// write side), and the replies to what it sent are still owed.
func (c *conn) drainThenClose() {
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		c.omu.Lock()
		idle := c.dead || (len(c.out) == 0 && !c.writing)
		c.omu.Unlock()
		if idle {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	c.close()
}

func (c *conn) reply(id json.RawMessage, result any, e *rpcError) {
	resp := map[string]any{"jsonrpc": "2.0", "id": json.RawMessage("null")}
	if len(id) > 0 {
		resp["id"] = id
	}
	if e != nil {
		resp["error"] = e
	} else {
		if result == nil {
			result = map[string]any{}
		}
		resp["result"] = result
	}
	b, err := json.Marshal(resp)
	if err != nil {
		b = []byte(`{"jsonrpc":"2.0","id":null,"error":{"code":-32603,"message":"unencodable result"}}`)
	}
	c.send(append(b, '\n'))
}

// run serves the connection until it closes, then detaches it everywhere.
func (c *conn) run() {
	defer c.srv.connWG.Done()
	go c.writeLoop()
	sc := bufio.NewScanner(c.nc)
	sc.Buffer(make([]byte, 64<<10), maxLine)
	for sc.Scan() {
		c.handleLine(bytes.TrimSpace(sc.Bytes()))
	}
	c.drainThenClose()
	for _, l := range c.attached {
		l.detach(c)
		c.srv.maybeUnload(l)
	}
	c.srv.mu.Lock()
	delete(c.srv.conns, c)
	c.srv.mu.Unlock()
}

type request struct {
	ID     json.RawMessage `json:"id"`
	Method string          `json:"method"`
	Params json.RawMessage `json:"params"`
}

func (c *conn) handleLine(line []byte) {
	if len(line) == 0 {
		return
	}
	if line[0] == '[' {
		c.reply(nil, nil, &rpcError{codeInvalid, "batches are not supported"})
		return
	}
	var req request
	if err := json.Unmarshal(line, &req); err != nil {
		c.reply(nil, nil, &rpcError{codeParse, "parse error: " + err.Error()})
		return
	}
	if len(req.ID) == 0 || string(req.ID) == "null" {
		return // a notification: ignored
	}
	if req.Method == "" {
		c.reply(req.ID, nil, &rpcError{codeInvalid, "invalid request: no method"})
		return
	}
	if len(req.Params) == 0 || string(req.Params) == "null" {
		req.Params = json.RawMessage("{}")
	}
	if req.Method != "hello" && !c.hello {
		c.reply(req.ID, nil, &rpcError{codeHelloFirst, "hello must come first"})
		return
	}
	result, e := c.srv.dispatch(c, &req)
	if _, replied := result.(alreadyReplied); !replied {
		c.reply(req.ID, result, e)
	}
}
