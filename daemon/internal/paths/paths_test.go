package paths_test

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/paths"
)

// policy builds a user home with an instance home inside it, the way the
// owner's instances are laid out (~/.son-of-anton).
func policy(t *testing.T) (paths.Policy, string) {
	t.Helper()
	home := t.TempDir()
	soa := filepath.Join(home, ".son-of-anton")
	if err := os.MkdirAll(soa, 0o700); err != nil {
		t.Fatal(err)
	}
	return paths.Policy{Home: home, SoaHome: soa}, home
}

func TestResolve(t *testing.T) {
	p, home := policy(t)
	cwd := filepath.Join(home, "work")
	if err := os.MkdirAll(cwd, 0o755); err != nil {
		t.Fatal(err)
	}
	cases := map[string]string{
		"main.go":         filepath.Join(cwd, "main.go"),
		"./sub/../x.txt":  filepath.Join(cwd, "x.txt"),
		"~/notes.md":      filepath.Join(home, "notes.md"),
		"~":               home,
		"/abs/path/f.txt": "/abs/path/f.txt",
	}
	for in, want := range cases {
		got, err := p.Resolve(cwd, in)
		if err != nil || got != want {
			t.Errorf("Resolve(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	if _, err := p.Resolve(cwd, ""); err == nil {
		t.Error("Resolve of an empty path must fail")
	}
}

func TestResolveFollowsSymlinks(t *testing.T) {
	p, home := policy(t)
	real := filepath.Join(home, "real")
	if err := os.MkdirAll(real, 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(home, "link")
	if err := os.Symlink(real, link); err != nil {
		t.Fatal(err)
	}
	// An existing directory reached through a link, and a new file under it.
	got, err := p.Resolve(home, "link/new.txt")
	if err != nil || got != filepath.Join(real, "new.txt") {
		t.Fatalf("Resolve through a symlink = %q, %v", got, err)
	}
}

func TestDeniedWrites(t *testing.T) {
	p, home := policy(t)
	for _, abs := range []string{
		filepath.Join(home, ".ssh", "id_ed25519"),
		filepath.Join(home, ".ssh", "authorized_keys"),
		filepath.Join(home, ".ssh", "config"),
		filepath.Join(home, ".aws", "credentials"),
		filepath.Join(home, ".gnupg", "pubring.kbx"),
		filepath.Join(home, ".netrc"),
		filepath.Join(home, ".git-credentials"),
		filepath.Join(p.SoaHome, ".env"),
		filepath.Join(p.SoaHome, "config.toml"),
		filepath.Join(p.SoaHome, "soa-d.db"),
		filepath.Join(p.SoaHome, "state.db"),
		filepath.Join(p.SoaHome, "sessions", "x.json"),
		"/etc/hosts",
		"/etc/sudoers.d/x",
		"/boot/loader.conf",
		"/usr/lib/systemd/system/x.service",
	} {
		c := p.CheckWrite(abs)
		if !c.Denied || c.Reason == "" {
			t.Errorf("CheckWrite(%q) = %+v, want denied with a reason", abs, c)
		}
	}
}

func TestProtectedInstructionFilesNeedApproval(t *testing.T) {
	p, home := policy(t)
	for _, abs := range []string{
		filepath.Join(home, "proj", "AGENTS.md"),
		filepath.Join(home, "proj", "sub", "agents.md"),
		filepath.Join(home, "proj", "CLAUDE.md"),
		filepath.Join(p.SoaHome, "SOUL.md"),
		filepath.Join(home, "proj", ".cursorrules"),
	} {
		c := p.CheckWrite(abs)
		if c.Denied || !c.Protected {
			t.Errorf("CheckWrite(%q) = %+v, want protected (needs approval), not denied", abs, c)
		}
	}
}

func TestOrdinaryWrites(t *testing.T) {
	p, home := policy(t)
	for _, abs := range []string{
		filepath.Join(home, "proj", "main.go"),
		filepath.Join(home, "proj", "README.md"),
		filepath.Join(p.SoaHome, "notes", "fleet.md"),
		filepath.Join(p.SoaHome, "memories", "facts.toml"),
		"/tmp/scratch.txt",
	} {
		if c := p.CheckWrite(abs); c.Denied || c.Protected {
			t.Errorf("CheckWrite(%q) = %+v, want an ordinary write", abs, c)
		}
	}
}

func TestReadBlocks(t *testing.T) {
	p, home := policy(t)
	for _, abs := range []string{
		filepath.Join(home, "proj", ".env"),
		filepath.Join(home, "proj", ".env.local"),
		filepath.Join(home, "proj", ".env.production"),
		filepath.Join(home, "proj", ".envrc"),
		filepath.Join(p.SoaHome, ".env"),
		"/proc/self/environ",
		"/proc/1/environ",
		"/dev/zero",
		"/dev/urandom",
	} {
		if err := p.CheckRead(abs); err == nil {
			t.Errorf("CheckRead(%q): want blocked", abs)
		}
	}
	for _, abs := range []string{
		filepath.Join(home, "proj", "main.go"),
		filepath.Join(home, "proj", ".env.example"),
		filepath.Join(p.SoaHome, "config.toml"),
		"/etc/hosts",
	} {
		if err := p.CheckRead(abs); err != nil {
			t.Errorf("CheckRead(%q) = %v, want allowed", abs, err)
		}
	}
}

func TestWorkspaceIsTheGitRootElseTheDirectory(t *testing.T) {
	root := t.TempDir()
	repo := filepath.Join(root, "repo")
	sub := filepath.Join(repo, "pkg", "sub")
	plain := filepath.Join(root, "plain", "dir")
	for _, d := range []string{filepath.Join(repo, ".git"), sub, plain} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	for dir, want := range map[string]string{repo: repo, sub: repo, plain: plain} {
		if got := paths.Workspace(dir); got != want {
			t.Errorf("Workspace(%s) = %s, want %s", dir, got, want)
		}
	}
}
