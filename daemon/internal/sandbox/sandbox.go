// Package sandbox turns a command into a bubblewrap invocation. A physics
// session's commands run inside it: it sees the run directory (read-write),
// the data directories (read-only), the Nix store and the runtime's bin/, and
// has no network and none of the rest of the host.
//
// It fails closed. With no bwrap, or when bwrap cannot create its
// namespaces, no command is built and the error says why; nothing here ever
// falls back to running a command unsandboxed.
package sandbox

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
)

// StorePath is the Nix store, always bound read-only.
const StorePath = "/nix/store"

// Policy is what a sandboxed command may reach.
type Policy struct {
	// RunDir is read-write, bound at its own path.
	RunDir string
	// Data directories are read-only, each bound at its own path.
	Data []string
	// Binds are extra read-write paths, bound at their own paths (the
	// session's private temp dir).
	Binds []string
	// Runtime is the runtime's bin/ directory: the whole PATH.
	Runtime string
	// Home is the commands' HOME, normally RunDir/.home.
	Home string
}

// UnavailableError says why no sandbox can be built; its message starts with
// "the sandbox is unavailable".
type UnavailableError struct{ Reason string }

func (e *UnavailableError) Error() string {
	return "the sandbox is unavailable, so nothing was run: " + e.Reason
}

// ReadRoots are the paths a session's file tools may read.
func (p Policy) ReadRoots() []string {
	roots := []string{p.RunDir}
	roots = append(roots, p.Data...)
	return append(roots, StorePath)
}

// WriteRoots are the paths a session's file tools may write.
func (p Policy) WriteRoots() []string { return []string{p.RunDir} }

// clean resolves path (absolute, not the filesystem root) through symlinks.
func clean(what, path string) (string, error) {
	if path == "" || !filepath.IsAbs(path) {
		return "", &UnavailableError{what + " must be an absolute path, got " + fmt.Sprintf("%q", path)}
	}
	real, err := filepath.EvalSymlinks(filepath.Clean(path))
	if err != nil {
		return "", &UnavailableError{fmt.Sprintf("%s %s: %v", what, path, err)}
	}
	if real == "/" {
		return "", &UnavailableError{what + " may not be the filesystem root"}
	}
	return real, nil
}

// Args is the bwrap argument list (without the program name) that runs argv
// in the sandbox. extraEnv are KEY=VALUE pairs set in the command's
// environment first; the sandbox's own variables win over them. Every path of
// the policy must exist; each is bound at its symlink-resolved path.
func (p Policy) Args(extraEnv []string, argv ...string) ([]string, error) {
	if len(argv) == 0 {
		return nil, &UnavailableError{"no command"}
	}
	runDir, err := clean("the run directory", p.RunDir)
	if err != nil {
		return nil, err
	}
	runtime, err := clean("the runtime", p.Runtime)
	if err != nil {
		return nil, err
	}
	if p.Home == "" || !filepath.IsAbs(p.Home) {
		return nil, &UnavailableError{"the home must be an absolute path"}
	}
	home := filepath.Clean(p.Home)
	if home != runDir && !strings.HasPrefix(home, runDir+"/") {
		// The home is writable only because it lies inside the run directory.
		return nil, &UnavailableError{"the home " + home + " is not inside the run directory"}
	}
	if err := os.MkdirAll(home, 0o700); err != nil {
		return nil, &UnavailableError{"the home: " + err.Error()}
	}

	a := []string{
		"--die-with-parent", "--new-session", "--unshare-all", "--clearenv",
		"--ro-bind", StorePath, StorePath,
		"--proc", "/proc",
		"--dev", "/dev",
		"--tmpfs", "/tmp",
		"--symlink", filepath.Join(runtime, "sh"), "/bin/sh",
		"--symlink", filepath.Join(runtime, "env"), "/usr/bin/env",
		"--ro-bind-try", "/etc/passwd", "/etc/passwd",
		"--ro-bind-try", "/etc/group", "/etc/group",
		"--ro-bind-try", "/etc/localtime", "/etc/localtime",
	}
	if runtime != StorePath && !strings.HasPrefix(runtime, StorePath+"/") {
		a = append(a, "--ro-bind", runtime, runtime)
	}
	for _, d := range p.Data {
		real, err := clean("a data directory", d)
		if err != nil {
			return nil, err
		}
		if err := noOverlap(real, runDir); err != nil {
			return nil, err
		}
		a = append(a, "--ro-bind", real, real)
	}
	a = append(a, "--bind", runDir, runDir)
	for _, b := range p.Binds {
		real, err := clean("a bind", b)
		if err != nil {
			return nil, err
		}
		a = append(a, "--bind", real, real)
	}
	for _, kv := range extraEnv {
		k, v, ok := strings.Cut(kv, "=")
		if !ok || k == "" {
			return nil, &UnavailableError{fmt.Sprintf("bad environment entry %q", kv)}
		}
		a = append(a, "--setenv", k, v)
	}
	a = append(a,
		"--setenv", "PATH", runtime,
		"--setenv", "HOME", home,
		"--setenv", "TMPDIR", "/tmp",
		"--setenv", "LANG", "C.UTF-8",
		"--setenv", "MPLBACKEND", "Agg",
		"--chdir", runDir,
		"--")
	return append(a, argv...), nil
}

var (
	probeMu sync.Mutex
	probed  = map[string]bool{}
)

// Command builds the exec.Cmd that runs argv in the sandbox. It fails closed:
// with no bwrap on PATH, or when bwrap cannot create its namespaces here, it
// returns an *UnavailableError and nothing runs. argv[0] should be an
// absolute path inside the runtime (bwrap resolves it before PATH is set).
func (p Policy) Command(extraEnv []string, argv ...string) (*exec.Cmd, error) {
	return p.CommandContext(context.Background(), extraEnv, argv...)
}

// CommandContext is Command with a context that kills the process when done.
func (p Policy) CommandContext(ctx context.Context, extraEnv []string, argv ...string) (*exec.Cmd, error) {
	bwrap, err := exec.LookPath("bwrap")
	if err != nil {
		return nil, &UnavailableError{"bwrap is not on PATH"}
	}
	args, err := p.Args(extraEnv, argv...)
	if err != nil {
		return nil, err
	}
	if err := p.probe(bwrap); err != nil {
		return nil, err
	}
	return exec.CommandContext(ctx, bwrap, args...), nil
}

// Bash is the shell the sandbox runs wrappers with.
func (p Policy) Bash() string { return filepath.Join(p.Runtime, "bash") }

// probe checks, once per bwrap and runtime, that bwrap can set the sandbox up
// here (user namespaces can be disabled) by running a no-op in it.
func (p Policy) probe(bwrap string) error {
	key := bwrap + "\x00" + p.RunDir + "\x00" + p.Runtime + "\x00" + p.Home + "\x00" + strings.Join(p.Data, "\x01") + "\x00" + strings.Join(p.Binds, "\x01")
	probeMu.Lock()
	ok := probed[key]
	probeMu.Unlock()
	if ok {
		return nil
	}
	args, err := p.Args(nil, p.Bash(), "-c", ":")
	if err != nil {
		return err
	}
	out, err := exec.Command(bwrap, args...).CombinedOutput()
	if err != nil {
		msg := strings.TrimSpace(string(out))
		if msg == "" {
			msg = err.Error()
		}
		var ee *exec.ExitError
		if !errors.As(err, &ee) {
			msg = "could not run bwrap: " + err.Error()
		}
		return &UnavailableError{"bwrap could not set the sandbox up: " + msg}
	}
	probeMu.Lock()
	probed[key] = true
	probeMu.Unlock()
	return nil
}

// noOverlap refuses a data directory that is, contains or lies inside the run
// directory: the read-only and read-write binds would shadow one another.
func noOverlap(data, runDir string) error {
	if data == runDir || strings.HasPrefix(data, runDir+"/") || strings.HasPrefix(runDir, data+"/") {
		return &UnavailableError{fmt.Sprintf("the data directory %s overlaps the run directory %s", data, runDir)}
	}
	return nil
}
