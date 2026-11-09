package main

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/server"
)

// A second daemon on the same home refuses to start and leaves the first
// one's socket alone.
func TestServeRefusesASecondDaemon(t *testing.T) {
	fake := fakellm.New(t)
	home, err := os.MkdirTemp("", "soad") // short: unix socket paths are limited
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
	defer func() {
		cancel()
		select {
		case <-done:
		case <-time.After(15 * time.Second):
			t.Error("Serve did not return")
		}
	}()

	var out, errOut bytes.Buffer
	if code := run([]string{"serve", "--home", home}, &out, &errOut); code != 1 || !strings.Contains(errOut.String(), "already running") {
		t.Fatalf("exit %d, stderr %q; want 1 and \"already running\"", code, errOut.String())
	}
	if _, err := os.Lstat(filepath.Join(home, "run", "soa-d.sock")); err != nil {
		t.Fatalf("the running daemon's socket must survive: %v", err)
	}
}
