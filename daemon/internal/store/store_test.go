package store_test

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"path/filepath"
	"regexp"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func open(t *testing.T, path string) *store.Store {
	t.Helper()
	st, err := store.Open(path)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return st
}

func newStore(t *testing.T) *store.Store {
	return open(t, filepath.Join(t.TempDir(), "soa-d.db"))
}

var ctx = context.Background()

func TestOpenIsIdempotent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "soa-d.db")
	a, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	v1, err := a.SchemaVersion()
	if err != nil || v1 < 1 {
		t.Fatalf("SchemaVersion = %d, %v", v1, err)
	}
	_ = a.Close()
	b := open(t, path)
	if v2, _ := b.SchemaVersion(); v2 != v1 {
		t.Fatalf("reopen changed the schema version: %d -> %d", v1, v2)
	}
}

func TestSessionRoundTrip(t *testing.T) {
	st := newStore(t)
	created, err := st.CreateSession(ctx, store.Session{
		Kind: "oneshot", Source: "query", Cwd: "/work", Model: "auto", PromptHash: "abc",
	})
	if err != nil {
		t.Fatal(err)
	}
	if !regexp.MustCompile(`^\d{8}_\d{6}_[0-9a-f]{6}$`).MatchString(created.ID) {
		t.Fatalf("session id %q, want YYYYmmdd_HHMMSS_xxxxxx", created.ID)
	}
	if created.Created.IsZero() {
		t.Fatal("Created not set")
	}
	got, err := st.Session(ctx, created.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.ID != created.ID || got.Kind != "oneshot" || got.Source != "query" || got.Cwd != "/work" ||
		got.Model != "auto" || got.PromptHash != "abc" || !got.Created.Equal(created.Created) {
		t.Fatalf("got %+v, want %+v", got, created)
	}
}

func TestSessionIDsAreUnique(t *testing.T) {
	st := newStore(t)
	seen := map[string]bool{}
	for range 50 {
		s, err := st.CreateSession(ctx, store.Session{Kind: "oneshot"})
		if err != nil {
			t.Fatal(err)
		}
		if seen[s.ID] {
			t.Fatalf("duplicate session id %s", s.ID)
		}
		seen[s.ID] = true
	}
}

func TestUnknownSessionIsNotFound(t *testing.T) {
	st := newStore(t)
	if _, err := st.Session(ctx, "20260101_000000_000000"); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("err = %v, want store.ErrNotFound", err)
	}
}

func TestPromptsAreStoredOnceByHash(t *testing.T) {
	st := newStore(t)
	sum := sha256.Sum256([]byte("system prompt"))
	want := hex.EncodeToString(sum[:])
	for range 2 {
		h, err := st.PutPrompt(ctx, "system prompt")
		if err != nil || h != want {
			t.Fatalf("PutPrompt = %q, %v; want sha256 hex %q", h, err, want)
		}
	}
	text, err := st.Prompt(ctx, want)
	if err != nil || text != "system prompt" {
		t.Fatalf("Prompt = %q, %v", text, err)
	}
}

func TestMessagesKeepOrderAndEveryField(t *testing.T) {
	st := newStore(t)
	s, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	calls := json.RawMessage(`[{"id":"c1","type":"function","function":{"name":"terminal","arguments":"{}"}}]`)
	in := []store.Message{
		{Role: "user", Content: "list files", APIContent: "list files\n\n<recalled-notes>x</recalled-notes>"},
		{Role: "assistant", Content: "", Reasoning: "need ls", ToolCalls: calls},
		{Role: "tool", Content: "a b c", ToolCallID: "c1", ToolName: "terminal"},
		{Role: "assistant", Content: "three files"},
	}
	for _, m := range in {
		if _, err := st.AppendMessages(ctx, s.ID, m); err != nil {
			t.Fatal(err)
		}
	}
	got, err := st.Messages(ctx, s.ID)
	if err != nil || len(got) != len(in) {
		t.Fatalf("Messages = %d, %v", len(got), err)
	}
	for i, m := range got {
		w := in[i]
		if m.Seq != int64(i+1) || m.Role != w.Role || m.Content != w.Content || m.APIContent != w.APIContent ||
			m.Reasoning != w.Reasoning || m.ToolCallID != w.ToolCallID || m.ToolName != w.ToolName {
			t.Fatalf("message %d = %+v, want %+v", i, m, w)
		}
		if string(m.ToolCalls) != string(w.ToolCalls) {
			t.Fatalf("message %d tool calls = %s, want %s", i, m.ToolCalls, w.ToolCalls)
		}
		if m.TS.IsZero() {
			t.Fatalf("message %d has no timestamp", i)
		}
	}
}

func TestAppendMessagesIsOneTransactionAndReturnsSeqs(t *testing.T) {
	st := newStore(t)
	s, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	seqs, err := st.AppendMessages(ctx, s.ID,
		store.Message{Role: "user", Content: "q"},
		store.Message{Role: "assistant", Content: "a"},
	)
	if err != nil || len(seqs) != 2 || seqs[0] != 1 || seqs[1] != 2 {
		t.Fatalf("seqs = %v, %v", seqs, err)
	}
	// A batch for a session that does not exist writes nothing.
	if _, err := st.AppendMessages(ctx, "20260101_000000_000000", store.Message{Role: "user", Content: "x"}); err == nil {
		t.Fatal("appending to an unknown session must fail")
	}
}

func TestSeqIsPerSession(t *testing.T) {
	st := newStore(t)
	a, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	b, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	_, _ = st.AppendMessages(ctx, a.ID, store.Message{Role: "user", Content: "1"}, store.Message{Role: "assistant", Content: "2"})
	seqs, _ := st.AppendMessages(ctx, b.ID, store.Message{Role: "user", Content: "1"})
	if len(seqs) != 1 || seqs[0] != 1 {
		t.Fatalf("second session starts at %v, want 1", seqs)
	}
}

func TestSearchFindsMessages(t *testing.T) {
	st := newStore(t)
	s, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	_, _ = st.AppendMessages(ctx, s.ID,
		store.Message{Role: "user", Content: "where is the backup server"},
		store.Message{Role: "assistant", Content: "the backup server is unholy"},
	)
	hits, err := st.Search(ctx, "unholy", 5)
	if err != nil || len(hits) != 1 || hits[0].SessionID != s.ID || hits[0].Seq != 2 {
		t.Fatalf("hits = %+v, %v", hits, err)
	}
	if hits, _ := st.Search(ctx, "zebra", 5); len(hits) != 0 {
		t.Fatalf("unrelated query hit %+v", hits)
	}
}

func TestDataSurvivesReopen(t *testing.T) {
	path := filepath.Join(t.TempDir(), "soa-d.db")
	a, _ := store.Open(path)
	s, _ := a.CreateSession(ctx, store.Session{Kind: "interactive"})
	_, _ = a.AppendMessages(ctx, s.ID, store.Message{Role: "user", Content: "persist me"})
	_ = a.Close()
	b := open(t, path)
	msgs, err := b.Messages(ctx, s.ID)
	if err != nil || len(msgs) != 1 || msgs[0].Content != "persist me" {
		t.Fatalf("after reopen: %+v, %v", msgs, err)
	}
}
