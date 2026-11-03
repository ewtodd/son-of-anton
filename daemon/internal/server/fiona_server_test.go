package server_test

// Phase 5b through the daemon: a chat's agent is fiona
// (daemon/phases/05b-fiona.md).

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

const (
	fionaOne = "You are fiona, the front desk of this son-of-anton instance (play), talking with the user over Signal. " +
		"You answer questions, look things up, read images, and keep notes and memories yourself."
	fionaTwo = "You never run commands or edit project files. Your file tools reach only your notes folder. " +
		"Real work (code, files, commands, anything in a repository or on a machine) goes to a son-of-anton worker through the relay tool: " +
		"pass the user's request on as they gave it, then tell them what the worker reports. " +
		"A worker cannot see your memory, so add anything from it the worker needs, marked as context from you."
	fionaAnyTime = "Workers here can run at any time. A request sent while a worker is busy waits in a queue, and the relay result says when it will run " +
		"(for example \"after the current turn\"). Tell the user exactly that. At a terminal, the user can take a worker over with /pickup."
	fionaHours = "Workers here run only between 20:00 and 07:00. A request sent outside those hours, or while a worker is busy, waits in a queue, " +
		"and the relay result says when it will run (for example \"queued until 20:00\" or \"after the current turn\"). Tell the user exactly that; " +
		"don't promise it sooner. At a terminal, the user can take a worker over with /pickup, which runs it at once whatever the hours."
)

func systemPrompt(t *testing.T, fake *fakellm.Server, i int) string {
	t.Helper()
	ms := wire(t, fake, i)
	if len(ms) == 0 || str(ms[0], "role") != "system" {
		t.Fatalf("no system message in request %d", i)
	}
	return str(ms[0], "content")
}

func TestFionaIsTheChatsIdentity(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "hi"})
	r := newChatRig(t, fake, "instance = \"play\"\n") // inside the rig's [daemon] table
	// The prompt is built when the chat's first message makes its session.
	if err := os.WriteFile(filepath.Join(r.h.home, "SOUL.md"), []byte("\nBe warm and brief.\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	r.say("hello")
	r.reply(sigGroup, "hi")
	sys := systemPrompt(t, fake, 0)
	want := fionaOne + "\n\n" + fionaTwo + "\n\n" + fionaAnyTime + "\n\nBe warm and brief."
	if !strings.HasPrefix(sys, want) {
		t.Fatalf("the chat's identity:\n%.900s", sys)
	}
	if strings.Contains(sys, "You are Son of Anton, an assistant") {
		t.Fatal("the default identity never appears in a chat")
	}
}

func TestWorkerHoursAndNoInstanceName(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "hi"})
	r := newChatRig(t, fake, "[relay]\nworker_hours = [20, 7]\n")
	r.say("hello")
	r.reply(sigGroup, "hi")
	sys := systemPrompt(t, fake, 0)
	one := strings.Replace(fionaOne, " (play)", "", 1)
	if !strings.HasPrefix(sys, one+"\n\n"+fionaTwo+"\n\n"+fionaHours) {
		t.Fatalf("hours and no instance:\n%.900s", sys)
	}
}

func TestTheChatsToolsAndNotesFolder(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{
			call("w1", "write_file", `{"path":"ideas.md","content":"a garden plan"}`),
			call("w2", "write_file", `{"path":"/tmp/fiona-escape.md","content":"x"}`),
			call("r1", "read_file", `{"path":"../../config.toml"}`),
		}},
		fakellm.Turn{Content: "noted"},
	)
	r := newChatRig(t, fake, "")
	r.say("note my garden plan")
	r.reply(sigGroup, "noted")
	names := toolNames(t, fake.Request(0))
	sort.Strings(names)
	if strings.Join(names, ",") != "clarify,cronjob,memory,patch,read_file,relay,search_files,session_search,todo,vision_analyze,web_extract,web_search,write_file" {
		t.Fatalf("a chat's tools = %v", names)
	}
	notes := filepath.Join(r.h.home, "notes", "gateway")
	if b, err := os.ReadFile(filepath.Join(notes, "ideas.md")); err != nil || string(b) != "a garden plan" {
		t.Fatalf("a relative path lands in the notes folder: %q, %v", b, err)
	}
	if _, err := os.Stat("/tmp/fiona-escape.md"); err == nil {
		_ = os.Remove("/tmp/fiona-escape.md")
		t.Fatal("a write outside the notes folder happened")
	}
	var results []string
	for _, m := range wire(t, fake, 1) {
		if str(m, "role") == "tool" {
			results = append(results, str(m, "content"))
		}
	}
	if len(results) != 3 || !strings.Contains(results[1], "outside the notes folder") || !strings.Contains(results[2], "outside the notes folder") {
		t.Fatalf("tool results = %v", results)
	}
}

func TestFionaFallsBackWhenHerModelIsDown(t *testing.T) {
	down := fakellm.Turn{When: `"model":"fiona-model"`, Status: 503, Body: `{"error":{"message":"eklow is down"}}`}
	fake := fakellm.New(t, down, down, down, down,
		fakellm.Turn{Content: "the default model answered"},
		fakellm.Turn{When: `"model":"fiona-model"`, Content: "fiona is back"},
	)
	r := newChatRig(t, fake, "[gateway]\nmodel = \"fiona-model\"\n\n[custom_providers.custom.models.\"fiona-model\"]\n")
	r.say("are you there?")
	r.reply(sigGroup, "the default model answered")
	if n := fake.Count(); n != 5 {
		t.Fatalf("requests = %d: four tries on fiona's route, then one on the default", n)
	}
	if m := str(fake.Request(4), "model"); m != "test-model" {
		t.Fatalf("the fallback went to %q", m)
	}
	r.say("and now?")
	r.reply(sigGroup, "fiona is back")
	if m := str(fake.Request(5), "model"); m != "fiona-model" {
		t.Fatalf("the next request tries fiona's route first: %q", m)
	}
}
