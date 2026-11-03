package agent_test

import (
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// A session stored without a prompt (imported from a store that kept none)
// resumes: its prompt is built then, stored, and kept from then on.
func TestASessionStoredWithoutAPromptResumes(t *testing.T) {
	st := newStore(t)
	if _, err := st.ImportSession(ctx, store.Session{ID: "20261003_185622_e2911d8e", Kind: "interactive", Cwd: t.TempDir(), Created: time.Now()}, "",
		[]store.Message{{Role: "user", Content: "old question", TS: time.Now()}, {Role: "assistant", Content: "old answer", TS: time.Now()}},
		store.ImportOpts{}); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t, fakellm.Turn{Content: "hello again"})
	calls := 0
	s, err := runner(st, fake, "the built prompt", &calls).Resume(ctx, "20261003_185622_e2911d8e")
	if err != nil {
		t.Fatalf("Resume: %v", err)
	}
	send(t, s, "still there?")
	s.Close()
	got := decode(t, fake.Messages(0))
	if calls != 1 || got[0].Role != "system" || got[0].Content != "the built prompt" || got[1].Content != "old question" {
		t.Fatalf("builder calls %d, request %+v", calls, got)
	}
	rec, _ := st.Session(ctx, "20261003_185622_e2911d8e")
	if p, err := st.Prompt(ctx, rec.PromptHash); err != nil || p != "the built prompt" {
		t.Fatalf("the built prompt is stored: %q, %v", p, err)
	}
	if _, err := runner(st, fake, "another prompt", &calls).Resume(ctx, "20261003_185622_e2911d8e"); err != nil || calls != 1 {
		t.Fatalf("a second resume reads the stored prompt: builder calls %d, %v", calls, err)
	}
}
