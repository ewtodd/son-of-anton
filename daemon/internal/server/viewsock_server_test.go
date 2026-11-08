package server_test

// Phase 12: the view socket (daemon/phases/12-view-socket.md). A connection
// on it may read and watch, and nothing else; a viewer changes nothing about
// the session it watches.

import (
	"bufio"
	"context"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"net"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/server"
)

const codeReadOnly = -32014

// viewAllowed is the view connection's allowlist (session.attach only as
// viewer); viewRefused is every other method dispatch knows. Together they
// must be exactly dispatch's methods: a new method fails
// TestEveryMethodIsClassifiedForViewers until it is put in one of them.
var (
	viewAllowed = []string{"hello", "session.list", "session.history", "session.children", "session.attach",
		"session.detach", "model.list", "physics.list", "physics.watch"}
	viewRefused = []string{"session.create", "session.rename", "session.delete",
		"physics.run", "physics.stop", "physics.resume",
		"turn.send", "turn.interrupt", "queue.steer", "queue.remove", "queue.clear",
		"approval.answer", "question.answer", "session.set", "command.run", "session.handoff"}
)

// dispatchMethods reads, from methods.go's source, every method name a case
// of a switch on req.Method in dispatch names.
func dispatchMethods(t *testing.T) []string {
	t.Helper()
	f, err := parser.ParseFile(token.NewFileSet(), "methods.go", nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, d := range f.Decls {
		fn, ok := d.(*ast.FuncDecl)
		if !ok || fn.Name.Name != "dispatch" {
			continue
		}
		ast.Inspect(fn.Body, func(n ast.Node) bool {
			sw, ok := n.(*ast.SwitchStmt)
			if !ok {
				return true
			}
			if sel, ok := sw.Tag.(*ast.SelectorExpr); !ok || sel.Sel.Name != "Method" {
				return true
			}
			for _, st := range sw.Body.List {
				for _, e := range st.(*ast.CaseClause).List {
					if lit, ok := e.(*ast.BasicLit); ok && lit.Kind == token.STRING {
						s, _ := strconv.Unquote(lit.Value)
						seen[s] = true
					}
				}
			}
			return true
		})
	}
	out := make([]string, 0, len(seen))
	for m := range seen {
		out = append(out, m)
	}
	sort.Strings(out)
	return out
}

func TestEveryMethodIsClassifiedForViewers(t *testing.T) {
	methods := dispatchMethods(t)
	if len(methods) < 20 {
		t.Fatalf("read only %d methods from dispatch: %v", len(methods), methods)
	}
	for _, m := range methods {
		if !slices.Contains(viewAllowed, m) && !slices.Contains(viewRefused, m) {
			t.Errorf("method %q is in dispatch but not classified for view connections: "+
				"add it to viewAllowed (and the server's allowlist) or to viewRefused", m)
		}
	}
	for _, m := range append(append([]string{}, viewAllowed...), viewRefused...) {
		if !slices.Contains(methods, m) {
			t.Errorf("method %q is classified but dispatch has no such method", m)
		}
	}
}

// aSessionWithATurn is a session the owner made, with one finished turn and
// no client left attached.
func aSessionWithATurn(t *testing.T, h *harness, text string) string {
	t.Helper()
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, text)
	c.turn()
	c.close()
	return id
}

func TestAViewerReadsAndFollows(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "first answer"},
		fakellm.Turn{Content: "second answer"},
		fakellm.Turn{Content: "third answer"},
	)
	h := newHarness(t, fake, "")
	id := aSessionWithATurn(t, h, "first question")

	v := h.dialRawAt(h.view)
	if hello := v.ok("hello", map[string]any{"protocol": 1, "client": "test"}); hello["read_only"] != true {
		t.Fatalf("hello on the view socket = %v; it says read_only", hello)
	}
	var listed bool
	for _, s := range objs(v.ok("session.list", map[string]any{}), "sessions") {
		listed = listed || str(s, "id") == id
	}
	if !listed {
		t.Fatal("session.list on the view socket lists the session")
	}
	if ms := objs(v.ok("session.history", map[string]any{"session": id}), "messages"); roleList(ms) != "user,assistant" {
		t.Fatalf("session.history = %v", ms)
	}
	v.ok("session.children", map[string]any{"session": id})
	if ms := objs(v.ok("model.list", map[string]any{"session": id}), "models"); len(ms) == 0 {
		t.Fatal("model.list lists the models")
	}
	if runs := v.ok("physics.list", nil); runs["runs"] == nil {
		t.Fatalf("physics.list = %v", runs)
	}
	v.fails("physics.watch", map[string]any{"run": "no-such-run"}, -32011) // looked up, not refused
	att := v.attach(id, "viewer")
	if str(att, "role") != "viewer" || roleList(objs(att, "messages")) != "user,assistant" {
		t.Fatalf("attach = %v", att)
	}

	// The owner drives on; the viewer follows live.
	d := h.dial()
	d.attach(id, "driver")
	d.send(id, "second question")
	evs := v.turn()
	if text(evs, "turn.start") != "second question" || text(evs, "assistant.delta") != "second answer" {
		t.Fatalf("the viewer saw %v", kinds(evs))
	}
	v.ok("session.detach", map[string]any{"session": id})
	d.send(id, "third question")
	d.turn()
	v.none("turn.start", 200*time.Millisecond)
}

func TestAViewerCanDoNothingElse(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "an answer"})
	h := newHarness(t, fake, "")
	id := aSessionWithATurn(t, h, "a question")
	v := h.dialView()
	v.attach(id, "viewer")

	params := map[string]map[string]any{
		"session.create":  {"cwd": t.TempDir()},
		"session.rename":  {"session": id, "title": "hijacked"},
		"session.delete":  {"session": id},
		"physics.run":     {"spec": "name = \"x\""},
		"physics.stop":    {"run": "r"},
		"physics.resume":  {"run": "r"},
		"turn.send":       {"session": id, "text": "do something", "mode": "now"},
		"turn.interrupt":  {"session": id},
		"queue.steer":     {"session": id, "all": true},
		"queue.remove":    {"session": id, "items": []string{"q_x"}},
		"queue.clear":     {"session": id},
		"approval.answer": {"session": id, "approval_id": "ap_x", "choice": "once"},
		"question.answer": {"session": id, "question_id": "qu_x", "answers": []string{"yes"}},
		"session.set":     {"session": id, "mode": "yolo", "title": "hijacked"},
		"command.run":     {"session": id, "name": "stop"},
		"session.handoff": {"session": id},
	}
	for _, m := range viewRefused {
		p, ok := params[m]
		if !ok {
			t.Fatalf("no parameters for %s in this test", m)
		}
		_, e := v.call(m, p)
		if e == nil || e.Code != codeReadOnly || !strings.Contains(e.Message, "read-only") || !strings.Contains(e.Message, m) {
			t.Errorf("%s on a view connection: %+v; want %d naming the method", m, e, codeReadOnly)
		}
	}
	// Any method added later, too.
	if _, e := v.call("session.frobnicate", map[string]any{"session": id}); e == nil || e.Code != codeReadOnly {
		t.Errorf("an unknown method on a view connection: %+v", e)
	}
	_, e := v.call("session.attach", map[string]any{"session": id, "as": "driver"})
	if e == nil || e.Code != codeReadOnly || !strings.Contains(e.Message, "attach as viewer") {
		t.Fatalf("attach as driver on a view connection: %+v", e)
	}

	// Nothing changed.
	o := h.dial()
	rows := objs(o.ok("session.list", map[string]any{"kinds": []string{"interactive", "oneshot", "cron", "chat", "child"}}), "sessions")
	if len(rows) != 1 || str(rows[0], "id") != id || str(rows[0], "title") == "hijacked" || str(rows[0], "mode") == "yolo" {
		t.Fatalf("after the refused calls: %v", rows)
	}
	if fake.Count() != 1 {
		t.Fatalf("%d model requests: a view connection started a turn", fake.Count())
	}
	if att := o.attach(id, "driver"); str(att, "role") != "driver" {
		t.Fatalf("the owner drives: %v", att)
	}
}

func TestTheSocketsModes(t *testing.T) {
	h := newHarness(t, fakellm.New(t), "")
	mode := func(p string) (os.FileMode, uint32) {
		t.Helper()
		fi, err := os.Lstat(p)
		if err != nil {
			t.Fatal(err)
		}
		return fi.Mode().Perm(), fi.Sys().(*syscall.Stat_t).Gid
	}
	if m, _ := mode(filepath.Join(h.home, "run")); m != 0o700 {
		t.Errorf("<home>/run is %o, want 700", m)
	}
	if m, _ := mode(h.sock()); m != 0o600 {
		t.Errorf("the owner socket is %o, want 600", m)
	}
	if m, gid := mode(h.view); m != 0o660 || gid != uint32(os.Getgid()) {
		t.Errorf("the view socket is %o, group %d; want 660, group %d", m, gid, os.Getgid())
	}
	if hello := h.dialRaw().ok("hello", map[string]any{"protocol": 1, "client": "test"}); hello["read_only"] != false {
		t.Errorf("hello on the owner socket = %v", hello)
	}
}

func TestListenViewRefuses(t *testing.T) {
	dir, err := os.MkdirTemp("", "soav")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	sock := filepath.Join(dir, "view.sock")
	gid := os.Getgid()

	if _, err := server.ListenView(filepath.Join(dir, "missing", "view.sock"), gid); err == nil {
		t.Error("a missing directory is made by the module, not the daemon")
	}
	if err := os.Chmod(dir, 0o770); err != nil {
		t.Fatal(err)
	}
	if _, err := server.ListenView(sock, gid); err == nil || !strings.Contains(err.Error(), "writable") {
		t.Errorf("a group-writable directory: %v", err)
	}
	if err := os.Chmod(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	if foreign, ok := foreignGroup(); ok {
		if _, err := server.ListenView(sock, foreign); err == nil {
			t.Errorf("group %d: the daemon is not in it and the directory is not setgid with it", foreign)
		}
		if _, err := os.Lstat(sock); err == nil {
			t.Error("a socket that did not get its group is not left behind")
		}
	}
	ln, err := server.ListenView(sock, gid)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := server.ListenView(sock, gid); err != server.ErrRunning {
		t.Errorf("a live socket: %v, want ErrRunning", err)
	}
	_ = ln.Close()
	if err := os.WriteFile(sock, nil, 0o600); err != nil { // a stale file
		t.Fatal(err)
	}
	ln, err = server.ListenView(sock, gid)
	if err != nil {
		t.Fatalf("a stale file is replaced: %v", err)
	}
	_ = ln.Close()
}

// foreignGroup is a group this process is not in, when it is not root.
func foreignGroup() (int, bool) {
	if os.Getuid() == 0 {
		return 0, false
	}
	groups, _ := os.Getgroups()
	for _, g := range []int{0, 1, 2, 3} {
		if g != os.Getgid() && !slices.Contains(groups, g) {
			return g, true
		}
	}
	return 0, false
}

// helloAnswered says whether a fresh connection to sock gets an answer to
// hello within d.
func helloAnswered(sock string, d time.Duration) bool {
	conn, err := net.Dial("unix", sock)
	if err != nil {
		return false
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(d))
	if _, err := conn.Write([]byte(`{"jsonrpc":"2.0","id":1,"method":"hello","params":{"protocol":1,"client":"test"}}` + "\n")); err != nil {
		return false
	}
	line, err := bufio.NewReader(conn).ReadString('\n')
	return err == nil && strings.Contains(line, `"result"`)
}

func TestViewConnectionsAreCapped(t *testing.T) {
	h := newHarness(t, fakellm.New(t), "")
	var vs []*client
	for range 32 {
		vs = append(vs, h.dialView())
	}
	if helloAnswered(h.view, 2*time.Second) {
		t.Fatal("a 33rd view connection is served")
	}
	h.dial() // the owner is not affected
	vs[0].close()
	waitUntil(t, "a view connection after one closed", func() bool { return helloAnswered(h.view, time.Second) })
}

// ---- a viewer changes nothing ----

func TestAViewerLeavesAWorkerToItsChat(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "f.txt")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	var desk string
	fake := fakellm.New(t,
		fakellm.Turn{When: asWorker, Content: "desk work done"},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relaySendTo("r1", &desk, "make f.txt writable")}},
		fakellm.Turn{When: asFiona, Content: "Sent."},
		fakellm.Turn{When: asWorker, ToolCalls: []fakellm.ToolCall{terminal("c1", "chmod 777 f.txt")}},
		fakellm.Turn{When: asWorker, Content: "it is writable"},
		fakellm.Turn{When: workerNotice, Content: "f.txt is writable."},
	)
	// Single-user: a terminal driving the session would take the chat's
	// reports and approvals; a viewer must not.
	r := newChatRig(t, fake, singleUser)
	desk = deskSession(t, r, dir, "start at the desk")
	v := r.h.dialView()
	v.attach(desk, "viewer")

	r.say("make f.txt writable")
	v.awaitWhere("turn.start", func(e map[string]any) bool { return str(e, "text") == "make f.txt writable" })
	req := v.await("approval.request")
	if !strings.Contains(str(req, "subject"), "chmod 777") {
		t.Fatalf("the viewer sees the approval: %v", req)
	}
	v.fails("approval.answer", map[string]any{"session": desk, "approval_id": str(req, "approval_id"), "choice": "once"}, codeReadOnly)
	r.replyLike(sigGroup, "Worker ", "⚠️ Permission requested", "chmod 777")
	r.say("/approve")
	r.reply(sigGroup, "f.txt is writable.")
	if fi, _ := os.Stat(f); fi.Mode().Perm() != 0o777 {
		t.Fatal("the chat's approval ran the command")
	}
	if sent := toolResult(t, fake, "r1"); sent["status"] != "started" || sent["when"] != "now" || sent["took_from_terminal"] != nil {
		t.Fatalf("a send to a session with a viewer = %v", sent)
	}
	v.await("turn.end")
	if slices.Contains(v.skipped, "role") {
		t.Fatal("the viewer was sent a role event")
	}
	v.none("role", 100*time.Millisecond)
}

func TestAViewerDoesNotOpenWorkerHours(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	dir := t.TempDir()
	h := time.Now().UTC().Hour()
	start, end := (h+2)%24, (h+3)%24
	var worker string
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "send", "new_in": dir, "message": "later please"})}},
		fakellm.Turn{When: asFiona, Content: "Queued."},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relaySendTo("r2", &worker, "and this too")}},
		fakellm.Turn{When: asFiona, Content: "Queued again."},
	)
	r := newChatRig(t, fake, fmt.Sprintf("[relay]\nworker_hours = [%d, %d]\n", start, end))
	r.say("do it tonight")
	r.reply(sigGroup, "Queued.")
	worker = toolResult(t, fake, "r1")["worker"].(string)

	v := r.h.dialView()
	v.attach(worker, "viewer")
	r.say("and this too, tonight")
	r.reply(sigGroup, "Queued again.")
	want := fmt.Sprintf("queued until %02d:00", start)
	if q := toolResult(t, fake, "r2"); q["status"] != "queued" || q["when"] != want {
		t.Fatalf("a send to a watched worker outside its hours = %v; want queued, %q", q, want)
	}
	v.none("turn.start", 400*time.Millisecond)
	if requestWith(t, fake, asWorker) >= 0 {
		t.Fatal("a viewer let a worker run outside its hours")
	}
}

func TestChatsAreNotVisibleOverTheSockets(t *testing.T) {
	rel := make(chan struct{})
	released := false
	t.Cleanup(func() {
		if !released {
			close(rel)
		}
	})
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, Content: "Hello."},
		fakellm.Turn{When: asFiona, Content: "Still here.", Release: rel},
	)
	r := newChatRig(t, fake, singleUser)
	r.say("hi")
	r.reply(sigGroup, "Hello.")
	r.say("are you there?")
	r.requests(2) // the chat's second turn is running, and held
	sums, err := openStore(t, r.h.home).ListSessions(context.Background(), []string{"chat"}, 0)
	if err != nil || len(sums) != 1 {
		t.Fatalf("the chat session: %v, %v", sums, err)
	}
	chat := sums[0].Session.ID

	all := []string{"interactive", "oneshot", "cron", "chat", "child", "physics"}
	for name, c := range map[string]*client{"owner": r.h.dial(), "view": r.h.dialView()} {
		for _, s := range objs(c.ok("session.list", map[string]any{"kinds": all, "limit": 500}), "sessions") {
			if str(s, "id") == chat || str(s, "kind") == "chat" {
				t.Errorf("%s socket: session.list shows the chat: %v", name, s)
			}
		}
		c.fails("session.history", map[string]any{"session": chat}, -32011)
		c.fails("session.attach", map[string]any{"session": chat, "as": "viewer"}, -32011)
		c.fails("session.children", map[string]any{"session": chat}, -32011)
		c.fails("model.list", map[string]any{"session": chat}, -32011)
	}
	released = true
	close(rel)
	r.reply(sigGroup, "Still here.")
}

func TestAViewerWatchesAPhysicsRun(t *testing.T) {
	e := newPhys(t, false, "", wt(fakellm.Turn{Hang: true}))
	w := e.start(e.spec(), nil)
	id := str(w, "id")
	waitUntil(t, "the worker's request", func() bool { return len(e.requests(workerMark)) == 1 })

	v := e.h.dialView()
	var listed bool
	for _, run := range objs(v.ok("physics.list", nil), "runs") {
		listed = listed || str(run, "id") == id
	}
	if !listed {
		t.Fatal("physics.list on the view socket lists the run")
	}
	if watch := v.ok("physics.watch", map[string]any{"run": id}); str(watch, "state") == "" || watch["lines"] == nil {
		t.Fatalf("physics.watch = %v", watch)
	}
	v.fails("physics.stop", map[string]any{"run": id}, codeReadOnly)
	v.fails("physics.resume", map[string]any{"run": id}, codeReadOnly)
	v.fails("physics.run", map[string]any{"spec": e.spec()}, codeReadOnly)
	ws := str(w, "worker_session")
	v.fails("session.attach", map[string]any{"session": ws, "as": "driver"}, codeReadOnly)
	if att := v.attach(ws, "viewer"); str(att, "role") != "viewer" {
		t.Fatalf("attach = %v", att)
	}
	if run := e.run(id); str(run, "state") != "working" {
		t.Fatalf("the run after the viewer: %v", run)
	}
	e.c.ok("physics.stop", map[string]any{"run": id})
}
