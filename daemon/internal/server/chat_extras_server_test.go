package server_test

// Phase 5d: a chat can look at the picture and read the file it was sent
// (but not write next to them), and a job it schedules reports back to it
// (daemon/phases/05d-chat-tools.md).

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/cron"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/signalfake"
)

// sent finds the saved attachment whose name ends with suffix.
func sent(t *testing.T, home, suffix string) string {
	matches, _ := filepath.Glob(filepath.Join(home, "cache", "signal", "*"+suffix))
	if len(matches) != 1 {
		t.Errorf("attachments ending %s: %v", suffix, matches)
		return ""
	}
	return matches[0]
}

func TestFionaSeesWhatTheChatSends(t *testing.T) {
	var r *chatRig
	args := func(f func(home string) map[string]any) func() string {
		return func() string {
			b, _ := json.Marshal(f(r.h.home))
			return string(b)
		}
	}
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{
			{ID: "v1", Name: "vision_analyze", ArgumentsFunc: args(func(h string) map[string]any {
				return map[string]any{"image_url": sent(t, h, "_shot.png"), "question": "what is it?"}
			})},
			{ID: "f1", Name: "read_file", ArgumentsFunc: args(func(h string) map[string]any { return map[string]any{"path": sent(t, h, "_notes.txt")} })},
			{ID: "w1", Name: "write_file", ArgumentsFunc: args(func(h string) map[string]any {
				return map[string]any{"path": filepath.Join(h, "cache", "signal", "planted.txt"), "content": "x"}
			})},
		}},
		fakellm.Turn{When: "Describe this image in full, then answer the question", Content: "a red square"},
		fakellm.Turn{When: asFiona, Content: "A red square, and your notes."},
	)
	r = newChatRig(t, fake, "")
	r.sig.SetAttachment("img", pngOf(t, 8, 8), false)
	r.sig.SetAttachment("doc", []byte("buy milk\nfeed the cat\n"), false)
	r.ts++
	env := signalfake.OwnerInGroup(sigAccount, sigGroup, "what are these?", r.ts)
	m := env["envelope"].(map[string]any)["syncMessage"].(map[string]any)["sentMessage"].(map[string]any)
	m["attachments"] = []any{
		map[string]any{"id": "img", "contentType": "image/png", "filename": "shot.png", "size": 100},
		map[string]any{"id": "doc", "contentType": "text/plain", "filename": "notes.txt", "size": 22},
	}
	r.sig.Push(env)
	r.reply(sigGroup, "A red square, and your notes.")

	if v := toolResult(t, fake, "v1"); v["success"] != true || v["analysis"] != "a red square" {
		t.Fatalf("vision_analyze on an attachment = %v", v)
	}
	if f := toolResult(t, fake, "f1"); !strings.Contains(str(f, "content"), "feed the cat") {
		t.Fatalf("read_file on an attachment = %v", f)
	}
	if w := toolResult(t, fake, "w1"); !strings.Contains(str(w, "error"), "outside the notes folder") {
		t.Fatalf("a write next to the attachments = %v", w)
	}
}

func TestAJobAChatSchedulesRepliesToIt(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{
			call("j1", "cronjob", `{"action":"create","schedule":"every 1h","prompt":"check the queue","name":"queue check"}`),
		}},
		fakellm.Turn{When: asFiona, Content: "Scheduled."},
	)
	r := newChatRig(t, fake, "")
	r.say("check the queue every hour")
	r.reply(sigGroup, "Scheduled.")
	jobs, err := cron.Load(r.h.home)
	if err != nil || len(jobs) != 1 {
		t.Fatalf("jobs = %v, %v", jobs, err)
	}
	origin, _ := jobs[0]["origin"].(map[string]any)
	if jobs[0]["deliver"] != "origin" || origin["platform"] != "signal" || origin["chat_id"] != "group:"+sigGroup {
		t.Fatalf("the job = %v", jobs[0])
	}
}
