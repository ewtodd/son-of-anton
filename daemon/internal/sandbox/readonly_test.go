package sandbox_test

// Phase 11c review: appending beneath the run directory, a read-only policy
// (the physics manager's), and data directories named through a symlink.

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/sandbox"
	"github.com/ewtodd/son-of-anton/daemon/internal/sandbox/sandboxtest"
)

func TestFSAppendStaysBeneathTheRunDirectory(t *testing.T) {
	fs, run, _, outside := fsWorld(t)
	if err := fs.Append(filepath.Join(run, "sub", "log.txt"), "a\n"); err != nil {
		t.Fatal(err)
	}
	if err := fs.Append(filepath.Join(run, "sub", "log.txt"), "b\n"); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(filepath.Join(run, "sub", "log.txt")); string(b) != "a\nb\n" {
		t.Errorf("log = %q", b)
	}
	if err := os.Symlink(filepath.Join(outside, "victim"), filepath.Join(run, "filelink")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(run, "dirlink")); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{"filelink", "dirlink/new", "dirlink/victim"} {
		if err := fs.Append(filepath.Join(run, p), "EVIL"); err == nil {
			t.Errorf("appended to %s", p)
		}
	}
	if err := fs.Append(filepath.Join(outside, "new"), "EVIL"); err == nil {
		t.Error("appended outside the run directory")
	}
	untouched(t, outside)
}

func TestReadOnlyPolicy(t *testing.T) {
	run, data, rt := dirs(t)
	p := sandbox.Policy{RunDir: run, Runtime: rt, Home: filepath.Join(run, ".home"), ReadOnly: true}
	args, err := p.Args(nil, "x")
	if err != nil {
		t.Fatal(err)
	}
	if index(args, "--ro-bind", run, run) < 0 || index(args, "--bind", run, run) >= 0 {
		t.Errorf("args = %v", args)
	}
	if _, err := os.Stat(filepath.Join(run, ".home")); err == nil {
		t.Error("a read-only policy created the home")
	}
	if w := p.WriteRoots(); len(w) != 0 {
		t.Errorf("write roots %v", w)
	}
	if err := os.WriteFile(filepath.Join(run, "f"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	fs := p.FS()
	if b, err := fs.ReadFile(filepath.Join(run, "f")); err != nil || string(b) != "x" {
		t.Errorf("read: %q %v", b, err)
	}
	if _, _, err := fs.WriteAtomic(filepath.Join(run, "g"), "y"); err == nil {
		t.Error("wrote with a read-only policy")
	}
	if err := fs.Append(filepath.Join(run, "g"), "y"); err == nil {
		t.Error("appended with a read-only policy")
	}
	_ = data
}

func TestANamedDataSymlinkWorksAtBothPaths(t *testing.T) {
	rt := sandboxtest.Runtime(t)
	root := filepath.Dir(rt)
	run, data, link := filepath.Join(root, "run"), filepath.Join(root, "data"), filepath.Join(root, "named", "datalink")
	for _, d := range []string{run, data, filepath.Dir(link)} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(data, "events.txt"), []byte("DATAFILE"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(data, link); err != nil {
		t.Fatal(err)
	}
	p := sandbox.Policy{RunDir: run, Data: []string{link}, Runtime: rt, Home: filepath.Join(run, ".home")}

	script := "cat " + link + "/events.txt; echo; cat " + data + "/events.txt; echo; " +
		"echo x > " + link + "/w 2>&1 | head -1; echo x > " + data + "/w 2>&1 | head -1; ls " + link + " " + data
	cmd, err := p.Command(nil, p.Bash(), "-c", script)
	if err != nil {
		t.Fatal(err)
	}
	out, _ := cmd.CombinedOutput()
	if got := string(out); strings.Count(got, "DATAFILE") != 2 || !strings.Contains(got, "Read-only") {
		t.Errorf("output = %q", got)
	}
	if _, err := os.Stat(filepath.Join(data, "w")); err == nil {
		t.Error("wrote into the data directory")
	}

	// The file tools' opener takes either form.
	fs := p.FS()
	for _, path := range []string{link + "/events.txt", data + "/events.txt"} {
		if b, err := fs.ReadFile(path); err != nil || string(b) != "DATAFILE" {
			t.Errorf("read %s: %q %v", path, b, err)
		}
		if _, _, err := fs.WriteAtomic(path+".new", "x"); err == nil {
			t.Errorf("wrote at %s", path)
		}
	}
}
