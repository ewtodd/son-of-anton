package sandbox_test

// The daemon's own file access for a sandboxed session: every open is
// confined beneath a root by the kernel, so a symlink the sandboxed process
// plants (even between a check and the open) cannot lead out.

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/sandbox"
)

func fsWorld(t *testing.T) (fs *sandbox.FS, run, data, outside string) {
	t.Helper()
	run, data, rt := dirs(t)
	outside = filepath.Join(filepath.Dir(run), "home")
	if err := os.MkdirAll(filepath.Join(outside, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{filepath.Join(outside, "victim"), filepath.Join(outside, "sub", "deep")} {
		if err := os.WriteFile(f, []byte("OUTSIDE"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	p := sandbox.Policy{RunDir: run, Data: []string{data}, Runtime: rt, Home: filepath.Join(run, ".home")}
	return p.FS(), run, data, outside
}

func untouched(t *testing.T, outside string) {
	t.Helper()
	for _, f := range []string{"victim", "sub/deep"} {
		if b, _ := os.ReadFile(filepath.Join(outside, f)); string(b) != "OUTSIDE" {
			t.Errorf("%s was changed: %q", f, b)
		}
	}
	for _, f := range []string{"new", "sub/new", "planted/new"} {
		if _, err := os.Stat(filepath.Join(outside, f)); err == nil {
			t.Errorf("%s was created outside the roots", f)
		}
	}
	if ents, _ := os.ReadDir(outside); len(ents) != 2 {
		t.Errorf("outside gained entries: %v", ents)
	}
}

func TestFSRefusesSymlinksOutTheRoots(t *testing.T) {
	fs, run, data, outside := fsWorld(t)
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	must(os.Symlink(filepath.Join(outside, "victim"), filepath.Join(run, "filelink")))
	must(os.Symlink(outside, filepath.Join(run, "dirlink")))
	must(os.Symlink(outside, filepath.Join(data, "datalink")))
	must(os.Symlink("../../home/sub", filepath.Join(run, "relative")))

	for _, p := range []string{
		filepath.Join(run, "filelink"),
		filepath.Join(run, "dirlink", "victim"),
		filepath.Join(run, "dirlink", "sub", "deep"),
		filepath.Join(run, "relative", "deep"),
		filepath.Join(data, "datalink", "victim"),
		filepath.Join(outside, "victim"),
	} {
		if b, err := fs.ReadFile(p); err == nil {
			t.Errorf("read %s = %q", p, b)
		}
		if _, err := fs.Stat(p); err == nil {
			t.Errorf("stat %s succeeded", p)
		}
	}
	for _, d := range []string{filepath.Join(run, "dirlink"), filepath.Join(data, "datalink")} {
		if _, err := fs.ReadDir(d); err == nil {
			t.Errorf("readdir %s succeeded", d)
		}
	}
	for _, p := range []string{
		filepath.Join(run, "filelink"),
		filepath.Join(run, "dirlink", "new"),
		filepath.Join(run, "dirlink", "sub", "new"),
		filepath.Join(run, "dirlink", "planted", "new"),
		filepath.Join(run, "relative", "new"),
		filepath.Join(data, "new"),
		filepath.Join(outside, "new"),
	} {
		if _, _, err := fs.WriteAtomic(p, "PLANTED"); err == nil {
			t.Errorf("write %s succeeded", p)
		}
	}
	untouched(t, outside)
}

// A path that was a plain directory when it was checked and is a symlink by
// the time it is opened: the open, not the check, is what confines.
func TestFSSwapAfterTheCheckStillConfined(t *testing.T) {
	fs, run, _, outside := fsWorld(t)
	dir := filepath.Join(run, "work")
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(dir, "result.txt")
	// ... the check passes here ...
	if err := os.Remove(dir); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, dir); err != nil {
		t.Fatal(err)
	}
	// ... and the open follows.
	if _, _, err := fs.WriteAtomic(target, "PLANTED"); err == nil {
		t.Error("the write followed the swapped symlink")
	}
	if _, err := fs.ReadFile(filepath.Join(dir, "victim")); err == nil {
		t.Error("the read followed the swapped symlink")
	}
	untouched(t, outside)
}

func TestFSWorksInsideTheRoots(t *testing.T) {
	fs, run, data, _ := fsWorld(t)
	if err := os.WriteFile(filepath.Join(data, "events"), []byte("D"), 0o644); err != nil {
		t.Fatal(err)
	}
	// A symlink that stays inside the run directory is fine.
	f := filepath.Join(run, "a", "b", "c.txt")
	n, created, err := fs.WriteAtomic(f, "hello")
	if err != nil || n != 5 || !created {
		t.Fatalf("write: %d %v %v", n, created, err)
	}
	if err := os.Chmod(f, 0o755); err != nil {
		t.Fatal(err)
	}
	if _, created, err = fs.WriteAtomic(f, "again"); err != nil || created {
		t.Fatalf("rewrite: %v %v", created, err)
	}
	if fi, _ := os.Stat(f); fi.Mode().Perm() != 0o755 {
		t.Errorf("mode = %v", fi.Mode())
	}
	if b, err := fs.ReadFile(f); err != nil || string(b) != "again" {
		t.Errorf("read: %q %v", b, err)
	}
	if err := os.Symlink("a", filepath.Join(run, "inner")); err != nil {
		t.Fatal(err)
	}
	if b, err := fs.ReadFile(filepath.Join(run, "inner", "b", "c.txt")); err != nil || string(b) != "again" {
		t.Errorf("read through an inside link: %q %v", b, err)
	}
	if fi, err := fs.Stat(filepath.Join(run, "a")); err != nil || !fi.IsDir() {
		t.Errorf("stat: %v %v", fi, err)
	}
	if ents, err := fs.ReadDir(filepath.Join(run, "a", "b")); err != nil || len(ents) != 1 {
		t.Errorf("readdir: %v %v", ents, err)
	}
	if b, err := fs.ReadFile(filepath.Join(data, "events")); err != nil || string(b) != "D" {
		t.Errorf("read data: %q %v", b, err)
	}
	if _, _, err := fs.WriteAtomic(filepath.Join(data, "events"), "x"); err == nil || !strings.Contains(err.Error(), "outside") {
		t.Errorf("write data: %v", err)
	}
	if ents, _ := os.ReadDir(filepath.Join(run, "a", "b")); len(ents) != 1 {
		t.Errorf("a temp file was left behind: %v", ents)
	}
}
