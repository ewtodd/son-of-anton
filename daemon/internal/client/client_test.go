package client_test

// Phase 9a: the Go client of the daemon's socket (daemon/phases/09a-tui-core.md).
// Connect's autostart is tested in cmd/soa-d, where the test binary can be the
// daemon.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/client"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/server"
)

type daemon struct {
	home   string
	cancel context.CancelFunc
	done   chan error
}

// serve starts a real server in a fresh short home (a unix socket path must fit
// in 107 bytes) against fake.
func serve(t *testing.T, fake *fakellm.Server) *daemon {
	t.Helper()
	home, err := os.MkdirTemp("", "soac")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(home) })
	t.Setenv("SOA_D_TEST_KEY", "")
	t.Setenv("SON_OF_ANTON_HOME", "")
	cfg := "[model]\ndefault = \"test-model\"\nprovider = \"custom\"\n\n" +
		"[custom_providers.custom]\nbase_url = \"" + fake.URL + "\"\nkey_env = \"SOA_D_TEST_KEY\"\n"
	if err := os.WriteFile(filepath.Join(home, "config.toml"), []byte(cfg), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(home, ".env"), []byte("SOA_D_TEST_KEY=s3cret\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	ln, err := server.Listen(home)
	if err != nil {
		t.Fatal(err)
	}
	srv, err := server.New(server.Options{Home: home, Version: "test"})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	d := &daemon{home: home, cancel: cancel, done: make(chan error, 1)}
	go func() { d.done <- srv.Serve(ctx, ln) }()
	t.Cleanup(d.stop)
	return d
}

func (d *daemon) stop() {
	if d.cancel == nil {
		return
	}
	d.cancel()
	d.cancel = nil
	select {
	case <-d.done:
	case <-time.After(15 * time.Second):
	}
}

func ctx(t *testing.T) context.Context {
	c, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	return c
}

type created struct {
	Session struct {
		ID string `json:"id"`
	} `json:"session"`
}

func TestDialCallsAndStreamsEvents(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "hello there", ChunkRunes: 2})
	d := serve(t, fake)
	c, err := client.Dial(d.home, "tui")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if h := c.Hello(); h["protocol"] != float64(1) || h["home"] != d.home {
		t.Fatalf("hello = %v", h)
	}
	var cr created
	if err := c.Call(ctx(t), "session.create", map[string]any{"cwd": t.TempDir()}, &cr); err != nil || cr.Session.ID == "" {
		t.Fatalf("session.create = %+v, %v", cr, err)
	}
	if err := c.Call(ctx(t), "session.attach", map[string]any{"session": cr.Session.ID, "as": "driver"}, nil); err != nil {
		t.Fatal(err)
	}
	if err := c.Call(ctx(t), "turn.send", map[string]any{"session": cr.Session.ID, "text": "hi"}, nil); err != nil {
		t.Fatal(err)
	}
	var text strings.Builder
	var kinds []string
	timeout := time.After(10 * time.Second)
	for done := false; !done; {
		select {
		case ev := <-c.Events():
			if ev.Session != cr.Session.ID || ev.Params["kind"] != ev.Kind {
				t.Fatalf("event %+v", ev)
			}
			kinds = append(kinds, ev.Kind)
			if ev.Kind == "assistant.delta" {
				text.WriteString(ev.Params["text"].(string))
			}
			if ev.Kind == "turn.end" {
				if ev.Params["status"] != "done" {
					t.Fatalf("turn.end = %v", ev.Params)
				}
				done = true
			}
		case <-timeout:
			t.Fatalf("no turn.end; events so far %v", kinds)
		}
	}
	if kinds[0] != "turn.start" || text.String() != "hello there" {
		t.Fatalf("events %v, text %q", kinds, text.String())
	}
}

func TestAnRPCErrorIsAnError(t *testing.T) {
	d := serve(t, fakellm.New(t))
	c, err := client.Dial(d.home, "tui")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	err = c.Call(ctx(t), "no.such.method", map[string]any{}, nil)
	var rpc *client.Error
	if !errors.As(err, &rpc) || rpc.Code != -32601 || err.Error() != rpc.Message {
		t.Fatalf("unknown method: %v", err)
	}
	err = c.Call(ctx(t), "session.attach", map[string]any{"session": "nope", "as": "driver"}, nil)
	if !errors.As(err, &rpc) || rpc.Code != -32011 {
		t.Fatalf("unknown session: %v", err)
	}
}

func TestUnreadEventsDoNotHoldUpCalls(t *testing.T) {
	long := strings.Repeat("abcdefghij", 600)
	fake := fakellm.New(t, fakellm.Turn{Content: long, ChunkRunes: 1})
	d := serve(t, fake)
	c, err := client.Dial(d.home, "tui")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	var cr created
	if err := c.Call(ctx(t), "session.create", map[string]any{"cwd": t.TempDir()}, &cr); err != nil {
		t.Fatal(err)
	}
	if err := c.Call(ctx(t), "session.attach", map[string]any{"session": cr.Session.ID, "as": "driver"}, nil); err != nil {
		t.Fatal(err)
	}
	if err := c.Call(ctx(t), "turn.send", map[string]any{"session": cr.Session.ID, "text": "go"}, nil); err != nil {
		t.Fatal(err)
	}
	// Nobody reads Events() while the turn streams 6,000 deltas.
	deadline := time.Now().Add(10 * time.Second)
	for {
		var res struct {
			Sessions []map[string]any `json:"sessions"`
		}
		if err := c.Call(ctx(t), "session.list", map[string]any{}, &res); err != nil {
			t.Fatalf("session.list while events pile up: %v", err)
		}
		if len(res.Sessions) == 1 && res.Sessions[0]["status"] == "idle" && fake.Count() == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the turn did not finish: %v", res.Sessions)
		}
		time.Sleep(20 * time.Millisecond)
	}
	var text strings.Builder
	for ended := false; !ended; {
		select {
		case ev := <-c.Events():
			if ev.Kind == "assistant.delta" {
				text.WriteString(ev.Params["text"].(string))
			}
			ended = ev.Kind == "turn.end"
		case <-time.After(5 * time.Second):
			t.Fatal("events were lost")
		}
	}
	if text.String() != long {
		t.Fatalf("got %d characters of %d, in order: %v", text.Len(), len(long), strings.HasPrefix(long, text.String()))
	}
}

func TestTheConnectionGoing(t *testing.T) {
	d := serve(t, fakellm.New(t))
	c, err := client.Dial(d.home, "tui")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	d.stop()
	select {
	case <-c.Done():
	case <-time.After(10 * time.Second):
		t.Fatal("Done() did not close when the daemon stopped")
	}
	for closed := false; !closed; { // anything left drains, then the channel is closed
		select {
		case _, open := <-c.Events():
			closed = !open
		case <-time.After(5 * time.Second):
			t.Fatal("Events() was not closed after Done()")
		}
	}
	start := time.Now()
	if err := c.Call(context.Background(), "session.list", map[string]any{}, nil); err == nil || time.Since(start) > 5*time.Second {
		t.Fatalf("a call on a gone connection = %v after %v", err, time.Since(start))
	}
	if _, err := client.Dial(d.home, "tui"); err == nil {
		t.Fatal("Dial with no daemon succeeded")
	}
}

func TestConnectNeverStartsADaemonAUnitServes(t *testing.T) {
	home, err := os.MkdirTemp("", "soac")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(home) })
	t.Setenv("SON_OF_ANTON_HOME", "")
	cfg := "[model]\ndefault = \"m\"\n\n[daemon]\nautostart = false\ninstance = \"play\"\n"
	if err := os.WriteFile(filepath.Join(home, "config.toml"), []byte(cfg), 0o600); err != nil {
		t.Fatal(err)
	}
	// A stand-in daemon that would leave a mark if it were started.
	mark := filepath.Join(home, "started")
	bin := filepath.Join(home, "soa-d")
	if err := os.WriteFile(bin, []byte("#!/bin/sh\ntouch "+mark+"\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SOA_D_BIN", bin)
	_, err = client.Connect(home, "tui")
	if err == nil || !strings.Contains(err.Error(), "son-of-anton-play") {
		t.Fatalf("Connect on a home a unit serves = %v", err)
	}
	time.Sleep(200 * time.Millisecond)
	if _, err := os.Stat(mark); err == nil {
		t.Fatal("Connect started a daemon on a home a unit serves")
	}
}
