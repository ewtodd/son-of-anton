package server_test

// A chat that polls relay status gets a short answer while nothing changes.

import (
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

func TestAnUnchangedWorkerStatusSaysSo(t *testing.T) {
	dir := t.TempDir()
	hold := make(chan struct{})
	t.Cleanup(func() { close(hold) })
	var worker string
	status := func(id string) fakellm.ToolCall {
		return fakellm.ToolCall{ID: id, Name: "relay", ArgumentsFunc: func() string { return `{"action":"status","session":"` + worker + `"}` }}
	}
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "send", "new_in": dir, "message": "long job"})}},
		fakellm.Turn{When: asFiona, Content: "Started."},
		fakellm.Turn{When: asWorker, Content: "working", Release: hold},
		// chat A: first status, the same again, then a change, then the same again
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{status("s1")}},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{status("s2")}},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("q1", map[string]any{"action": "send", "message": "and also this"})}},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{status("s3")}},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{status("s4")}},
		fakellm.Turn{When: asFiona, Content: "Polled."},
		// chat B, on a single-user instance: its first status
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{status("b1")}},
		fakellm.Turn{When: asFiona, Content: "B looked."},
	)
	r := newChatRig(t, fake, singleUser)
	r.say("start the long job")
	r.reply(sigGroup, "Started.")
	worker = toolResult(t, fake, "r1")["worker"].(string)
	waitUntil(t, "the worker's request", func() bool { return requestWith(t, fake, asWorker) >= 0 })
	r.say("how is it going?")
	r.reply(sigGroup, "Polled.")

	s1, s2 := toolResult(t, fake, "s1"), toolResult(t, fake, "s2")
	if s1["unchanged"] != nil || s1["state"] != "running" || s1["last_reply"] == nil {
		t.Fatalf("the first status = %v", s1)
	}
	note, _ := s2["note"].(string)
	if s2["success"] != true || s2["unchanged"] != true || s2["state"] != "running" || s2["worker"] != worker ||
		!strings.Contains(note, "Nothing has changed") || !strings.Contains(note, "ago") ||
		!strings.Contains(note, "notification") || !strings.Contains(note, "End your turn now") || s2["queued"] != nil {
		t.Fatalf("an unchanged status = %v", s2)
	}
	if s3 := toolResult(t, fake, "s3"); s3["unchanged"] != nil || len(s3["queued"].([]any)) != 1 {
		t.Fatalf("a status after a change = %v", s3)
	}
	if s4 := toolResult(t, fake, "s4"); s4["unchanged"] != true {
		t.Fatalf("a repeat after the change = %v", s4)
	}

	r.sayIn(sigGroup2, "and the worker?")
	r.reply(sigGroup2, "B looked.")
	if b := toolResult(t, fake, "b1"); b["unchanged"] != nil || b["state"] != "running" || b["queued"] == nil {
		t.Fatalf("another chat's first status = %v", b)
	}
}
