package server_test

// Phase 5c through the daemon: fiona hands work to workers with relay
// (daemon/phases/05c-relay.md). Chat requests are told apart in the fake by
// fiona's identity, worker requests by the default identity.

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

const (
	asFiona  = "You never run commands or edit project files."
	asWorker = "You are Son of Anton, an assistant"
)

func relayCall(id string, args map[string]any) fakellm.ToolCall {
	b, _ := json.Marshal(args)
	return call(id, "relay", string(b))
}

// toolResult finds a tool call's result in any request the fake received.
func toolResult(t *testing.T, fake *fakellm.Server, callID string) map[string]any {
	t.Helper()
	var out map[string]any
	waitUntil(t, "the result of "+callID, func() bool {
		for i := 0; i < fake.Count(); i++ {
			for _, m := range wire(t, fake, i) {
				if str(m, "role") == "tool" && str(m, "tool_call_id") == callID {
					if json.Unmarshal([]byte(str(m, "content")), &out) == nil {
						return true
					}
				}
			}
		}
		return false
	})
	return out
}

func requestWith(t *testing.T, fake *fakellm.Server, marker string) int {
	t.Helper()
	for i := 0; i < fake.Count(); i++ {
		if strings.Contains(string(fake.Bodies()[i]), marker) {
			return i
		}
	}
	return -1
}

func TestARelayedTaskReportsBack(t *testing.T) {
	dir := t.TempDir()
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "send", "new_in": dir, "message": "fix the build"})}},
		fakellm.Turn{When: asFiona, Content: "On it."},
		fakellm.Turn{When: asWorker, Content: "build fixed: a missing import"},
		fakellm.Turn{When: "build fixed: a missing import", Content: "The build is fixed."},
	)
	r := newChatRig(t, fake, "")
	r.say("please fix the build in that repo")
	r.reply(sigGroup, "On it.")
	r.reply(sigGroup, "The build is fixed.")

	sent := toolResult(t, fake, "r1")
	worker, _ := sent["worker"].(string)
	if sent["success"] != true || sent["status"] != "started" || sent["when"] != "now" || worker == "" {
		t.Fatalf("relay send = %v", sent)
	}
	st := openStore(t, r.h.home)
	w, err := st.Session(context.Background(), worker)
	if err != nil || w.Kind != "interactive" || w.Owner == "" || w.Source != "relay" || w.Scope != "common" || w.Chat != "" || w.Cwd != dir {
		t.Fatalf("the worker = %+v, %v", w, err)
	}
	wi := requestWith(t, fake, asWorker)
	if wi < 0 || !strings.HasPrefix(systemPrompt(t, fake, wi), asWorker) || lastUser(t, fake, wi) != "fix the build" {
		t.Fatal("the worker runs as son-of-anton with the user's request")
	}
	names := strings.Join(toolNames(t, fake.Request(wi)), ",")
	if strings.Contains(names, "relay") || !strings.Contains(names, "terminal") {
		t.Fatalf("a worker's tools = %s", names)
	}
	ni := requestWith(t, fake, "build fixed: a missing import")
	notice := lastUser(t, fake, ni)
	if !strings.HasPrefix(notice, "[worker ") || !strings.Contains(notice, "("+worker+") finished]\nbuild fixed: a missing import") {
		t.Fatalf("the chat hears = %q", notice)
	}
	chatNames := strings.Join(toolNames(t, fake.Request(0)), ",")
	if !strings.Contains(chatNames, "relay") {
		t.Fatalf("a chat has relay: %s", chatNames)
	}
}

func TestRelaySaysWhenAWorkerWillRun(t *testing.T) {
	t.Setenv("SON_OF_ANTON_TIMEZONE", "UTC")
	dir := t.TempDir()
	h := time.Now().UTC().Hour()
	start, end := (h+2)%24, (h+3)%24
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "send", "new_in": dir, "message": "later please"})}},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r2", map[string]any{"action": "status"})}},
		fakellm.Turn{When: asFiona, Content: "Queued."},
	)
	r := newChatRig(t, fake, fmt.Sprintf("[relay]\nworker_hours = [%d, %d]\n", start, end))
	r.say("do it tonight")
	r.reply(sigGroup, "Queued.")
	sent := toolResult(t, fake, "r1")
	if sent["status"] != "queued" || sent["when"] != fmt.Sprintf("queued until %02d:00", start) {
		t.Fatalf("outside hours = %v", sent)
	}
	status := toolResult(t, fake, "r2")
	q, _ := status["queued"].([]any)
	if status["success"] != true || len(q) != 1 || q[0] != "later please" {
		t.Fatalf("status = %v", status)
	}
	if requestWith(t, fake, asWorker) >= 0 {
		t.Fatal("a worker ran outside its hours")
	}
}

func TestABusyWorkerQueuesOrSteers(t *testing.T) {
	dir := t.TempDir()
	hold := make(chan struct{})
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "send", "new_in": dir, "message": "long job"})}},
		fakellm.Turn{When: asFiona, Content: "Started."},
		fakellm.Turn{When: asWorker, Content: "working", Release: hold},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{
			relayCall("r2", map[string]any{"action": "send", "message": "and also this"}),
			relayCall("r3", map[string]any{"action": "send", "message": "actually stop and do that", "now": true}),
		}},
		fakellm.Turn{When: asFiona, Content: "Passed on."},
		fakellm.Turn{When: asWorker, Content: "steer-done"},
		fakellm.Turn{When: asWorker, Content: "queue-done"},
		fakellm.Turn{When: "steer-done", Content: "Reported one."},
		fakellm.Turn{When: "queue-done", Content: "Reported two."},
	)
	r := newChatRig(t, fake, "")
	r.say("start the long job")
	r.reply(sigGroup, "Started.")
	waitUntil(t, "the worker's request", func() bool { return requestWith(t, fake, asWorker) >= 0 })
	r.say("two more things")
	r.reply(sigGroup, "Passed on.")
	if q := toolResult(t, fake, "r2"); q["status"] != "queued" || q["when"] != "after the current turn" {
		t.Fatalf("a send to a busy worker = %v", q)
	}
	if s := toolResult(t, fake, "r3"); s["status"] != "steered" || s["when"] != "now" {
		t.Fatalf("now = %v", s)
	}
	close(hold)
	r.reply(sigGroup, "Reported one.")
	r.reply(sigGroup, "Reported two.")
	for _, m := range r.sig.Sends() {
		if strings.Contains(m.Text, "working") {
			t.Fatal("a steered worker turn reported back")
		}
	}
}

func TestEachChatDrivesOnlyItsWorkers(t *testing.T) {
	dir := t.TempDir()
	var aWorker string
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "send", "new_in": dir, "message": "chat A's job"})}},
		fakellm.Turn{When: asFiona, Content: "A started."},
		fakellm.Turn{When: asWorker, Content: "A done"},
		fakellm.Turn{When: "A done", Content: "A finished."},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{
			relayCall("r2", map[string]any{"action": "list"}),
			{ID: "r3", Name: "relay", ArgumentsFunc: func() string { return `{"action":"send","session":"` + aWorker + `","message":"hijack"}` }},
		}},
		fakellm.Turn{When: asFiona, Content: "B listed."},
	)
	r := newChatRig(t, fake, "")
	r.sayIn(sigGroup, "start A's job")
	r.reply(sigGroup, "A finished.")
	aWorker = toolResult(t, fake, "r1")["worker"].(string)
	r.sayIn(sigGroup2, "what workers are there?")
	r.reply(sigGroup2, "B listed.")
	if ws, _ := toolResult(t, fake, "r2")["workers"].([]any); len(ws) != 0 {
		t.Fatalf("chat B sees chat A's worker on a shared instance: %v", ws)
	}
	if hijack := toolResult(t, fake, "r3"); hijack["success"] != false || hijack["error"] != "no worker "+aWorker+" for this chat" {
		t.Fatalf("chat B drove chat A's worker: %v", hijack)
	}
}

func TestAWorkersApprovalAsksItsChat(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "f.txt")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "send", "new_in": dir, "message": "make f.txt writable"})}},
		fakellm.Turn{When: asFiona, Content: "Asked the worker."},
		fakellm.Turn{When: asWorker, ToolCalls: []fakellm.ToolCall{terminal("c1", "chmod 777 f.txt")}},
		fakellm.Turn{When: asWorker, Content: "it is writable now"},
		fakellm.Turn{When: "it is writable now", Content: "Done, f.txt is writable."},
	)
	r := newChatRig(t, fake, "")
	r.say("make f.txt writable")
	r.replyLike(sigGroup, "Worker ", "⚠️ Permission requested", "chmod 777")
	r.say("/approve")
	r.reply(sigGroup, "Done, f.txt is writable.")
	if fi, _ := os.Stat(f); fi.Mode().Perm() != 0o777 {
		t.Fatal("the approved command ran")
	}
}

func TestAPickedUpWorkerAsksTheTerminal(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "f.txt")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "send", "new_in": dir, "message": "first job"})}},
		fakellm.Turn{When: asFiona, Content: "Started."},
		fakellm.Turn{When: asWorker, Content: "first done"},
		fakellm.Turn{When: "first done", Content: "First is done."},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r2", map[string]any{"action": "send", "message": "second job"})}},
		fakellm.Turn{When: asFiona, Content: "Queued for the terminal."},
		fakellm.Turn{When: asWorker, ToolCalls: []fakellm.ToolCall{terminal("c1", "chmod 777 f.txt")}},
		fakellm.Turn{When: asWorker, Content: "second done"},
		fakellm.Turn{When: "second done", Content: "Second is done."},
	)
	r := newChatRig(t, fake, "")
	r.say("first job please")
	r.reply(sigGroup, "First is done.")
	worker := toolResult(t, fake, "r1")["worker"].(string)

	c := r.h.dial()
	var listed bool
	for _, s := range objs(c.ok("session.list", map[string]any{}), "sessions") {
		listed = listed || str(s, "id") == worker
	}
	if !listed {
		t.Fatal("the terminal lists workers")
	}
	c.attach(worker, "driver")
	r.say("now the second job")
	r.reply(sigGroup, "Queued for the terminal.")
	if q := toolResult(t, fake, "r2"); q["status"] != "queued" || q["when"] != "when the terminal that picked it up gets to it" {
		t.Fatalf("a send to a picked-up worker = %v", q)
	}
	c.ok("queue.steer", map[string]any{"session": worker})
	req := c.await("approval.request")
	if !strings.Contains(str(req, "subject"), "chmod 777") {
		t.Fatalf("approval = %v", req)
	}
	for _, m := range r.sig.Sends() {
		if strings.Contains(m.Text, "Permission requested") {
			t.Fatal("a picked-up worker's approval went to the chat")
		}
	}
	c.ok("approval.answer", map[string]any{"session": worker, "approval_id": str(req, "approval_id"), "choice": "once"})
	c.turn()
	if fi, _ := os.Stat(f); fi.Mode().Perm() != 0o777 {
		t.Fatal("the terminal approved the command")
	}
	r.reply(sigGroup, "Second is done.")
}
