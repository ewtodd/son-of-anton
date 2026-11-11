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
	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
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
	r.say("hello")
	r.reply(sigGroup, "hi")
	sys := systemPrompt(t, fake, 0)
	// fiona's block, then her notes folder; neither the default identity nor
	// the coding guidance appears in a chat.
	want := prompt.FionaIdentity("play", nil, false) + "\n\nYour notes folder: " + filepath.Join(r.h.home, "notes", "gateway")
	if !strings.HasPrefix(sys, want) {
		t.Fatalf("the chat's identity:\n%.900s", sys)
	}
	for _, not := range []string{"You are Son of Anton, an assistant", prompt.Guidance} {
		if strings.Contains(sys, not) {
			t.Fatalf("a chat's prompt carries %.60q", not)
		}
	}
}

func TestWorkerHoursAndNoInstanceName(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "hi"})
	r := newChatRig(t, fake, "[relay]\nworker_hours = [20, 7]\n")
	r.say("hello")
	r.reply(sigGroup, "hi")
	sys := systemPrompt(t, fake, 0)
	if !strings.HasPrefix(sys, prompt.FionaIdentity("", &[2]int{20, 7}, false)+"\n\n") {
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
	if strings.Join(names, ",") != "clarify,cronjob,memory,patch,read_file,relay,search_files,session_search,todo,web_extract,web_search,write_file" {
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
