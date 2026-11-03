package main

// Phase 8g: a soa-d instance imports its Python state.db at start and retires
// it (daemon/phases/08g-models-and-cleanup.md).

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/client"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

// pythonHome is a short home with config.toml (plus extra) and a Python
// state.db holding one terminal session.
func pythonHome(t *testing.T, extra string) string {
	t.Helper()
	home := shortHome(t, fakellm.New(t))
	f, err := os.OpenFile(filepath.Join(home, "config.toml"), os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString(extra); err != nil {
		t.Fatal(err)
	}
	f.Close()
	pdb, err := sql.Open("sqlite", filepath.Join(home, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer pdb.Close()
	if _, err := pdb.Exec(pySchema); err != nil {
		t.Fatal(err)
	}
	if _, err := pdb.Exec(`INSERT INTO sessions (id, source, started_at, title, cwd) VALUES
		('20261001_090000_aaaaaaaa', 'cli', 1759327200, 'an old python chat', '/tmp')`); err != nil {
		t.Fatal(err)
	}
	if _, err := pdb.Exec(`INSERT INTO messages (session_id, role, content, timestamp) VALUES
		('20261001_090000_aaaaaaaa', 'user', 'hello from the python days', 1759327201),
		('20261001_090000_aaaaaaaa', 'assistant', 'hi back', 1759327202)`); err != nil {
		t.Fatal(err)
	}
	return home
}

// startDaemon starts this binary as the daemon on home and stops it at the end.
func startDaemon(t *testing.T, home string) *client.Client {
	t.Helper()
	t.Setenv("SOA_D_BIN", os.Args[0])
	t.Setenv("SOA_D_TEST_AS_MAIN", "1")
	c, err := client.Connect(home, "tui")
	if err != nil {
		t.Fatal(err)
	}
	pid := int(c.Hello()["pid"].(float64))
	sock := filepath.Join(home, "run", "soa-d.sock")
	t.Cleanup(func() {
		c.Close()
		_ = syscall.Kill(pid, syscall.SIGTERM)
		for deadline := time.Now().Add(15 * time.Second); time.Now().Before(deadline); time.Sleep(50 * time.Millisecond) {
			if _, err := os.Stat(sock); os.IsNotExist(err) {
				return
			}
		}
		t.Error("the started daemon did not stop")
	})
	return c
}

func titles(t *testing.T, c *client.Client) []string {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var res struct {
		Sessions []map[string]any `json:"sessions"`
	}
	if err := c.Call(ctx, "session.list", map[string]any{}, &res); err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, s := range res.Sessions {
		title, _ := s["title"].(string)
		out = append(out, title)
	}
	return out
}

func TestServeImportsAndRetiresThePythonStateDB(t *testing.T) {
	home := pythonHome(t, "\n[daemon]\nimport_python = true\n")
	c := startDaemon(t, home)
	if got := titles(t, c); len(got) != 1 || got[0] != "an old python chat" {
		t.Fatalf("sessions after start = %q", got)
	}
	if _, err := os.Stat(filepath.Join(home, "state.db")); !os.IsNotExist(err) {
		t.Fatalf("state.db is still in place: %v", err)
	}
	if _, err := os.Stat(filepath.Join(home, "state.db.imported")); err != nil {
		t.Fatalf("state.db.imported: %v", err)
	}
}

func TestServeLeavesStateDBAloneWithoutTheSetting(t *testing.T) {
	home := pythonHome(t, "")
	c := startDaemon(t, home)
	if got := titles(t, c); len(got) != 0 {
		t.Fatalf("sessions imported without import_python: %q", got)
	}
	if _, err := os.Stat(filepath.Join(home, "state.db")); err != nil {
		t.Fatalf("state.db was touched: %v", err)
	}
}
