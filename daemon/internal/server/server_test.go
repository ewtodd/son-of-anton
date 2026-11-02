package server_test

// Phase 2a contract: the daemon's socket protocol (daemon/phases/02a-server.md).

import (
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/server"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

const interrupted = "[response interrupted]"

// Error codes (phases/02a-server.md, "Errors").
const (
	codeParse        = -32700
	codeNoMethod     = -32601
	codeBadParams    = -32602
	codeHelloFirst   = -32001
	codeProtocol     = -32002
	codeNotDriver    = -32010
	codeNotFound     = -32011
	codeNotPending   = -32012
	unknownSessionID = "20260101_000000_000000"
)

func TestHelloComesFirst(t *testing.T) {
	h := newHarness(t, fakellm.New(t), "")
	c := h.dialRaw()
	c.fails("session.list", map[string]any{}, codeHelloFirst)
	c.fails("hello", map[string]any{"protocol": 99, "client": "test"}, codeProtocol)
	r := c.ok("hello", map[string]any{"protocol": 1, "client": "test"})
	if r["protocol"] != 1.0 || r["version"] != "test" || r["pid"] == nil {
		t.Fatalf("hello = %v", r)
	}
	c.fails("no.such.method", map[string]any{}, codeNoMethod)
	c.fails("session.attach", map[string]any{"session": unknownSessionID, "as": "driver"}, codeNotFound)
	c.fails("session.create", map[string]any{"cwd": "relative/dir"}, codeBadParams)
	c.fails("session.create", map[string]any{"cwd": filepath.Join(h.home, "missing")}, codeBadParams)
	c.raw("this is not json\n")
	select {
	case f := <-c.orphans:
		if f.Error == nil || f.Error.Code != codeParse {
			t.Fatalf("a line that is not JSON gets a parse error with a null id, got %+v", f)
		}
	case <-time.After(waitFor):
		t.Fatal("no parse-error response")
	}
	// The connection survives a bad line.
	c.ok("session.list", map[string]any{})
}

// A client that sends its requests and then closes its side (a script piping
// into socat, say) still gets every reply.
func TestRepliesSurviveAHalfClose(t *testing.T) {
	h := newHarness(t, fakellm.New(t), "")
	conn, err := net.Dial("unix", h.sock())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	reqs := `{"jsonrpc":"2.0","id":1,"method":"hello","params":{"protocol":1,"client":"t"}}` + "\n" +
		`{"jsonrpc":"2.0","id":2,"method":"session.create","params":{"cwd":"` + t.TempDir() + `"}}` + "\n" +
		`{"jsonrpc":"2.0","id":3,"method":"session.list","params":{}}` + "\n"
	if _, err := conn.Write([]byte(reqs)); err != nil {
		t.Fatal(err)
	}
	if err := conn.(*net.UnixConn).CloseWrite(); err != nil {
		t.Fatal(err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(waitFor))
	b, _ := io.ReadAll(conn)
	if lines := strings.Split(strings.TrimSpace(string(b)), "\n"); len(lines) != 3 {
		t.Fatalf("got %d replies after a half-close, want 3:\n%s", len(lines), b)
	}
}

func TestTheSocketIsPrivateAndExclusive(t *testing.T) {
	h := newHarness(t, fakellm.New(t), "")
	if fi, err := os.Stat(filepath.Join(h.home, "run")); err != nil || fi.Mode().Perm() != 0o700 {
		t.Fatalf("run dir: %v, %v; want mode 0700", fi.Mode(), err)
	}
	fi, err := os.Lstat(h.sock())
	if err != nil || fi.Mode()&os.ModeSocket == 0 || fi.Mode().Perm() != 0o600 {
		t.Fatalf("socket: %v, %v; want a socket with mode 0600", fi.Mode(), err)
	}
	if _, err := server.Listen(h.home); !errors.Is(err, server.ErrRunning) {
		t.Fatalf("a second Listen while serving = %v, want server.ErrRunning", err)
	}
	if err := h.stop(); err != nil {
		t.Fatalf("Serve returned %v", err)
	}
	if _, err := os.Lstat(h.sock()); !os.IsNotExist(err) {
		t.Fatalf("Serve must remove its socket on shutdown: %v", err)
	}
	if err := os.WriteFile(h.sock(), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	ln, err := server.Listen(h.home)
	if err != nil {
		t.Fatalf("a stale file at the socket path is replaced: %v", err)
	}
	_ = ln.Close()
}

func TestATurnStreamsAndCommits(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "Hello there", ChunkRunes: 3, PromptTokens: 50, CompletionTokens: 5})
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	att := c.attach(id, "driver")
	if str(att, "role") != "driver" || len(objs(att, "messages")) != 0 || obj(att, "session")["kind"] != "interactive" {
		t.Fatalf("attach = %v", att)
	}
	if r := c.send(id, "hi"); r["started"] != true {
		t.Fatalf("turn.send on an idle session = %v, want started", r)
	}
	evs := c.turn()
	if evs[0]["kind"] != "turn.start" || str(evs[0], "text") != "hi" || str(evs[0], "session") != id {
		t.Fatalf("first event = %v; a turn opens with turn.start carrying the user's text and the session id", evs[0])
	}
	if got := text(evs, "assistant.delta"); got != "Hello there" {
		t.Fatalf("deltas = %q", got)
	}
	ms := committed(evs)
	if roleList(ms) != "user,assistant" || str(ms[0], "content") != "hi" || str(ms[1], "content") != "Hello there" {
		t.Fatalf("committed rows = %v", ms)
	}
	if s := seqList(ms); s[0] != 1 || s[1] != 2 {
		t.Fatalf("seqs = %v", s)
	}
	lastDelta := -1
	for i, e := range evs {
		if e["kind"] == "assistant.delta" {
			lastDelta = i
		}
	}
	if indexOf(evs, "message") < lastDelta {
		t.Fatalf("rows are announced once committed, after the deltas: %v", kinds(evs))
	}
	end := evs[len(evs)-1]
	if str(end, "status") != "done" || obj(end, "usage")["prompt_tokens"] != 50.0 || end["api_calls"] != 1.0 {
		t.Fatalf("turn.end = %v", end)
	}
	upd := c.awaitWhere("session.updated", func(e map[string]any) bool { return str(obj(e, "state"), "status") == "idle" })
	s := obj(upd, "state")
	if s["context_tokens"] != 55.0 || obj(s, "usage")["input_tokens"] != 50.0 || obj(s, "usage")["api_calls"] != 1.0 {
		t.Fatalf("session after the turn = %v", s)
	}
}

func TestToolEventsAndCommittedRows(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("c1", "echo tooled")}},
		fakellm.Turn{Content: "done"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "run it")
	evs := c.turn()
	start, end := of(evs, "tool.start"), of(evs, "tool.end")
	if len(start) != 1 || len(end) != 1 {
		t.Fatalf("events = %v", kinds(evs))
	}
	if str(start[0], "call_id") != "c1" || str(start[0], "name") != "terminal" ||
		!strings.Contains(str(start[0], "arguments"), "echo tooled") || !strings.Contains(str(start[0], "summary"), "echo tooled") {
		t.Fatalf("tool.start = %v; it carries the call id, the name, the raw arguments and a one-line summary", start[0])
	}
	if str(end[0], "call_id") != "c1" || !strings.Contains(str(end[0], "result"), "tooled") || end[0]["is_error"] != false || end[0]["duration_ms"] == nil {
		t.Fatalf("tool.end = %v", end[0])
	}
	ms := committed(evs)
	if roleList(ms) != "user,assistant,tool,assistant" {
		t.Fatalf("committed rows = %v", roleList(ms))
	}
	calls := objs(ms[1], "tool_calls")
	if len(calls) != 1 || str(calls[0], "id") != "c1" || str(calls[0], "name") != "terminal" || !strings.Contains(str(calls[0], "arguments"), "echo tooled") {
		t.Fatalf("the assistant row's tool calls = %v; flattened to {id, name, arguments}", ms[1]["tool_calls"])
	}
	if str(ms[2], "tool_call_id") != "c1" || str(ms[2], "tool_name") != "terminal" || !strings.Contains(str(ms[2], "content"), "tooled") {
		t.Fatalf("tool row = %v", ms[2])
	}
	toolEnd, toolRow := indexOf(evs, "tool.end"), -1
	for i, e := range evs {
		if e["kind"] == "message" && str(obj(e, "message"), "role") == "tool" {
			toolRow = i
		}
	}
	if toolRow < toolEnd {
		t.Fatalf("the tool row is committed after tool.end: %v", kinds(evs))
	}
}

func TestToolErrorsAreFlagged(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "read_file", `{"path":"/definitely/not/here.txt"}`)}},
		fakellm.Turn{Content: "no file"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "read")
	end := of(c.turn(), "tool.end")
	if len(end) != 1 || end[0]["is_error"] != true {
		t.Fatalf("a result with an error field is flagged: %v", end)
	}
}

func TestQueuedSendsRunInOrder(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("c1", "sleep 0.5; echo one")}},
		fakellm.Turn{Content: "a done"},
		fakellm.Turn{Content: "b done"},
		fakellm.Turn{Content: "c done"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "a")
	c.await("tool.start")
	for _, q := range []string{"b", "c"} {
		r := c.ok("turn.send", map[string]any{"session": id, "text": q, "mode": "queue"})
		if r["started"] != false || str(r, "queued") == "" {
			t.Fatalf("turn.send while running = %v, want queued with an item id", r)
		}
	}
	evs := c.turns(3)
	var starts []string
	for _, e := range of(evs, "turn.start") {
		starts = append(starts, str(e, "text"))
	}
	if strings.Join(starts, ",") != "b,c" {
		t.Fatalf("queued turns started %v, want b then c", starts)
	}
	var sizes []int
	for _, e := range of(evs, "queue.updated") {
		sizes = append(sizes, len(objs(e, "items")))
	}
	if len(sizes) < 4 || sizes[0] != 1 || sizes[1] != 2 || sizes[len(sizes)-1] != 0 {
		t.Fatalf("queue.updated sizes = %v; want 1, 2, ..., 0", sizes)
	}
	if fake.Count() != 4 {
		t.Fatalf("requests = %d", fake.Count())
	}
	last := wire(t, fake, 3)
	if roleList(last) != "system,user,assistant,tool,assistant,user,assistant,user" || str(last[len(last)-1], "content") != "c" {
		t.Fatalf("the last request is %s ending %q", roleList(last), str(last[len(last)-1], "content"))
	}
	if str(wire(t, fake, 2)[5], "content") != "b" {
		t.Fatal("b runs before c")
	}
}

func TestTheQueueSurvivesARestart(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Hang: true}, fakellm.Turn{Content: "b done"})
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "a")
	c.await("turn.start")
	waitUntil(t, "the first request", func() bool { return fake.Count() == 1 })
	c.ok("turn.send", map[string]any{"session": id, "text": "b", "mode": "queue"})
	h.restart()

	c2 := h.dial()
	att := c2.attach(id, "driver")
	ms := objs(att, "messages")
	if roleList(ms) != "user,assistant" || str(ms[1], "content") != interrupted {
		t.Fatalf("after a restart the interrupted turn is recorded: %v", ms)
	}
	if items := objs(att, "queue"); len(items) != 1 || str(items[0], "text") != "b" || str(items[0], "kind") != "user" {
		t.Fatalf("queue after restart = %v", att["queue"])
	}
	c2.none("turn.start", 500*time.Millisecond)
	if r := c2.ok("queue.steer", map[string]any{"session": id}); r["started"] != true {
		t.Fatalf("queue.steer on an idle session = %v, want started", r)
	}
	evs := c2.turn()
	if starts := of(evs, "turn.start"); len(starts) != 1 || str(starts[0], "text") != "b" || str(evs[len(evs)-1], "status") != "done" {
		t.Fatalf("events = %v", evs)
	}
	req := wire(t, fake, 1)
	if roleList(req) != "system,user,assistant,user" || str(req[3], "content") != "b" {
		t.Fatalf("request = %s", roleList(req))
	}
}

func TestInterruptRecordsTheTurn(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "partial answer here", ChunkRunes: 8, StallAfterFirst: true},
		fakellm.Turn{Content: "ok"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	if r := c.ok("turn.interrupt", map[string]any{"session": id}); r["interrupted"] != false {
		t.Fatalf("interrupting an idle session = %v", r)
	}
	c.send(id, "q")
	c.await("assistant.delta")
	if r := c.ok("turn.interrupt", map[string]any{"session": id}); r["interrupted"] != true {
		t.Fatalf("turn.interrupt = %v", r)
	}
	evs := c.turn()
	if str(evs[len(evs)-1], "status") != "interrupted" {
		t.Fatalf("turn.end = %v", evs[len(evs)-1])
	}
	ms := committed(evs)
	if roleList(ms) != "user,assistant" || str(ms[1], "content") != "partial" {
		t.Fatalf("an interrupted turn keeps the user's message and what streamed: %v", ms)
	}
	c.send(id, "next")
	if starts := of(c.turn(), "turn.start"); len(starts) != 1 || str(starts[0], "text") != "next" {
		t.Fatal("the next turn runs")
	}
	req := wire(t, fake, 1)
	if roleList(req) != "system,user,assistant,user" || str(req[2], "content") != "partial" {
		t.Fatalf("request after an interrupt = %s %v", roleList(req), req)
	}
}

func TestInterruptStopsARunningTool(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("c1", "sleep 30")}})
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "wait")
	c.await("tool.start")
	start := time.Now()
	c.ok("turn.interrupt", map[string]any{"session": id})
	evs := c.turn()
	if time.Since(start) > 5*time.Second {
		t.Fatal("an interrupt must stop the running command")
	}
	ms := committed(evs)
	if roleList(ms) != "user,assistant,tool,assistant" || str(ms[3], "content") != interrupted {
		t.Fatalf("committed = %v", ms)
	}
}

func TestSendNowSteersAfterTheRunningTool(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("c1", "sleep 0.5; echo finished")}},
		fakellm.Turn{Content: "steered ok"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "go")
	c.await("tool.start")
	if r := c.ok("turn.send", map[string]any{"session": id, "text": "change of plan", "mode": "now"}); r["steered"] != true {
		t.Fatalf("turn.send now while running = %v, want steered", r)
	}
	evs := c.turns(2)
	ends := of(evs, "turn.end")
	if str(ends[0], "status") != "steered" || str(ends[1], "status") != "done" {
		t.Fatalf("turn.end statuses = %v, %v", ends[0], ends[1])
	}
	if starts := of(evs, "turn.start"); len(starts) != 1 || str(starts[0], "text") != "change of plan" {
		t.Fatalf("the steer starts the next turn at once: %v", starts)
	}
	for _, m := range committed(evs) {
		if str(m, "role") == "tool" && !strings.Contains(str(m, "content"), "finished") {
			t.Fatalf("a steer lets the running command finish; its result = %q", str(m, "content"))
		}
	}
	req := wire(t, fake, 1)
	if roleList(req) != "system,user,assistant,tool,assistant,user" || str(req[4], "content") != interrupted || str(req[5], "content") != "change of plan" {
		t.Fatalf("request after a steer = %s %v", roleList(req), req)
	}
}

func TestSendNowDuringTheModelRequestKeepsWhatStreamed(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "first draft text", ChunkRunes: 6, StallAfterFirst: true},
		fakellm.Turn{Content: "ok"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "go")
	c.await("assistant.delta")
	c.ok("turn.send", map[string]any{"session": id, "text": "no, shorter", "mode": "now"})
	c.turns(2)
	req := wire(t, fake, 1)
	if roleList(req) != "system,user,assistant,user" || str(req[2], "content") != "first" || str(req[3], "content") != "no, shorter" {
		t.Fatalf("request after a steer mid-stream = %s %v", roleList(req), req)
	}
}

func TestQueueSteerAndRemove(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("c1", "sleep 0.5; echo x")}},
		fakellm.Turn{Content: "after all"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "a")
	c.await("tool.start")
	ids := map[string]string{}
	for _, q := range []string{"b", "c", "d"} {
		ids[q] = str(c.ok("turn.send", map[string]any{"session": id, "text": q}), "queued")
	}
	r := c.ok("queue.remove", map[string]any{"session": id, "items": []string{ids["c"]}})
	if items := objs(r, "queue"); len(items) != 2 || str(items[0], "text") != "b" || str(items[1], "text") != "d" {
		t.Fatalf("queue.remove = %v", r)
	}
	if r := c.ok("queue.steer", map[string]any{"session": id, "all": true}); r["steered"] != true {
		t.Fatalf("queue.steer all while running = %v", r)
	}
	evs := c.turns(2)
	if starts := of(evs, "turn.start"); len(starts) != 1 || str(starts[0], "text") != "b\n\nd" {
		t.Fatalf("steering onto every queued item joins them: %v", starts)
	}
	if ups := of(evs, "queue.updated"); len(ups) == 0 || len(objs(ups[len(ups)-1], "items")) != 0 {
		t.Fatalf("the queue ends empty: %v", ups)
	}
	c.none("turn.start", 500*time.Millisecond)
	if fake.Count() != 2 || str(wire(t, fake, 1)[5], "content") != "b\n\nd" {
		t.Fatalf("requests = %d", fake.Count())
	}
}

func TestQueueClear(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Hang: true})
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "a")
	c.await("turn.start")
	c.send(id, "e")
	c.send(id, "f")
	if r := c.ok("queue.clear", map[string]any{"session": id}); len(objs(r, "queue")) != 0 {
		t.Fatalf("queue.clear = %v", r)
	}
	c.ok("turn.interrupt", map[string]any{"session": id})
	c.turn()
	c.none("turn.start", 500*time.Millisecond)
}

func TestApprovalsGoToTheDriver(t *testing.T) {
	cwd := t.TempDir()
	f := filepath.Join(cwd, "f.txt")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("c1", "chmod 777 f.txt")}},
		fakellm.Turn{Content: "done"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("c2", "chmod 666 f.txt")}},
		fakellm.Turn{Content: "denied ok"},
	)
	h := newHarness(t, fake, "")
	d, v := h.dial(), h.dial()
	id := d.create(cwd)
	d.attach(id, "driver")
	if att := v.attach(id, "viewer"); str(att, "role") != "viewer" {
		t.Fatalf("viewer attach = %v", att)
	}
	d.send(id, "go")
	req := d.await("approval.request")
	if str(req, "approval_kind") != "command" || !strings.Contains(str(req, "subject"), "chmod 777") || len(strs(req, "reasons")) == 0 {
		t.Fatalf("approval.request = %v", req)
	}
	choices := strings.Join(strs(req, "choices"), ",")
	if !strings.Contains(choices, "once") || !strings.Contains(choices, "deny") {
		t.Fatalf("choices = %q", choices)
	}
	if vr := v.await("approval.request"); str(vr, "approval_id") != str(req, "approval_id") {
		t.Fatal("viewers see the same request")
	}
	answer := map[string]any{"session": id, "approval_id": str(req, "approval_id"), "choice": "once"}
	v.fails("approval.answer", answer, codeNotDriver)
	d.ok("approval.answer", answer)
	d.fails("approval.answer", answer, codeNotPending)
	if r := v.await("approval.resolved"); str(r, "choice") != "once" {
		t.Fatalf("approval.resolved = %v", r)
	}
	d.turn()
	if fi, _ := os.Stat(f); fi.Mode().Perm() != 0o777 {
		t.Fatal("an approved command runs")
	}

	d.send(id, "again")
	req = d.await("approval.request")
	d.ok("approval.answer", map[string]any{"session": id, "approval_id": str(req, "approval_id"), "choice": "deny"})
	evs := d.turn()
	for _, m := range committed(evs) {
		if str(m, "role") == "tool" && !strings.Contains(str(m, "content"), `"blocked"`) {
			t.Fatalf("a denied command's result = %q", str(m, "content"))
		}
	}
	if fi, _ := os.Stat(f); fi.Mode().Perm() != 0o777 {
		t.Fatal("a denied command must not run")
	}
}

func TestAPendingApprovalReplaysOnAttach(t *testing.T) {
	cwd := t.TempDir()
	if err := os.WriteFile(filepath.Join(cwd, "f.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("c1", "chmod 777 f.txt")}},
		fakellm.Turn{Content: "done"},
	)
	h := newHarness(t, fake, "")
	d1 := h.dial()
	id := d1.create(cwd)
	d1.attach(id, "driver")
	d1.send(id, "go")
	first := d1.await("approval.request")
	d1.close()

	d2 := h.dial()
	d2.attach(id, "driver")
	again := d2.await("approval.request")
	if str(again, "approval_id") != str(first, "approval_id") {
		t.Fatalf("the pending request replays on attach: %v vs %v", again, first)
	}
	d2.ok("approval.answer", map[string]any{"session": id, "approval_id": str(again, "approval_id"), "choice": "once"})
	if evs := d2.turn(); str(evs[len(evs)-1], "status") != "done" {
		t.Fatalf("turn.end = %v", evs[len(evs)-1])
	}
}

func TestClarifyGoesToTheDriver(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "clarify", `{"question":"Which?","choices":["a","b"]}`)}},
		fakellm.Turn{Content: "ok"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "ask me")
	q := c.await("question.request")
	if str(q, "text") != "Which?" || strings.Join(strs(q, "choices"), ",") != "a,b" || q["multi_select"] != false {
		t.Fatalf("question.request = %v", q)
	}
	c.ok("question.answer", map[string]any{"session": id, "question_id": str(q, "question_id"), "answers": []string{"b"}})
	c.fails("question.answer", map[string]any{"session": id, "question_id": str(q, "question_id"), "answers": []string{"a"}}, codeNotPending)
	for _, m := range committed(c.turn()) {
		if str(m, "role") == "tool" && !strings.Contains(str(m, "content"), `"user_response":"b"`) {
			t.Fatalf("clarify result = %q", str(m, "content"))
		}
	}
}

func TestANewDriverDemotesTheOld(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "from b"})
	h := newHarness(t, fake, "")
	a, b := h.dial(), h.dial()
	id := a.create(t.TempDir())
	a.attach(id, "driver")
	if r := b.attach(id, "driver"); str(r, "role") != "driver" {
		t.Fatalf("attach = %v", r)
	}
	if e := a.await("role"); str(e, "as") != "viewer" {
		t.Fatalf("role event = %v", e)
	}
	a.fails("turn.send", map[string]any{"session": id, "text": "x"}, codeNotDriver)
	a.fails("turn.interrupt", map[string]any{"session": id}, codeNotDriver)
	b.send(id, "y")
	b.turn()
	if e := a.await("turn.end"); str(e, "status") != "done" {
		t.Fatal("a viewer sees the turn")
	}
}

func TestReconnectGetsOnlyWhatItMissed(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "r1"}, fakellm.Turn{Content: "r2"})
	h := newHarness(t, fake, "")
	a, b := h.dial(), h.dial()
	id := a.create(t.TempDir())
	a.attach(id, "driver")
	a.send(id, "1")
	a.turn()
	a.ok("session.detach", map[string]any{"session": id})
	b.attach(id, "driver")
	b.send(id, "2")
	b.turn()

	r := a.ok("session.attach", map[string]any{"session": id, "as": "viewer", "after_seq": 2})
	if s := seqList(objs(r, "messages")); len(s) != 2 || s[0] != 3 || s[1] != 4 || r["gap"] != false {
		t.Fatalf("after_seq 2 = %v gap %v", s, r["gap"])
	}
	c := h.dial()
	r = c.ok("session.attach", map[string]any{"session": id, "as": "viewer", "limit": 2})
	if s := seqList(objs(r, "messages")); len(s) != 2 || s[0] != 3 || r["more_before"] != true {
		t.Fatalf("limit 2 = %v more_before %v; the newest page comes first", s, r["more_before"])
	}
	r = c.ok("session.history", map[string]any{"session": id, "before_seq": 3, "limit": 10})
	if s := seqList(objs(r, "messages")); len(s) != 2 || s[0] != 1 || s[1] != 2 || r["more_before"] != false {
		t.Fatalf("history before 3 = %v more_before %v", s, r["more_before"])
	}
	r = c.ok("session.attach", map[string]any{"session": id, "as": "viewer", "after_seq": 0, "limit": 1})
	if s := seqList(objs(r, "messages")); r["gap"] != true || len(s) != 1 || s[0] != 4 {
		t.Fatalf("more missed than the limit = %v gap %v; want gap and the newest page", s, r["gap"])
	}
}

func TestAttachingMidTurnReplaysTheLiveTail(t *testing.T) {
	release := make(chan struct{})
	var once sync.Once
	t.Cleanup(func() { once.Do(func() { close(release) }) })
	fake := fakellm.New(t, fakellm.Turn{Content: "Hello world", ChunkRunes: 6, Release: release})
	h := newHarness(t, fake, "")
	a := h.dial()
	id := a.create(t.TempDir())
	a.attach(id, "driver")
	a.send(id, "hi")
	a.await("assistant.delta")

	b := h.dial()
	att := b.attach(id, "viewer")
	if len(objs(att, "messages")) != 0 || str(obj(att, "session"), "status") != "running" {
		t.Fatalf("attach mid-turn = %v", att)
	}
	if e := b.await("turn.start"); str(e, "text") != "hi" {
		t.Fatalf("the live tail starts with turn.start: %v", e)
	}
	if e := b.await("assistant.delta"); str(e, "text") != "Hello " {
		t.Fatalf("replayed delta = %v", e)
	}
	once.Do(func() { close(release) })
	evs := b.turn()
	if got := "Hello " + text(evs, "assistant.delta"); got != "Hello world" {
		t.Fatalf("live deltas after the replay = %q", got)
	}
	if roleList(committed(evs)) != "user,assistant" {
		t.Fatalf("committed = %v", committed(evs))
	}
}

func TestSessionListModelsAndSettings(t *testing.T) {
	cwd := t.TempDir()
	f := filepath.Join(cwd, "f.txt")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t,
		fakellm.Turn{Content: "a"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("c1", "chmod 777 f.txt")}},
		fakellm.Turn{Content: "b"},
	)
	fake.SetModels("test-model", "live-model")
	h := newHarness(t, fake, "")
	c := h.dial()
	s1 := c.create(cwd)
	c.attach(s1, "driver")
	c.send(s1, "first question")
	c.turn()
	s2 := c.create(t.TempDir())

	list := objs(c.ok("session.list", map[string]any{}), "sessions")
	if len(list) != 2 || str(list[0], "id") != s2 || str(list[1], "id") != s1 ||
		str(list[1], "preview") != "first question" || str(list[1], "kind") != "interactive" || str(list[1], "status") != "idle" {
		t.Fatalf("session.list = %v", list)
	}

	ml := c.ok("model.list", map[string]any{"session": s1})
	names := map[string][]string{}
	for _, m := range objs(ml, "models") {
		names[str(m, "name")] = strs(m, "reasoning_efforts")
	}
	if _, ok := names["live-model"]; !ok || len(names) != 3 || strings.Join(names["test-model"], ",") != "low,high" || str(ml, "current") != "test-model" {
		t.Fatalf("model.list = %v; configured routes plus the provider's /v1/models", ml)
	}

	r := c.ok("session.set", map[string]any{"session": s1, "model": "other-model", "reasoning_effort": "low", "mode": "yolo"})
	if s := obj(r, "session"); str(s, "model") != "other-model" || str(s, "reasoning_effort") != "low" || str(s, "mode") != "yolo" {
		t.Fatalf("session.set = %v", r)
	}
	c.awaitWhere("session.updated", func(e map[string]any) bool { return str(obj(e, "state"), "model") == "other-model" })
	c.fails("session.set", map[string]any{"session": s1, "mode": "bogus"}, codeBadParams)

	c.send(s1, "again")
	if evs := c.turn(); indexOf(evs, "approval.request") >= 0 {
		t.Fatal("in yolo mode nothing asks")
	}
	if fi, _ := os.Stat(f); fi.Mode().Perm() != 0o777 {
		t.Fatal("the command ran")
	}
	req := fake.Request(1)
	if req["model"] != "other-model" || req["reasoning_effort"] != "low" {
		t.Fatalf("the next request uses the new settings: model %v effort %v", req["model"], req["reasoning_effort"])
	}
	if string(fake.Messages(1)[0]) != string(fake.Messages(0)[0]) {
		t.Fatal("the system prompt bytes never change")
	}
	att := h.dial().attach(s1, "viewer")
	if s := obj(att, "session"); str(s, "model") != "other-model" || str(s, "mode") != "yolo" {
		t.Fatalf("settings persist with the session: %v", s)
	}
}

func TestABackgroundNoticeStartsATurn(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "terminal", `{"command":"sleep 0.3; echo bgdone","background":true,"notify_on_complete":true}`)}},
		fakellm.Turn{Content: "started it"},
		fakellm.Turn{Content: "saw it"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "run it")
	evs := c.turns(2)
	starts := of(evs, "turn.start")
	if len(starts) != 2 || !strings.Contains(str(starts[1], "text"), "[Background process") || !strings.Contains(str(starts[1], "text"), "bgdone") {
		t.Fatalf("a completion notice starts a turn: %v", starts)
	}
	last := wire(t, fake, 2)
	if !strings.Contains(str(last[len(last)-1], "content"), "bgdone") {
		t.Fatalf("the notice is the turn's user message: %v", last[len(last)-1])
	}
}

func TestStopKillsBackgroundProcesses(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "terminal", `{"command":"sleep 30","background":true}`)}},
		fakellm.Turn{Content: "started"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "start")
	pid := 0
	for _, m := range committed(c.turn()) {
		if str(m, "role") == "tool" {
			var res map[string]any
			_ = json.Unmarshal([]byte(str(m, "content")), &res)
			p, _ := res["pid"].(float64)
			pid = int(p)
		}
	}
	if pid == 0 || syscall.Kill(pid, 0) != nil {
		t.Fatal("the background process should be running")
	}
	upd := c.awaitWhere("session.updated", func(e map[string]any) bool { return str(obj(e, "state"), "status") == "idle" })
	if obj(upd, "state")["background_processes"] != 1.0 {
		t.Fatalf("session = %v; it counts running background processes", obj(upd, "state"))
	}
	if r := c.ok("command.run", map[string]any{"session": id, "name": "stop"}); r["killed"] != 1.0 {
		t.Fatalf("stop = %v", r)
	}
	waitUntil(t, "the process to die", func() bool { return syscall.Kill(pid, 0) != nil })
	c.fails("command.run", map[string]any{"session": id, "name": "nope"}, codeBadParams)
}

func TestCommitIsATemplatedTurn(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "committed"})
	h := newHarness(t, fake, "[git]\nauthor_name = \"Bot\"\nauthor_email = \"bot@example.com\"\n")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	if r := c.ok("command.run", map[string]any{"session": id, "name": "commit", "args": "only the docs"}); r["started"] != true {
		t.Fatalf("commit on an idle session = %v", r)
	}
	start := of(c.turn(), "turn.start")[0]
	got := str(start, "text")
	for _, want := range []string{"uncommitted", "Additional instructions from the user: only the docs", `--author="Bot <bot@example.com>"`} {
		if !strings.Contains(got, want) {
			t.Fatalf("commit prompt %q lacks %q", got, want)
		}
	}
	last := wire(t, fake, 0)
	if str(last[len(last)-1], "content") != got {
		t.Fatal("the template is the turn's user message")
	}
}

func TestShutdownRecordsTheTurnAndCleansUp(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "terminal", `{"command":"sleep 30","background":true}`)}},
		fakellm.Turn{Content: "ok"},
		fakellm.Turn{Hang: true},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "bg")
	pid := 0
	for _, m := range committed(c.turn()) {
		if str(m, "role") == "tool" {
			var res map[string]any
			_ = json.Unmarshal([]byte(str(m, "content")), &res)
			p, _ := res["pid"].(float64)
			pid = int(p)
		}
	}
	c.send(id, "hang")
	c.await("turn.start")
	waitUntil(t, "the hanging request", func() bool { return fake.Count() == 3 })
	if err := h.stop(); err != nil {
		t.Fatalf("Serve returned %v", err)
	}
	waitUntil(t, "the background process to die", func() bool { return pid != 0 && syscall.Kill(pid, 0) != nil })
	if _, err := os.Lstat(h.sock()); !os.IsNotExist(err) {
		t.Fatal("the socket is removed")
	}
	st, err := store.Open(filepath.Join(h.home, "soa-d.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	ms, _ := st.Messages(t.Context(), id)
	if n := len(ms); n < 2 || ms[n-2].Content != "hang" || ms[n-1].Content != interrupted {
		t.Fatalf("a turn cut by shutdown is recorded as interrupted: %+v", ms)
	}
}
