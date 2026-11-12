package tools

import (
	"crypto/sha256"
	"os"
	"path/filepath"
	"sync"
	"syscall"
)

// readStamp records which file Read saw and which of its lines, so Write and
// Edit can refuse a file this session has not read, or one that changed on
// disk since. seen is the line ranges read while the file held these bytes;
// whole is set when there are no lines to count (an image, an empty file) or
// the session wrote the file itself.
type readStamp struct {
	size  int64
	sum   [sha256.Size]byte
	lines int
	seen  []lineRange
	whole bool
}

// lineRange is a half-open range of 0-based line indexes.
type lineRange struct{ start, end int }

// covers says whether the session has seen every line of the file.
func (st readStamp) covers() bool {
	if st.whole {
		return true
	}
	next := 0
	for _, r := range st.seen {
		if r.start > next {
			return false
		}
		next = max(next, r.end)
	}
	return next >= st.lines
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
	onNote  func() // called, with no lock held, after a notice is queued
	onExit  func() // called, with no lock held, after a background process exits
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
	// Resolved, so a sandbox binds it at the path the snapshot is written to.
	if real, err := filepath.EvalSymlinks(dir); err == nil {
		dir = real
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

// TempDir is the session's private temp dir ("" once closed).
func (s *State) TempDir() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.tempDir
}

// SnapshotPath is the shell environment snapshot inside the private temp dir.
func (s *State) SnapshotPath() string {
	return filepath.Join(s.tempDir, "env.sh")
}

func (s *State) readStamp(path string) (readStamp, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	st, ok := s.stamps[path]
	return st, ok
}

// recordRead adds lines [start, end) of the file st stamps to what the session
// has seen of path. A stamp of other bytes than the recorded one starts over.
func (s *State) recordRead(path string, st readStamp, start, end int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if old, ok := s.stamps[path]; ok && old.sum == st.sum && old.size == st.size {
		st.seen, st.whole = old.seen, old.whole || st.whole
	}
	if end > start {
		st.seen = mergeRange(st.seen, lineRange{start, end})
	}
	s.stamps[path] = st
}

func (s *State) setStamp(path string, st readStamp) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.stamps[path] = st
}

// mergeRange inserts r into rs, which is sorted and disjoint, joining the
// ranges r overlaps or touches.
func mergeRange(rs []lineRange, r lineRange) []lineRange {
	out := make([]lineRange, 0, len(rs)+1)
	placed := false
	for _, x := range rs {
		switch {
		case x.end < r.start:
			out = append(out, x)
		case r.end < x.start:
			if !placed {
				out = append(out, r)
				placed = true
			}
			out = append(out, x)
		default:
			r = lineRange{min(r.start, x.start), max(r.end, x.end)}
		}
	}
	if !placed {
		out = append(out, r)
	}
	return out
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

// SetNotify registers f to run (on any goroutine, with no State lock held)
// each time a completion notice is queued.
func (s *State) SetNotify(f func()) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.onNote = f
}

// SetOnExit registers f to run (on any goroutine, with no State lock held)
// each time a background process exits, killed or not.
func (s *State) SetOnExit(f func()) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.onExit = f
}

// BackgroundRunning counts the background processes still running.
func (s *State) BackgroundRunning() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for _, p := range s.procs {
		if !p.exited {
			n++
		}
	}
	return n
}

// KillBackground kills the running background processes and returns how many.
// They were killed on purpose, so they queue no completion notice.
func (s *State) KillBackground() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for _, p := range s.procs {
		if !p.exited {
			p.consumed, p.killed = true, true
			p.signal(syscall.SIGKILL)
			n++
		}
	}
	return n
}

// Notify queues a completion notice and tells the notify callback.
func (s *State) Notify(text string) {
	s.mu.Lock()
	s.notes = append(s.notes, note{"", text})
	cb := s.onNote
	s.mu.Unlock()
	if cb != nil {
		cb()
	}
}
