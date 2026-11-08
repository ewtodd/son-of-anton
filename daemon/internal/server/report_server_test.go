package server_test

// Phase 13: fiona sends a worker's report to the chat as a PDF
// (daemon/phases/13-reports.md).

import (
	"encoding/base64"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/signalfake"
)

func TestFionaSendsAWorkersReport(t *testing.T) {
	dir := t.TempDir()
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "send", "new_in": dir, "message": "fix the build"})}},
		fakellm.Turn{When: asFiona, Content: "On it."},
		fakellm.Turn{When: asWorker, ToolCalls: []fakellm.ToolCall{terminal("c1", "echo compiled-ok")}},
		fakellm.Turn{When: asWorker, Content: "build fixed: a missing import"},
		fakellm.Turn{When: "build fixed: a missing import", Content: "The build is fixed."},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r2", map[string]any{"action": "report"})}},
		fakellm.Turn{When: asFiona, Content: "Here is the report."},
	)
	r := newChatRig(t, fake, "")
	r.say("please fix the build")
	r.reply(sigGroup, "On it.")
	r.reply(sigGroup, "The build is fixed.")
	r.say("send me a report of that")
	r.reply(sigGroup, "Here is the report.")

	res := toolResult(t, fake, "r2")
	sent, _ := res["sent"].(string)
	if res["success"] != true || !strings.HasSuffix(sent, ".pdf") || num(res, "messages") < 3 {
		t.Fatalf("relay report = %v", res)
	}
	var report signalfake.Sent
	reportAt, replyAt := -1, -1
	for i, m := range r.sig.Sends() {
		if len(m.Attachments) > 0 {
			report, reportAt = m, i
		}
		if m.Text == "Here is the report." {
			replyAt = i
		}
	}
	if reportAt < 0 || report.To != "group:"+sigGroup || !strings.HasPrefix(report.Text, "Report: ") || len(report.Attachments) != 1 {
		t.Fatalf("the report's message: %+v", report)
	}
	if reportAt > replyAt {
		t.Fatal("the report comes before fiona's word about it")
	}
	prefix := "data:application/pdf;filename=" + sent + ";base64,"
	data, ok := strings.CutPrefix(report.Attachments[0], prefix)
	if !ok {
		t.Fatalf("the attachment starts %.80q, want %q", report.Attachments[0], prefix)
	}
	pdf, err := base64.StdEncoding.DecodeString(data)
	if err != nil || !strings.HasPrefix(string(pdf), "%PDF-") {
		t.Fatalf("the attachment is not a PDF: %v %.20q", err, pdf)
	}
	f := filepath.Join(t.TempDir(), "r.pdf")
	if err := os.WriteFile(f, pdf, 0o600); err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command("pdftotext", "-layout", f, "-").Output()
	if err != nil {
		t.Fatalf("pdftotext (poppler-utils, in the dev shell): %v", err)
	}
	for _, want := range []string{"fix the build", "echo compiled-ok", "compiled-ok", "build fixed: a missing import"} {
		if !strings.Contains(string(out), want) {
			t.Errorf("the report lacks %q:\n%s", want, out)
		}
	}
}

func TestARelayReportIsOnlyOfTheChatsWorkers(t *testing.T) {
	dir := t.TempDir()
	var aWorker string
	fake := fakellm.New(t,
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{relayCall("r1", map[string]any{"action": "send", "new_in": dir, "message": "chat A's job"})}},
		fakellm.Turn{When: asFiona, Content: "A started."},
		fakellm.Turn{When: asWorker, Content: "A done"},
		fakellm.Turn{When: "A done", Content: "A finished."},
		fakellm.Turn{When: asFiona, ToolCalls: []fakellm.ToolCall{
			{ID: "r2", Name: "relay", ArgumentsFunc: func() string { return `{"action":"report","session":"` + aWorker + `"}` }},
			relayCall("r3", map[string]any{"action": "report"}),
		}},
		fakellm.Turn{When: asFiona, Content: "B refused."},
	)
	r := newChatRig(t, fake, "")
	r.sayIn(sigGroup, "start A's job")
	r.reply(sigGroup, "A finished.")
	aWorker = toolResult(t, fake, "r1")["worker"].(string)
	r.sayIn(sigGroup2, "report on A's worker")
	r.reply(sigGroup2, "B refused.")
	if res := toolResult(t, fake, "r2"); res["success"] != false || res["error"] != "no worker "+aWorker+" for this chat" {
		t.Fatalf("chat B's report of chat A's worker: %v", res)
	}
	if res := toolResult(t, fake, "r3"); res["success"] != false || !strings.HasPrefix(str(res, "error"), "no worker is bound to this chat") {
		t.Fatalf("a report with no worker bound: %v", res)
	}
	for _, m := range r.sig.Sends() {
		if len(m.Attachments) > 0 {
			t.Fatalf("a report was sent: %+v", m.To)
		}
	}
}
