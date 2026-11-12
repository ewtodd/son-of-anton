package server_test

// A chat and the user's terminal sessions: a terminal session and a worker are
// the same thing, so on a single-user instance relay lists it and a send
// continues it in place, with its own prompt, history and directory.

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

const singleUser = "[gateway]\nsingle_user = true\n"

// workerNotice marks the chat's request carrying a turn's report. A reply's
// own words won't do when the session goes straight on to a queued message:
// its next request carries them too, and might take the chat's scripted turn.
const workerNotice = "[worker "

// relaySendTo sends to the session *session names when the turn is served.
func relaySendTo(id string, session *string, message string) fakellm.ToolCall {
	return fakellm.ToolCall{ID: id, Name: "relay", ArgumentsFunc: func() string {
		b, _ := json.Marshal(map[string]any{"action": "send", "session": *session, "message": message})
		return string(b)
	}}
}

// deskSession is a terminal session in dir with one finished turn and no
// terminal left attached.
func deskSession(t *testing.T, r *chatRig, dir, first string) string {
	t.Helper()
	c := r.h.dial()
	id := c.create(dir)
	c.attach(id, "driver")
	c.send(id, first)
	c.turn()
	c.close()
	return id
}

func TestAChatContinuesATerminalSession(t *testing.T) {
	dir := t.TempDir()
	var desk string
	fake := fakellm.New(t,
		fakellm.Turn{When: asWorker, Content: "desk work done"},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "list"})}},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relaySendTo("r2", &desk, "carry on from your phone")}},
		fakellm.Turn{When: asFiona, Content: "Sent."},
		fakellm.Turn{When: asWorker, Content: "continued in place"},
		fakellm.Turn{When: "continued in place", Content: "It carried on."},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relaySendTo("r3", &desk, "one more thing")}},
		fakellm.Turn{When: asFiona, Content: "Sent again."},
		fakellm.Turn{When: asWorker, Content: "and that too"},
		fakellm.Turn{When: "and that too", Content: "That is done too."},
	)
	r := newChatRig(t, fake, singleUser)
	desk = deskSession(t, r, dir, "start at the desk")

	r.say("pick up my session")
	r.reply(sigGroup, "Sent.")
	r.reply(sigGroup, "It carried on.")

	var entry map[string]any
	ws, _ := toolResult(t, fake, "r1")["workers"].([]any)
	for _, w := range ws {
		if m := w.(map[string]any); str(m, "worker") == desk {
			entry = m
		}
	}
	if entry == nil || str(entry, "cwd") != dir || str(entry, "state") != "idle" {
		t.Fatalf("relay list = %v", ws)
	}
	if sent := toolResult(t, fake, "r2"); sent["success"] != true || sent["worker"] != desk || sent["status"] != "started" {
		t.Fatalf("relay send = %v", sent)
	}
	st := openStore(t, r.h.home)
	s, err := st.Session(context.Background(), desk)
	if err != nil || s.Kind != "interactive" || s.Cwd != dir {
		t.Fatalf("the session = %+v, %v", s, err)
	}
	if all, err := st.ListSessions(context.Background(), []string{"interactive"}, 0); err != nil || len(all) != 1 {
		t.Fatalf("no second session is started: %v, %v", all, err)
	}
	cont := -1
	for i := range fake.Count() {
		if strings.HasPrefix(systemPrompt(t, fake, i), asWorker) && lastUser(t, fake, i) == "carry on from your phone" {
			cont = i
		}
	}
	if cont < 0 {
		t.Fatal("the session gets the message")
	}
	if systemPrompt(t, fake, cont) != systemPrompt(t, fake, 0) {
		t.Fatal("the session keeps its own prompt, byte for byte")
	}
	body := string(fake.Bodies()[cont])
	if !strings.Contains(body, "start at the desk") || !strings.Contains(body, "desk work done") {
		t.Fatal("the session keeps its own history")
	}

	// Nothing keeps a finished session loaded, and a later send still reports.
	r.say("one more thing please")
	r.reply(sigGroup, "That is done too.")
}

func TestASharedInstanceKeepsTerminalSessionsFromItsChats(t *testing.T) {
	dir := t.TempDir()
	var desk string
	fake := fakellm.New(t,
		fakellm.Turn{When: asWorker, Content: "desk work done"},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "list"}), relaySendTo("r2", &desk, "hijack")}},
		fakellm.Turn{When: asFiona, Content: "Nothing there."},
	)
	r := newChatRig(t, fake, "")
	desk = deskSession(t, r, dir, "start at the desk")
	r.say("pick up a session")
	r.reply(sigGroup, "Nothing there.")
	if ws, _ := toolResult(t, fake, "r1")["workers"].([]any); len(ws) != 0 {
		t.Fatalf("a chat sees a terminal session on a shared instance: %v", ws)
	}
	if res := toolResult(t, fake, "r2"); res["success"] != false || res["error"] != "no worker "+desk+" for this chat" {
		t.Fatalf("a chat drove a terminal session on a shared instance: %v", res)
	}
}

func TestRelayListLeavesOutEmptyTerminalSessions(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "list"})}},
		fakellm.Turn{When: asFiona, Content: "Nothing yet."},
	)
	r := newChatRig(t, fake, singleUser)
	c := r.h.dial()
	c.create(t.TempDir())
	c.close()
	r.say("what is there?")
	r.reply(sigGroup, "Nothing yet.")
	if ws, _ := toolResult(t, fake, "r1")["workers"].([]any); len(ws) != 0 {
		t.Fatalf("relay list = %v", ws)
	}
}

// The reason for all this: the user closed the TUI, the turn ran on, and the
// chat had no way to see it or reach it.
func TestATerminalSessionLeftRunningTakesTheChatsMessageNext(t *testing.T) {
	dir := t.TempDir()
	var desk string
	rel := make(chan struct{})
	fake := fakellm.New(t,
		fakellm.Turn{When: asWorker, Content: "long job done", Release: rel},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "list"}), relaySendTo("r2", &desk, "then do this")}},
		fakellm.Turn{When: asFiona, Content: "It will do that next."},
		fakellm.Turn{When: workerNotice, Content: "The long job is done."},
		fakellm.Turn{When: asWorker, Content: "did that too"},
		fakellm.Turn{When: "did that too", Content: "That is done as well."},
	)
	r := newChatRig(t, fake, singleUser)
	c := r.h.dial()
	desk = c.create(dir)
	c.attach(desk, "driver")
	c.send(desk, "start the long job")
	c.await("turn.start")
	c.close()

	r.say("what is my session doing?")
	r.reply(sigGroup, "It will do that next.")
	ws, _ := toolResult(t, fake, "r1")["workers"].([]any)
	if len(ws) != 1 || str(ws[0].(map[string]any), "state") != "running" {
		t.Fatalf("relay list = %v", ws)
	}
	if q := toolResult(t, fake, "r2"); q["status"] != "queued" || q["when"] != "after the current turn" {
		t.Fatalf("a send to a running session = %v", q)
	}
	close(rel)
	r.reply(sigGroup, "The long job is done.")
	r.reply(sigGroup, "That is done as well.")
}

func TestAChatTakesOverAWaitingApproval(t *testing.T) {
	// Nobody may be there at all, or a terminal may have been left open.
	for _, name := range []string{"terminal closed", "terminal left open"} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			f := filepath.Join(dir, "f.txt")
			if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
				t.Fatal(err)
			}
			var desk string
			fake := fakellm.New(t,
				fakellm.Turn{When: asWorker, ToolCalls: []fakellm.ToolCall{bashCall("c1", "chmod 777 f.txt")}},
				fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relaySendTo("r1", &desk, "and when that is done, stop")}},
				fakellm.Turn{When: asFiona, Content: "Queued."},
				fakellm.Turn{When: asWorker, Content: "it is writable"},
				fakellm.Turn{When: workerNotice, Content: "f.txt is writable."},
				fakellm.Turn{When: asWorker, Content: "stopped"},
				fakellm.Turn{When: "stopped", Content: "It stopped."},
			)
			r := newChatRig(t, fake, singleUser)
			c := r.h.dial()
			desk = c.create(dir)
			c.attach(desk, "driver")
			c.send(desk, "make f.txt writable")
			c.await("approval.request")
			if name == "terminal closed" {
				c.close()
			}

			r.say("my session is waiting, take it")
			r.replyLike(sigGroup, "Worker ", "⚠️ Permission requested", "chmod 777")
			r.say("/approve")
			r.reply(sigGroup, "f.txt is writable.")
			if fi, _ := os.Stat(f); fi.Mode().Perm() != 0o777 {
				t.Fatal("the chat's approval ran the command")
			}
			r.reply(sigGroup, "It stopped.")
		})
	}
}

// One person on a single-user instance: if they are writing from the chat,
// they are not at the terminal they left open.
func TestASendTakesASessionFromAnOpenTerminal(t *testing.T) {
	dir := t.TempDir()
	var desk string
	fake := fakellm.New(t,
		fakellm.Turn{When: asWorker, Content: "desk work done"},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "list"})}},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relaySendTo("r2", &desk, "from the chat")}},
		fakellm.Turn{When: asFiona, Content: "Taken."},
		fakellm.Turn{When: asWorker, Content: "done from the phone"},
		fakellm.Turn{When: workerNotice, Content: "It finished."},
		fakellm.Turn{When: asWorker, Content: "at the desk again"},
	)
	r := newChatRig(t, fake, singleUser)
	c := r.h.dial()
	desk = c.create(dir)
	c.attach(desk, "driver")
	c.send(desk, "start at the desk")
	c.turn()

	r.say("pick up my session")
	if role := c.await("role"); str(role, "as") != "viewer" || str(role, "by") != "chat" {
		t.Fatalf("the terminal hears = %v", role)
	}
	r.reply(sigGroup, "Taken.")
	r.reply(sigGroup, "It finished.")
	ws, _ := toolResult(t, fake, "r1")["workers"].([]any)
	if len(ws) != 1 || str(ws[0].(map[string]any), "state") != "picked up" {
		t.Fatalf("relay list = %v", ws)
	}
	if sent := toolResult(t, fake, "r2"); sent["status"] != "started" || sent["took_from_terminal"] != true {
		t.Fatalf("a send to an open terminal = %v", sent)
	}

	// A terminal takes it back, and what it runs is not reported.
	back := r.h.dial()
	back.attach(desk, "driver")
	back.send(desk, "back at the desk")
	back.turn()
	time.Sleep(200 * time.Millisecond)
	if fake.Count() != 7 {
		t.Fatalf("%d requests: the chat heard about the terminal's turn", fake.Count())
	}
}

func TestASharedInstanceLeavesATerminalItsWorker(t *testing.T) {
	var worker string
	dir := t.TempDir()
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "send", "new_in": dir, "message": "first job"})}},
		fakellm.Turn{When: asFiona, Content: "Started."},
		fakellm.Turn{When: asWorker, Content: "first done"},
		fakellm.Turn{When: "first done", Content: "First is done."},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relaySendTo("r2", &worker, "second job")}},
		fakellm.Turn{When: asFiona, Content: "Queued."},
	)
	r := newChatRig(t, fake, "")
	r.say("first job please")
	r.reply(sigGroup, "First is done.")
	worker = toolResult(t, fake, "r1")["worker"].(string)
	c := r.h.dial()
	c.attach(worker, "driver")
	r.say("now the second job")
	r.reply(sigGroup, "Queued.")
	c.none("role", 100*time.Millisecond)
	if q := toolResult(t, fake, "r2"); q["status"] != "queued" || q["took_from_terminal"] != nil {
		t.Fatalf("on a shared instance a chat is not the person at the terminal: %v", q)
	}
}

// A second agent on the same task in the same repository: the worker would
// have been told it was resuming a session that was still running.
func TestNoNewWorkerWhereASessionIsRunning(t *testing.T) {
	repo, elsewhere := t.TempDir(), t.TempDir()
	sub := filepath.Join(repo, "daemon")
	for _, d := range []string{sub, filepath.Join(repo, ".git")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	rel := make(chan struct{})
	t.Cleanup(func() { close(rel) })
	fake := fakellm.New(t,
		fakellm.Turn{When: asWorker, Content: "long job", ChunkRunes: 2, Release: rel},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "send", "new_in": repo, "message": "the same job"})}},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r2", map[string]any{"action": "send", "new_in": sub, "message": "the same job"})}},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r3", map[string]any{"action": "send", "new_in": elsewhere, "message": "another job"})}},
		fakellm.Turn{When: asFiona, Content: "Started elsewhere."},
		fakellm.Turn{When: asWorker, Content: "another job done"},
		fakellm.Turn{When: workerNotice, Content: "Elsewhere is done."},
	)
	r := newChatRig(t, fake, singleUser)
	c := r.h.dial()
	desk := c.create(sub)
	c.attach(desk, "driver")
	c.send(desk, "start the long job")
	c.await("turn.start")

	r.say("do that again")
	r.reply(sigGroup, "Started elsewhere.")
	for _, id := range []string{"r1", "r2"} {
		if res := toolResult(t, fake, id); res["success"] != false || !strings.Contains(str(res, "error"), desk) || !strings.Contains(str(res, "error"), "already running") {
			t.Fatalf("%s = %v", id, res)
		}
	}
	if res := toolResult(t, fake, "r3"); res["success"] != true {
		t.Fatalf("another directory is free: %v", res)
	}
	r.reply(sigGroup, "Elsewhere is done.")
}

func TestRelayDetachGivesATerminalSessionBack(t *testing.T) {
	dir := t.TempDir()
	var desk string
	fake := fakellm.New(t,
		fakellm.Turn{When: asWorker, Content: "desk work done"},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relaySendTo("r1", &desk, "carry on")}},
		fakellm.Turn{When: asFiona, Content: "Sent."},
		fakellm.Turn{When: asWorker, Content: "carried on"},
		fakellm.Turn{When: "carried on", ToolCalls: []fakellm.ToolCall{relayCall("r2", map[string]any{"action": "detach"})}},
		fakellm.Turn{When: asFiona, Content: "Let go."},
		fakellm.Turn{When: asWorker, Content: "alone again"},
	)
	r := newChatRig(t, fake, singleUser)
	desk = deskSession(t, r, dir, "start at the desk")
	r.say("carry on")
	r.reply(sigGroup, "Sent.")
	r.reply(sigGroup, "Let go.")
	if d := toolResult(t, fake, "r2"); d["success"] != true || d["detached"] != true {
		t.Fatalf("relay detach = %v", d)
	}
	st := openStore(t, r.h.home)
	if s, err := st.Session(context.Background(), desk); err != nil || s.Owner != "" {
		t.Fatalf("the session = %+v, %v", s, err)
	}
	c := r.h.dial()
	c.attach(desk, "driver")
	c.send(desk, "back at the desk")
	c.turn()
	c.close()
	time.Sleep(200 * time.Millisecond)
	if fake.Count() != 7 {
		t.Fatalf("%d requests: the chat still hears about the session", fake.Count())
	}
}
