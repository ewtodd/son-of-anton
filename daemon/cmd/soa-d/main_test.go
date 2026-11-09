package main

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/buildinfo"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// testHome is an instance home pointing at the fake model server; the key
// lives only in .env, the way Nix installs it.
func testHome(t *testing.T, fake *fakellm.Server) string {
	t.Helper()
	dir := t.TempDir()
	cfg := "[model]\ndefault = \"test-model\"\nprovider = \"custom\"\n\n" +
		"[custom_providers.custom]\nbase_url = \"" + fake.URL + "\"\nkey_env = \"SOA_D_TEST_KEY\"\n"
	if err := os.WriteFile(filepath.Join(dir, "config.toml"), []byte(cfg), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".env"), []byte("SOA_D_TEST_KEY=s3cret\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SOA_D_TEST_KEY", "")
	t.Setenv("SON_OF_ANTON_HOME", "")
	return dir
}

func TestQueryPrintsTheAnswerAndRecordsTheSession(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "pong", ChunkRunes: 2})
	home := testHome(t, fake)
	var out, errOut bytes.Buffer
	if code := run([]string{"query", "--home", home, "ping"}, &out, &errOut); code != 0 {
		t.Fatalf("exit %d, stderr %q", code, errOut.String())
	}
	if out.String() != "pong\n" {
		t.Fatalf("stdout = %q, want the answer and a newline", out.String())
	}
	if got := fake.Headers()[0].Get("Authorization"); got != "Bearer s3cret" {
		t.Fatalf("Authorization = %q, want the key from .env", got)
	}
	if req := fake.Request(0); req["model"] != "test-model" {
		t.Fatalf("model = %v", req["model"])
	}

	st, err := store.Open(filepath.Join(home, "soa-d.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	hits, err := st.Search(context.Background(), "pong", 5)
	if err != nil || len(hits) != 1 {
		t.Fatalf("search = %+v, %v", hits, err)
	}
	sess, err := st.Session(context.Background(), hits[0].SessionID)
	if err != nil || sess.Kind != "oneshot" || sess.Source != "query" || sess.Model != "test-model" {
		t.Fatalf("session = %+v, %v", sess, err)
	}
	if _, err := os.Stat(filepath.Join(home, "state.db")); !os.IsNotExist(err) {
		t.Fatal("soa-d must never create or touch the Python fork's state.db")
	}
}

func TestQueryHomeComesFromTheEnvironment(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "ok"})
	home := testHome(t, fake)
	t.Setenv("SON_OF_ANTON_HOME", home)
	var out, errOut bytes.Buffer
	if code := run([]string{"query", "hi"}, &out, &errOut); code != 0 || out.String() != "ok\n" {
		t.Fatalf("exit %d, stdout %q, stderr %q", code, out.String(), errOut.String())
	}
}

func TestQueryFailsCleanly(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Status: 400, Body: `{"error":"no such model"}`})
	home := testHome(t, fake)
	var out, errOut bytes.Buffer
	code := run([]string{"query", "--home", home, "ping"}, &out, &errOut)
	if code == 0 || out.Len() != 0 {
		t.Fatalf("exit %d, stdout %q; a failed query prints nothing on stdout", code, out.String())
	}
	if !strings.Contains(errOut.String(), "400") {
		t.Fatalf("stderr = %q, want the HTTP status", errOut.String())
	}
}

func TestUsageErrors(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := run([]string{"query"}, &out, &errOut); code != 2 {
		t.Fatalf("query with no prompt: exit %d, want 2", code)
	}
	if code := run([]string{"frobnicate"}, &out, &errOut); code != 2 {
		t.Fatalf("unknown command: exit %d, want 2", code)
	}
	errOut.Reset()
	out.Reset()
	if code := run([]string{"version"}, &out, &errOut); code != 0 || out.String() != "soa-d "+buildinfo.Version+"\n" {
		t.Fatalf("version: exit %d, stdout %q", code, out.String())
	}
}

func toolTurns(command string) []fakellm.Turn {
	args, _ := json.Marshal(map[string]string{"command": command})
	return []fakellm.Turn{
		{ToolCalls: []fakellm.ToolCall{{ID: "c1", Name: "terminal", Arguments: string(args)}}},
		{Content: "done"},
	}
}

func TestQueryRunsToolsInTheWorkingDirectory(t *testing.T) {
	fake := fakellm.New(t, toolTurns("echo made > made.txt")...)
	home := testHome(t, fake)
	work := t.TempDir()
	t.Chdir(work)
	var out, errOut bytes.Buffer
	if code := run([]string{"query", "--home", home, "make a file"}, &out, &errOut); code != 0 {
		t.Fatalf("exit %d, stderr %q", code, errOut.String())
	}
	if out.String() != "done\n" {
		t.Fatalf("stdout = %q; tool activity must not reach stdout", out.String())
	}
	if _, err := os.Stat(filepath.Join(work, "made.txt")); err != nil {
		t.Fatal("the tool call must run in the query's working directory")
	}
	if offered, _ := fake.Request(0)["tools"].([]any); len(offered) != len(tools.Builtin()) {
		t.Fatalf("query must offer every builtin tool, got %d", len(offered))
	}
}

func TestQueryDeniesDangerousCommandsUnlessYolo(t *testing.T) {
	for _, yolo := range []bool{false, true} {
		fake := fakellm.New(t, toolTurns("rm -r victim")...)
		home := testHome(t, fake)
		work := t.TempDir()
		victim := filepath.Join(work, "victim")
		if err := os.MkdirAll(filepath.Join(victim, "inner"), 0o755); err != nil {
			t.Fatal(err)
		}
		t.Chdir(work)
		args := []string{"query", "--home", home}
		if yolo {
			args = append(args, "--yolo")
		}
		var out, errOut bytes.Buffer
		if code := run(append(args, "clean up"), &out, &errOut); code != 0 {
			t.Fatalf("yolo=%v: exit %d, stderr %q", yolo, code, errOut.String())
		}
		_, err := os.Stat(victim)
		if !yolo && err != nil {
			t.Fatal("without --yolo a dangerous command must be denied: nobody is there to approve it")
		}
		if yolo && !os.IsNotExist(err) {
			t.Fatal("with --yolo the command runs")
		}
		if !yolo && !bytes.Contains(fake.Messages(1)[3], []byte("BLOCKED")) {
			t.Fatalf("the model must be told the command was blocked: %s", fake.Messages(1)[3])
		}
	}
}
