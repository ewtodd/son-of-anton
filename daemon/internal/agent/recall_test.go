package agent_test

import (
	"context"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

// Phase 3b: what Runner.Recall returns for a turn's text rides that user
// message's api_content (text, a blank line, the block); the transcript keeps
// the text alone, and the stored bytes replay unchanged on every later
// request, whatever recall would say by then.
func TestRecallRidesTheUserMessagesAPIContent(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t, fakellm.Turn{Content: "one"}, fakellm.Turn{Content: "two"}, fakellm.Turn{Content: "three"})
	r := runner(st, fake, "sys", nil)
	block := "<recalled-notes>\nfirst block\n</recalled-notes>"
	var asked []string
	r.Recall = func(_ context.Context, text string) string {
		asked = append(asked, text)
		return block
	}
	s, _ := r.NewSession(ctx, "interactive", "/w")
	send(t, s, "first question")
	rows, _ := st.Messages(ctx, s.ID())
	if rows[0].Content != "first question" || rows[0].APIContent != "first question\n\n"+block {
		t.Fatalf("stored user row = %+v", rows[0])
	}
	if got := decode(t, fake.Messages(0)); got[1].Content != "first question\n\n"+block {
		t.Fatalf("sent = %q", got[1].Content)
	}
	block = "<recalled-notes>\nchanged since\n</recalled-notes>"
	send(t, s, "second")
	if string(fake.Messages(1)[1]) != string(fake.Messages(0)[1]) {
		t.Fatal("an earlier user message replays as it was first sent")
	}
	block = ""
	send(t, s, "third")
	rows, _ = st.Messages(ctx, s.ID())
	if last := rows[len(rows)-2]; last.APIContent != "third" && last.APIContent != "" {
		t.Fatalf("nothing recalled leaves the text alone: %+v", last)
	}
	if len(asked) != 3 || asked[0] != "first question" || asked[2] != "third" {
		t.Fatalf("Recall was asked about %q", asked)
	}
}
