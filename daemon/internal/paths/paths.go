// Package paths is the tool-side path policy: where a tool may read and
// write, resolved against the session cwd and the user's home.
package paths

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Policy is the path policy for one instance.
type Policy struct {
	Home    string // the user's home
	SoaHome string // the instance home (~/.son-of-anton)
}

// WriteCheck is the result of CheckWrite.
type WriteCheck struct {
	Denied    bool
	Protected bool // needs approval in every mode
	Reason    string
}

// Resolve turns a tool-supplied path into an absolute one: relative to cwd,
// `~` expands to Home, and symlinks are resolved on the longest existing
// prefix (a new file under a symlinked directory resolves to the real
// directory).
func (p Policy) Resolve(cwd, path string) (string, error) {
	if path == "" {
		return "", errors.New("path is required")
	}
	var abs string
	switch {
	case path == "~":
		abs = p.Home
	case strings.HasPrefix(path, "~/"):
		abs = filepath.Join(p.Home, path[2:])
	case filepath.IsAbs(path):
		abs = path
	default:
		abs = filepath.Join(cwd, path)
	}
	abs = filepath.Clean(abs)
	dir := abs
	for {
		real, err := filepath.EvalSymlinks(dir)
		if err == nil {
			if dir == abs {
				return real, nil
			}
			rest, relErr := filepath.Rel(dir, abs)
			if relErr != nil {
				return abs, nil
			}
			return filepath.Join(real, rest), nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return abs, nil
		}
		dir = parent
	}
}

// readBlockedBasenames are secret files, anywhere. .env.example and friends
// are not blocked.
var readBlockedBasenames = map[string]bool{
	".env":             true,
	".env.local":       true,
	".env.development": true,
	".env.production":  true,
	".env.test":        true,
	".env.staging":     true,
	".envrc":           true,
}

// procReadFiles are the sensitive per-pid /proc entries.
var procReadFiles = map[string]bool{
	"environ": true, "mem": true, "maps": true, "smaps": true,
	"auxv": true, "pagemap": true, "cmdline": true,
}

// CheckRead reports why a read is blocked; nil means allowed.
func (p Policy) CheckRead(abs string) error {
	if readBlockedBasenames[filepath.Base(abs)] {
		return fmt.Errorf("denied: %s is a secret file", filepath.Base(abs))
	}
	if abs == "/dev" || strings.HasPrefix(abs, "/dev/") {
		return errors.New("denied: /dev entries are not readable")
	}
	if procSensitive(abs) {
		return errors.New("denied: sensitive /proc entry")
	}
	return nil
}

func procSensitive(abs string) bool {
	parts := strings.Split(strings.Trim(abs, "/"), "/")
	if len(parts) < 3 || parts[0] != "proc" {
		return false
	}
	if parts[2] == "fd" && len(parts) > 3 {
		return true
	}
	return procReadFiles[parts[2]]
}

var (
	deniedHomeDirs     = []string{".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure"}
	deniedConfigDirs   = []string{"gh", "gcloud"}
	deniedHomeFiles    = map[string]bool{".netrc": true, ".pgpass": true, ".npmrc": true, ".pypirc": true, ".git-credentials": true}
	protectedBasenames = map[string]bool{"agents.md": true, "claude.md": true, "soul.md": true, ".cursorrules": true}
)

// CheckWrite classifies a write target: denied (never), protected (approval
// in every mode), or ordinary.
func (p Policy) CheckWrite(abs string) WriteCheck {
	if err := p.CheckRead(abs); err != nil {
		return WriteCheck{Denied: true, Reason: err.Error()}
	}
	base := filepath.Base(abs)
	if protectedBasenames[strings.ToLower(base)] {
		return WriteCheck{Protected: true, Reason: base + " is a protected instruction file; its changes always need approval"}
	}
	if p.SoaHome != "" && abs == filepath.Join(p.SoaHome, "memories", "rules.toml") {
		return WriteCheck{Protected: true, Reason: "memory rules always need the user's approval"}
	}
	if p.Home != "" {
		for _, d := range deniedHomeDirs {
			if under(abs, filepath.Join(p.Home, d)) {
				return WriteCheck{Denied: true, Reason: fmt.Sprintf("denied: inside %s (use the terminal with sudo if you must)", "~"+d)}
			}
		}
		for _, d := range deniedConfigDirs {
			if under(abs, filepath.Join(p.Home, ".config", d)) {
				return WriteCheck{Denied: true, Reason: fmt.Sprintf("denied: inside ~/.config/%s", d)}
			}
		}
		if deniedHomeFiles[base] {
			return WriteCheck{Denied: true, Reason: "denied: " + base + " holds credentials"}
		}
	}
	switch {
	case abs == "/etc" || strings.HasPrefix(abs, "/etc/"):
		return WriteCheck{Denied: true, Reason: "denied: /etc is system configuration (use the terminal with sudo if you must)"}
	case abs == "/boot" || strings.HasPrefix(abs, "/boot/"):
		return WriteCheck{Denied: true, Reason: "denied: /boot is system configuration (use the terminal with sudo if you must)"}
	case strings.HasPrefix(abs, "/usr/lib/systemd/"):
		return WriteCheck{Denied: true, Reason: "denied: /usr/lib/systemd is system configuration (use the terminal with sudo if you must)"}
	}
	if p.SoaHome != "" {
		switch {
		case base == ".env" || base == "config.toml":
			return WriteCheck{Denied: true, Reason: "denied: the instance home's " + base + " is managed outside the agent"}
		case strings.HasPrefix(base, "soa-d.db") || strings.HasPrefix(base, "state.db"):
			return WriteCheck{Denied: true, Reason: "denied: " + base + " is the daemon's database"}
		case under(abs, filepath.Join(p.SoaHome, "sessions")),
			under(abs, filepath.Join(p.SoaHome, "pairing")):
			return WriteCheck{Denied: true, Reason: "denied: inside the instance home's " + filepath.Base(abs) + " directory"}
		case abs == filepath.Join(p.SoaHome, ".nix-managed.json"),
			abs == filepath.Join(p.SoaHome, "memories", ".nix-rules.json"):
			return WriteCheck{Denied: true, Reason: "denied: " + base + " is Nix-managed"}
		}
	}
	return WriteCheck{}
}

// under reports whether abs is dir or inside it.
func under(abs, dir string) bool {
	return abs == dir || strings.HasPrefix(abs, dir+string(filepath.Separator))
}

// Workspace is the directory a session belongs to for -c: the nearest
// ancestor of dir (inclusive) holding .git, else dir itself.
func Workspace(dir string) string {
	dir = filepath.Clean(dir)
	for d := dir; ; d = filepath.Dir(d) {
		if _, err := os.Stat(filepath.Join(d, ".git")); err == nil {
			return d
		}
		if filepath.Dir(d) == d {
			return dir
		}
	}
}
