package tui_test

// Phase 11c: a physics run's sessions are driven by their run's controller,
// so the TUI attaches to one as a viewer (the daemon refuses a driver).

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/client"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/tui"
)

func TestAPhysicsSessionIsResumedAsAViewer(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Hang: true})
	d := newDaemon(t, fake)
	data := t.TempDir()
	if err := os.WriteFile(filepath.Join(data, "d.root"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	restartWith(t, d, fmt.Sprintf("\n[physics]\npython = %q\ndata_dirs = [%q]\nworkspace_root = %q\n",
		filepath.Join(t.TempDir(), "python3"), data, t.TempDir()))

	c, err := client.Dial(d.home, "test")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	spec := fmt.Sprintf("name = \"peak\"\nproblem = \"Report centroid.\"\ndata = [%q]\n[[checks]]\nid = \"c\"\nkey = \"centroid\"\nexpected = 1\ntolerance = 0.1\n", filepath.Join(data, "d.root"))
	var run struct {
		ID     string `json:"id"`
		Worker string `json:"worker_session"`
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := c.Call(ctx, "physics.run", map[string]any{"spec": spec}, &run); err != nil {
		t.Fatal(err)
	}
	defer c.Call(ctx, "physics.stop", map[string]any{"run": run.ID}, nil)

	a := open(t, d, tui.Options{Resume: run.Worker})
	if a.Session() != run.Worker {
		t.Fatalf("the TUI is on %q, want %q", a.Session(), run.Worker)
	}
	waitScreen(t, a, "Begin.")
	if !strings.Contains(a.Screen(), "Begin.") {
		t.Fatal("the worker's transcript is not shown")
	}
	waitScreen(t, a, "ctrl+c interrupt")
	send(a, ":q") // a viewer leaves without being asked: the turn is not its to decide
	waitExit(t, a)
}
