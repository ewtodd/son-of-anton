package tools

import (
	"crypto/sha256"
	"path/filepath"
	"testing"
)

// A chat's session is unloaded between turns; the read stamps must survive
// that, so a Read in one message still satisfies Edit in the next.
func TestReadStampsPersistAndRestore(t *testing.T) {
	dir := t.TempDir()
	a, err := NewState(dir)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte("movie log"))
	path := filepath.Join(dir, "log.md")
	a.recordRead(path, readStamp{size: 10, sum: sum, lines: 53}, 0, 53)
	snap := a.Stamps()
	if len(snap) != 1 {
		t.Fatalf("one stamp expected, got %d", len(snap))
	}
	// Another session (the resumed one) restores them.
	b, err := NewState(dir)
	if err != nil {
		t.Fatal(err)
	}
	b.RestoreStamps(snap)
	st, ok := b.readStamp(path)
	if !ok || st.size != 10 || st.lines != 53 || !st.covers() {
		t.Fatalf("restored stamp = %+v, %v", st, ok)
	}
	// A write replaces the stamp with the new bytes.
	b.setStamp(path, readStamp{size: 12, sum: sum, lines: 53, whole: true})
	snap = b.Stamps()
	if snap[path].Size != 12 || !snap[path].Whole {
		t.Fatalf("after written stamp: %+v", snap[path])
	}
}

func TestStateWithoutPersistIsSafe(t *testing.T) {
	s, err := NewState(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte("x"))
	s.recordRead("/p", readStamp{size: 1, sum: sum, lines: 1}, 0, 1)
	s.setStamp("/p", readStamp{size: 2, sum: sum, lines: 1, whole: true})
	if got := s.Stamps()["/p"]; got.Size != 2 {
		t.Fatalf("stamp = %+v", got)
	}
}
