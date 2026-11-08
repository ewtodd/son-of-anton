package main

// Phase 12: soa-d tui --socket/--view and soa-d serve --view-socket
// (daemon/phases/12-view-socket.md).

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/server"
)

func TestTUIViewFlags(t *testing.T) {
	for _, args := range [][]string{
		{"tui", "--view"},
		{"tui", "--view", "-c"},
		{"tui", "--view", "--resume", "x", "-m", "other"},
		{"tui", "--view", "--resume", "x", "--reasoning", "low"},
		{"tui", "--view", "--resume", "x", "--yolo"},
		{"tui", "--socket"},
		{"tui", "--socket", "/run/x/view.sock", "--home", "/h", "--view", "--resume", "x"},
	} {
		code, out, errOut := runIn(t, args...)
		if code != 2 || out != "" || !strings.Contains(errOut, "soa-d tui") {
			t.Errorf("%v: exit %d, %q, %q", args, code, out, errOut)
		}
	}
}

func TestServeViewFlags(t *testing.T) {
	home := shortHome(t, fakellm.New(t))
	for _, args := range [][]string{
		{"serve", "--view-socket", "/run/x/view.sock"},
		{"serve", "--view-group", "viewers"},
		{"serve", "--view-socket"},
		{"serve", "--home", home, "--view-group"},
	} {
		if code, _, errOut := runIn(t, args...); code != 2 || !strings.Contains(errOut, "soa-d serve") {
			t.Errorf("%v: exit %d, %q", args, code, errOut)
		}
	}
	code, _, errOut := runIn(t, "serve", "--home", home, "--view-socket", filepath.Join(t.TempDir(), "view.sock"), "--view-group", "no-such-group-for-soa-d")
	if code != 1 || !strings.Contains(errOut, "no-such-group-for-soa-d") {
		t.Fatalf("an unknown group: exit %d, %q", code, errOut)
	}
	if _, err := os.Stat(filepath.Join(home, "run", "soa-d.sock")); err == nil {
		t.Fatal("the daemon listened before it knew its view group")
	}
}

// The view TUI dials the socket it is given and nothing else: no daemon is
// started, and the home is not touched.
func TestTUIOverASocketNeverStartsADaemon(t *testing.T) {
	home := shortHome(t, fakellm.New(t)) // autostart is on
	marker := filepath.Join(t.TempDir(), "started")
	bin := filepath.Join(t.TempDir(), "soa-d")
	if err := os.WriteFile(bin, []byte("#!/bin/sh\ntouch "+marker+"\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SOA_D_BIN", bin)
	t.Setenv("SON_OF_ANTON_HOME", home)
	missing := filepath.Join(t.TempDir(), "view.sock")
	code, out, errOut := runIn(t, "tui", "--socket", missing, "--view", "--resume", "20261108_093000_abcdef")
	if code != 1 || out != "" || !strings.Contains(errOut, "cannot reach "+missing) {
		t.Fatalf("exit %d, %q, %q", code, out, errOut)
	}
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("soa-d tui --socket started a daemon")
	}
	if _, err := os.Stat(filepath.Join(home, "run")); err == nil {
		t.Fatal("soa-d tui --socket touched the home")
	}
}

func TestTUIOverTheViewSocket(t *testing.T) {
	home := shortHome(t, fakellm.New(t))
	dir, err := os.MkdirTemp("", "soav")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	sock := filepath.Join(dir, "view.sock")
	ln, err := server.Listen(home)
	if err != nil {
		t.Fatal(err)
	}
	vln, err := server.ListenView(sock, os.Getgid())
	if err != nil {
		t.Fatal(err)
	}
	srv, err := server.New(server.Options{Home: home, Version: "test"})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- srv.Serve(ctx, ln, vln) }()
	defer func() { cancel(); <-done }()
	code, out, errOut := runIn(t, "tui", "--socket", sock, "--view", "--resume", "nope")
	if code != 1 || out != "" || errOut != "soa-d: session nope is not in soa-d\n" {
		t.Fatalf("exit %d, %q, %q", code, out, errOut)
	}
}
