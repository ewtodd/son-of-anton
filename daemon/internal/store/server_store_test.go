package store_test

// Phase 2a store additions for the daemon's server: a durable per-session
// queue of pending sends, a session list for resuming, paged history, and
// per-session settings that survive a restart.

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func texts(items []store.QueueItem) []string {
	out := make([]string, len(items))
	for i, it := range items {
		out[i] = it.Text
	}
	return out
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestTheQueueIsOrderedPerSessionAndDurable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "soa-d.db")
	st, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	a, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	b, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	var ids []string
	for _, text := range []string{"one", "two", "three"} {
		it, err := st.QueuePush(ctx, a.ID, "user", text)
		if err != nil || it.ID == "" || it.Kind != "user" || it.Text != text || it.Created.IsZero() {
			t.Fatalf("QueuePush = %+v, %v", it, err)
		}
		ids = append(ids, it.ID)
	}
	if _, err := st.QueuePush(ctx, b.ID, "notice", "other session"); err != nil {
		t.Fatal(err)
	}
	if err := st.QueueRemove(ctx, a.ID, ids[1], "no-such-id"); err != nil {
		t.Fatalf("QueueRemove ignores unknown ids: %v", err)
	}
	_ = st.Close()

	st = open(t, path)
	items, err := st.QueueItems(ctx, a.ID)
	if err != nil || !equal(texts(items), []string{"one", "three"}) {
		t.Fatalf("after a reopen the queue is %v, %v; want [one three] in push order", texts(items), err)
	}
	if other, _ := st.QueueItems(ctx, b.ID); len(other) != 1 || other[0].Kind != "notice" {
		t.Fatalf("each session has its own queue: %+v", other)
	}
	if err := st.QueueClear(ctx, a.ID); err != nil {
		t.Fatal(err)
	}
	if items, _ := st.QueueItems(ctx, a.ID); len(items) != 0 {
		t.Fatalf("QueueClear left %v", texts(items))
	}
	if other, _ := st.QueueItems(ctx, b.ID); len(other) != 1 {
		t.Fatal("QueueClear touches only its session")
	}
}

func TestListSessionsNewestActivityFirst(t *testing.T) {
	st := newStore(t)
	older, _ := st.CreateSession(ctx, store.Session{Kind: "interactive", Cwd: "/a"})
	if _, err := st.AppendMessages(ctx, older.ID,
		store.Message{Role: "user", Content: "first question about the build"},
		store.Message{Role: "assistant", Content: "answer"}); err != nil {
		t.Fatal(err)
	}
	newer, _ := st.CreateSession(ctx, store.Session{Kind: "interactive", Cwd: "/b"})
	if _, err := st.AppendMessages(ctx, newer.ID, store.Message{Role: "user", Content: "the newer turn"}); err != nil {
		t.Fatal(err)
	}
	child, _ := st.CreateSession(ctx, store.Session{Kind: "child", ParentID: newer.ID})
	shot, _ := st.CreateSession(ctx, store.Session{Kind: "oneshot"})
	if _, err := st.AppendMessages(ctx, shot.ID, store.Message{Role: "user", Content: "the shot"}); err != nil {
		t.Fatal(err)
	}
	_ = child // a zero-turn child is nobody's listing

	list, err := st.ListSessions(ctx, []string{"interactive"}, 10)
	if err != nil || len(list) != 2 || list[0].Session.ID != newer.ID || list[1].Session.ID != older.ID {
		t.Fatalf("ListSessions = %+v, %v; want [newer older]: top-level interactive only, by last activity", list, err)
	}
	if list[1].Preview != "first question about the build" || list[1].Messages != 2 || list[1].Updated.Before(list[1].Session.Created) {
		t.Fatalf("summary = %+v; preview is the first user message, Messages the row count", list[1])
	}

	// New activity moves a session to the front.
	if _, err := st.AppendMessages(ctx, older.ID, store.Message{Role: "user", Content: "again"}); err != nil {
		t.Fatal(err)
	}
	list, _ = st.ListSessions(ctx, []string{"interactive"}, 10)
	if list[0].Session.ID != older.ID {
		t.Fatalf("after new activity: %+v", list)
	}
	if one, _ := st.ListSessions(ctx, []string{"interactive", "oneshot"}, 1); len(one) != 1 {
		t.Fatalf("limit: %+v", one)
	}
	if both, _ := st.ListSessions(ctx, []string{"interactive", "oneshot"}, 10); len(both) != 3 {
		t.Fatalf("two kinds: %+v", both)
	}
}

func TestThePreviewIsCut(t *testing.T) {
	st := newStore(t)
	s, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	long := ""
	for range 50 {
		long += "word "
	}
	if _, err := st.AppendMessages(ctx, s.ID, store.Message{Role: "user", Content: "line one\n" + long}); err != nil {
		t.Fatal(err)
	}
	list, _ := st.ListSessions(ctx, []string{"interactive"}, 1)
	if p := list[0].Preview; len([]rune(p)) > 100 || p == "" || p[:8] != "line one" {
		t.Fatalf("preview = %q; at most 100 characters, from the start of the first user message", p)
	}
}

func seqs(ms []store.Message) []int64 {
	out := make([]int64, len(ms))
	for i, m := range ms {
		out[i] = m.Seq
	}
	return out
}

func sameSeqs(a []int64, b ...int64) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestHistoryPages(t *testing.T) {
	st := newStore(t)
	s, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	for i := range 10 {
		role := "user"
		if i%2 == 1 {
			role = "assistant"
		}
		if _, err := st.AppendMessages(ctx, s.ID, store.Message{Role: role, Content: "m"}); err != nil {
			t.Fatal(err)
		}
	}
	for _, c := range []struct {
		before    int64
		limit     int
		want      []int64
		wantMore  bool
		direction string
	}{
		{0, 3, []int64{8, 9, 10}, true, "before"},
		{8, 3, []int64{5, 6, 7}, true, "before"},
		{2, 5, []int64{1}, false, "before"},
		{0, 20, []int64{1, 2, 3, 4, 5, 6, 7, 8, 9, 10}, false, "before"},
		{7, 2, []int64{8, 9}, true, "after"},
		{8, 5, []int64{9, 10}, false, "after"},
		{10, 5, []int64{}, false, "after"},
	} {
		var got []store.Message
		var more bool
		var err error
		if c.direction == "before" {
			got, more, err = st.MessagesBefore(ctx, s.ID, c.before, c.limit)
		} else {
			got, more, err = st.MessagesAfter(ctx, s.ID, c.before, c.limit)
		}
		if err != nil || !sameSeqs(seqs(got), c.want...) || more != c.wantMore {
			t.Fatalf("Messages%s(%d, %d) = %v more=%v, %v; want %v more=%v (oldest first)",
				c.direction, c.before, c.limit, seqs(got), more, err, c.want, c.wantMore)
		}
	}
}

func TestSessionSettingsPersist(t *testing.T) {
	path := filepath.Join(t.TempDir(), "soa-d.db")
	st, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	s, _ := st.CreateSession(ctx, store.Session{Kind: "interactive", Model: "m1", ReasoningEffort: "high", Mode: "default"})
	if got, _ := st.Session(ctx, s.ID); got.ReasoningEffort != "high" || got.Mode != "default" {
		t.Fatalf("CreateSession keeps the settings: %+v", got)
	}
	if err := st.UpdateSessionSettings(ctx, s.ID, "m2", "low", "yolo"); err != nil {
		t.Fatal(err)
	}
	_ = st.Close()
	st = open(t, path)
	got, err := st.Session(ctx, s.ID)
	if err != nil || got.Model != "m2" || got.ReasoningEffort != "low" || got.Mode != "yolo" {
		t.Fatalf("after a reopen: %+v, %v", got, err)
	}
	if err := st.UpdateSessionSettings(ctx, "20260101_000000_000000", "m", "", ""); err == nil {
		t.Fatal("updating an unknown session is an error")
	}
}

// -c continues a session of this workspace only (the git root, or the
// directory itself), never another directory's conversation.
func TestSessionsListByWorkspace(t *testing.T) {
	st := newStore(t)
	a, _ := st.CreateSession(ctx, store.Session{Kind: "interactive", Cwd: "/repo/pkg", Workspace: "/repo"})
	b, _ := st.CreateSession(ctx, store.Session{Kind: "interactive", Cwd: "/repo", Workspace: "/repo"})
	away, _ := st.CreateSession(ctx, store.Session{Kind: "interactive", Cwd: "/elsewhere", Workspace: "/elsewhere"})
	for _, s := range []string{a.ID, b.ID, away.ID} {
		if _, err := st.AppendMessages(ctx, s, store.Message{Role: "user", Content: "started"}); err != nil {
			t.Fatal(err)
		}
	}
	list, err := st.ListSessionsIn(ctx, []string{"interactive"}, "/repo", 10)
	if err != nil || len(list) != 2 || list[0].Session.ID != b.ID || list[1].Session.ID != a.ID {
		t.Fatalf("ListSessionsIn(/repo) = %+v, %v", list, err)
	}
	if all, _ := st.ListSessionsIn(ctx, []string{"interactive"}, "", 10); len(all) != 3 {
		t.Fatalf("an empty workspace lists everything: %d", len(all))
	}
}

func TestTheDatabaseIsOwnerOnly(t *testing.T) {
	path := filepath.Join(t.TempDir(), "soa-d.db")
	if err := os.WriteFile(path, nil, 0o644); err != nil { // as an older soa-d left it
		t.Fatal(err)
	}
	st := open(t, path)
	s, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	if _, err := st.AppendMessages(ctx, s.ID, store.Message{Role: "user", Content: "private"}); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{path, path + "-wal"} {
		if fi, err := os.Stat(p); err != nil || fi.Mode().Perm() != 0o600 {
			t.Fatalf("%s: %v, %v; the database holds conversations and must be 0600", p, fi.Mode(), err)
		}
	}
}
