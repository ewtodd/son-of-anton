package store_test

import (
	"crypto/sha256"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func TestReadStampsRoundTrip(t *testing.T) {
	st := newStore(t)
	sum := sha256.Sum256([]byte("note text"))
	want := map[string]store.ReadStamp{
		"/home/u/notes/a.md": {Path: "/home/u/notes/a.md", Size: 9, Sum: sum[:], Lines: 3,
			Seen: []store.SeenRange{{Start: 0, End: 3}}, Whole: false},
		"/home/u/notes/b.txt": {Path: "/home/u/notes/b.txt", Size: 0, Sum: nil, Lines: 0,
			Seen: nil, Whole: true},
	}
	if err := st.PutReadStamps(ctx, "session-a", want); err != nil {
		t.Fatalf("PutReadStamps: %v", err)
	}
	got, err := st.ReadStamps(ctx, "session-a")
	if err != nil {
		t.Fatalf("ReadStamps: %v", err)
	}
	if len(got) != len(want) {
		t.Fatalf("got %d stamps, want %d", len(got), len(want))
	}
	for path, w := range want {
		g, ok := got[path]
		if !ok {
			t.Fatalf("missing stamp for %s", path)
		}
		if g.Size != w.Size || g.Lines != w.Lines || g.Whole != w.Whole || len(g.Seen) != len(w.Seen) {
			t.Fatalf("stamp %s = %+v, want %+v", path, g, w)
		}
		if string(g.Sum) != string(w.Sum) {
			t.Fatalf("stamp %s hash mismatch", path)
		}
	}
	// Writing a smaller set replaces the old one.
	if err := st.PutReadStamps(ctx, "session-a", map[string]store.ReadStamp{"x": {Path: "x", Size: 1, Sum: sum[:], Lines: 1, Whole: true}}); err != nil {
		t.Fatalf("PutReadStamps (second): %v", err)
	}
	got, err = st.ReadStamps(ctx, "session-a")
	if err != nil || len(got) != 1 {
		t.Fatalf("after replace: %v, %d stamps", err, len(got))
	}
}

func TestReadStampsArePerSession(t *testing.T) {
	st := newStore(t)
	sum := sha256.Sum256([]byte("x"))
	if err := st.PutReadStamps(ctx, "s1", map[string]store.ReadStamp{"p": {Path: "p", Size: 1, Sum: sum[:], Lines: 1, Whole: true}}); err != nil {
		t.Fatal(err)
	}
	got, err := st.ReadStamps(ctx, "s2")
	if err != nil || len(got) != 0 {
		t.Fatalf("another session must have no stamps: %v, %d", err, len(got))
	}
}
