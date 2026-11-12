package main

// Phase 9a: soa-d tui, and client.Connect starting the daemon
// (daemon/phases/09a-tui-core.md). With SOA_D_TEST_AS_MAIN=1 this test binary
// is soa-d itself, so Connect can start it as the daemon.

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/client"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/server"
)

func TestMain(m *testing.M) {
	if os.Getenv("SOA_D_TEST_AS_MAIN") == "1" {
		os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
	}
	os.Exit(m.Run())
}

// shortHome is testHome in a short path: a unix socket path must fit in 107
// bytes.
func shortHome(t *testing.T, fake *fakellm.Server) string {
	t.Helper()
	home, err := os.MkdirTemp("", "soam")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(home) })
	src := testHome(t, fake)
	for _, f := range []string{"config.toml", ".env"} {
		b, err := os.ReadFile(filepath.Join(src, f))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(home, f), b, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return home
}

func TestConnectStartsTheDaemonOnce(t *testing.T) {
	home := shortHome(t, fakellm.New(t))
	t.Setenv("SOA_D_BIN", os.Args[0])
	t.Setenv("SOA_D_TEST_AS_MAIN", "1")
	c, err := client.Connect(home, "tui")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	pid := int(c.Hello()["pid"].(float64))
	sock := filepath.Join(home, "run", "soa-d.sock")
	t.Cleanup(func() {
		_ = syscall.Kill(pid, syscall.SIGTERM)
		for deadline := time.Now().Add(15 * time.Second); time.Now().Before(deadline); time.Sleep(50 * time.Millisecond) {
			if _, err := os.Stat(sock); os.IsNotExist(err) {
				return
			}
		}
		t.Error("the started daemon did not stop")
	})
	if pid == os.Getpid() || pid <= 0 {
		t.Fatalf("hello pid %d: the daemon is not a process of its own", pid)
	}
	if fi, err := os.Stat(filepath.Join(home, "logs", "soa-d.log")); err != nil || fi.Size() == 0 {
		t.Fatalf("the daemon's log: %v, %v", fi, err)
	}
	c2, err := client.Connect(home, "tui")
	if err != nil {
		t.Fatal(err)
	}
	defer c2.Close()
	if p := int(c2.Hello()["pid"].(float64)); p != pid {
		t.Fatalf("a second Connect reached pid %d, want the running %d", p, pid)
	}
}

func TestTUIFlags(t *testing.T) {
	for _, args := range [][]string{
		{"tui", "--bogus"},
		{"tui", "-c", "--resume", "x"},
		{"tui", "--home"},
		{"tui", "-m"},
	} {
		code, out, errOut := runIn(t, args...)
		if code != 2 || out != "" || !strings.Contains(errOut, "soa-d tui") {
			t.Fatalf("%v: exit %d, %q, %q", args, code, out, errOut)
		}
	}
}

func TestTUIResumingAnUnknownSessionFailsBeforeTheScreen(t *testing.T) {
	home := shortHome(t, fakellm.New(t))
	ln, err := server.Listen(home)
	if err != nil {
		t.Fatal(err)
	}
	srv, err := server.New(server.Options{Home: home, Version: "test"})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- srv.Serve(ctx, ln) }()
	defer func() { cancel(); <-done }()
	code, out, errOut := runIn(t, "tui", "--home", home, "--resume", "nope")
	if code != 1 || out != "" || errOut != "soa-d: session nope is not in soa-d\n" {
		t.Fatalf("exit %d, %q, %q", code, out, errOut)
	}
}

func TestTheDaemonCannotGainPrivileges(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{{ID: "c1", Name: "Bash", Arguments: `{"command":"grep NoNewPrivs /proc/self/status"}`}}},
		fakellm.Turn{Content: "done"},
	)
	home := shortHome(t, fake)
	t.Setenv("SOA_D_BIN", os.Args[0])
	t.Setenv("SOA_D_TEST_AS_MAIN", "1")
	c, err := client.Connect(home, "tui")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	pid := int(c.Hello()["pid"].(float64))
	sock := filepath.Join(home, "run", "soa-d.sock")
	t.Cleanup(func() {
		_ = syscall.Kill(pid, syscall.SIGTERM)
		for deadline := time.Now().Add(15 * time.Second); time.Now().Before(deadline); time.Sleep(50 * time.Millisecond) {
			if _, err := os.Stat(sock); os.IsNotExist(err) {
				return
			}
		}
		t.Error("the started daemon did not stop")
	})
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	var cr struct {
		Session struct {
			ID string `json:"id"`
		} `json:"session"`
	}
	if err := c.Call(ctx, "session.create", map[string]any{"cwd": t.TempDir()}, &cr); err != nil {
		t.Fatal(err)
	}
	id := cr.Session.ID
	for _, step := range []struct {
		method string
		params map[string]any
	}{
		{"session.attach", map[string]any{"session": id, "as": "driver"}},
		{"session.set", map[string]any{"session": id, "mode": "yolo"}},
		{"turn.send", map[string]any{"session": id, "text": "check"}},
	} {
		if err := c.Call(ctx, step.method, step.params, nil); err != nil {
			t.Fatalf("%s: %v", step.method, err)
		}
	}
	var result string
	for result == "" {
		select {
		case ev := <-c.Events():
			if ev.Kind == "tool.end" {
				result, _ = ev.Params["result"].(string)
			}
		case <-ctx.Done():
			t.Fatal("no tool result")
		}
	}
	var out struct {
		Output string `json:"output"`
	}
	if err := json.Unmarshal([]byte(result), &out); err != nil || strings.TrimSpace(out.Output) != "NoNewPrivs:\t1" {
		t.Fatalf("a command the daemon ran could gain privileges: %s", result)
	}
}
