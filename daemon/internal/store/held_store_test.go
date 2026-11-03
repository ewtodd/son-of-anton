package store_test

import (
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// Held messages keep their order per chat, a chat's question is stale once
// another message is held, and clearing leaves neither messages nor question.
func TestHeldMessagesPerChat(t *testing.T) {
	st := newStore(t)
	for _, m := range []struct{ chat, text string }{{"a", "1"}, {"b", "x"}, {"a", "2"}} {
		if err := st.HoldMessage(ctx, m.chat, store.Held{Sender: "s", Text: m.text, TS: 1}); err != nil {
			t.Fatal(err)
		}
	}
	got, _ := st.HeldMessages(ctx, "a")
	if len(got) != 2 || got[0].Text != "1" || got[1].Text != "2" {
		t.Fatalf("chat a holds %+v", got)
	}
	if chats, _ := st.HeldChats(ctx); len(chats) != 2 {
		t.Fatalf("HeldChats = %v", chats)
	}
	_ = st.SetHeldAsked(ctx, "a", true)
	if asked, _ := st.HeldAsked(ctx, "a"); !asked {
		t.Fatal("not asked after SetHeldAsked")
	}
	_ = st.HoldMessage(ctx, "a", store.Held{Text: "3"})
	if asked, _ := st.HeldAsked(ctx, "a"); asked {
		t.Fatal("a new held message left the old question open")
	}
	_ = st.SetHeldAsked(ctx, "a", true)
	_ = st.ClearHeld(ctx, "a")
	if got, _ := st.HeldMessages(ctx, "a"); len(got) != 0 {
		t.Fatalf("cleared chat holds %+v", got)
	}
	if asked, _ := st.HeldAsked(ctx, "a"); asked {
		t.Fatal("clearing left the question open")
	}
	if got, _ := st.HeldMessages(ctx, "b"); len(got) != 1 {
		t.Fatal("clearing a touched another chat")
	}
}
