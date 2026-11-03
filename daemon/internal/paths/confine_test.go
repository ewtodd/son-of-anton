package paths_test

// Phase 5b: a chat's file tools are confined to its notes folder, symlinks
// resolved.

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/paths"
)

func TestConfineKeepsPathsInTheFolder(t *testing.T) {
	home := t.TempDir()
	soa := filepath.Join(home, ".son-of-anton")
	notes := filepath.Join(soa, "notes", "gateway")
	if err := os.MkdirAll(notes, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(soa, "config.toml"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(soa, filepath.Join(notes, "escape")); err != nil {
		t.Fatal(err)
	}
	p := paths.Policy{Home: home, SoaHome: soa, Confine: notes}
	in := filepath.Join(notes, "ideas.md")
	if err := p.CheckRead(in); err != nil {
		t.Fatalf("inside: %v", err)
	}
	if c := p.CheckWrite(filepath.Join(notes, "sub", "new.md")); c.Denied {
		t.Fatalf("a new file inside: %+v", c)
	}
	for _, out := range []string{
		filepath.Join(soa, "config.toml"),
		filepath.Join(notes, "..", "common", "x.md"),
		filepath.Join(notes, "escape", "config.toml"),
		"/etc/hostname",
	} {
		err := p.CheckRead(out)
		if err == nil || !strings.Contains(err.Error(), "is outside the notes folder ("+notes+"); the chat's file tools reach only it.") {
			t.Errorf("read %s: %v", out, err)
		}
		if c := p.CheckWrite(out); !c.Denied || !strings.Contains(c.Reason, "outside the notes folder") {
			t.Errorf("write %s: %+v", out, c)
		}
	}
	if err := (paths.Policy{Home: home, SoaHome: soa}).CheckRead(filepath.Join(home, "x.txt")); err != nil {
		t.Fatalf("no Confine, no confinement: %v", err)
	}
}
