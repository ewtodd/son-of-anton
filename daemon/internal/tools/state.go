package tools

import (
	"os"
	"path/filepath"
	"sync"
	"syscall"
)

// readStamp records what a read_file saw, so a later write_file can warn when
// the file changed underneath the session.
type readStamp struct {
	mtime   int64
	size    int64
	partial bool
}

// State is a session's mutable tool state: the session cwd, a private 0700
// temp dir holding the shell's environment snapshot, and per-path read
// stamps. It is safe for concurrent use.
type State struct {
	mu      sync.Mutex
	cwd     string
	tempDir string
	stamps  map[string]readStamp
	closed  bool
	procs   map[string]*bgProc
	notes   []note
	todos   []todoItem
}

// NewState creates the per-session state. The cwd need not exist yet.
func NewState(cwd string) (*State, error) {
	dir, err := os.MkdirTemp("", "soa-d-session-")
	if err != nil {
		return nil, err
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		_ = os.RemoveAll(dir)
		return nil, err
	}
	return &State{cwd: cwd, tempDir: dir, stamps: map[string]readStamp{}, procs: map[string]*bgProc{}}, nil
}

// Cwd is the session's current working directory.
func (s *State) Cwd() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cwd
}

// SetCwd updates the session's working directory.
func (s *State) SetCwd(cwd string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cwd = cwd
}

// SnapshotPath is the shell environment snapshot inside the private temp dir.
func (s *State) SnapshotPath() string {
	return filepath.Join(s.tempDir, "env.sh")
}

// ReadStamp returns the recorded read for a path.
func (s *State) ReadStamp(path string) (readStamp, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	st, ok := s.stamps[path]
	return st, ok
}

// SetReadStamp records a read (partial when the read did not cover the file).
func (s *State) SetReadStamp(path string, st readStamp) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.stamps[path] = st
}

// Close kills the session's background processes and removes its private temp
// dir.
func (s *State) Close() error {
	s.mu.Lock()
	dir := s.tempDir
	s.tempDir = ""
	s.closed = true
	for _, p := range s.procs {
		p.signal(syscall.SIGKILL)
	}
	s.mu.Unlock()
	if dir == "" {
		return nil
	}
	return os.RemoveAll(dir)
}
