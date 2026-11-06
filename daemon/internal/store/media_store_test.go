package store_test

// Images: content-addressed media, the images column on messages and queue
// items, and pruning what nothing references.

import (
	"errors"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func TestMediaRoundTripsAndDedupes(t *testing.T) {
	st := newStore(t)
	img, err := st.PutMedia(ctx, "image/png", "shot.png", []byte("PNGDATA"))
	if err != nil {
		t.Fatal(err)
	}
	if img.ID == "" || img.Mime != "image/png" || img.Name != "shot.png" {
		t.Fatalf("image = %+v", img)
	}
	again, err := st.PutMedia(ctx, "image/png", "shot.png", []byte("PNGDATA"))
	if err != nil || again.ID != img.ID {
		t.Fatalf("the same bytes did not reuse the row: %+v %v", again, err)
	}
	if b, err := st.Media(ctx, img.ID); err != nil || string(b) != "PNGDATA" {
		t.Fatalf("Media = %q, %v", b, err)
	}
	if _, err := st.Media(ctx, "nope"); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("an unknown id = %v", err)
	}
}

func TestMessagesCarryImageReferences(t *testing.T) {
	st := newStore(t)
	sess, err := st.CreateSession(ctx, store.Session{Kind: "interactive", Source: "test", Cwd: "/w"})
	if err != nil {
		t.Fatal(err)
	}
	img, err := st.PutMedia(ctx, "image/png", "", []byte("P1"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.AppendMessages(ctx, sess.ID,
		store.Message{Role: "user", Content: "look", Images: []store.Image{img}},
		store.Message{Role: "assistant", Content: "I see it"},
		store.Message{Role: "tool", ToolCallID: "c1", ToolName: "read_file", Content: "{}", Images: []store.Image{img}},
	); err != nil {
		t.Fatal(err)
	}
	msgs, err := st.Messages(ctx, sess.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(msgs) != 3 || len(msgs[0].Images) != 1 || msgs[0].Images[0].ID != img.ID ||
		len(msgs[1].Images) != 0 || len(msgs[2].Images) != 1 {
		t.Fatalf("messages = %+v", msgs)
	}
	// A message row keeps its image alive; nothing else does not.
	if err := st.PruneMedia(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Media(ctx, img.ID); err != nil {
		t.Fatalf("a referenced image was pruned: %v", err)
	}
}

func TestQueueItemsCarryImages(t *testing.T) {
	st := newStore(t)
	sess, err := st.CreateSession(ctx, store.Session{Kind: "interactive", Source: "test", Cwd: "/w"})
	if err != nil {
		t.Fatal(err)
	}
	img, err := st.PutMedia(ctx, "image/jpeg", "pic.jpg", []byte("JPG"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.QueuePushAt(ctx, sess.ID, "user", "look at this", time.Now(), []store.Image{img}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.QueuePush(ctx, sess.ID, "notice", "plain"); err != nil {
		t.Fatal(err)
	}
	items, err := st.QueueItems(ctx, sess.ID)
	if err != nil || len(items) != 2 {
		t.Fatalf("items = %v, %v", items, err)
	}
	if len(items[0].Images) != 1 || items[0].Images[0].ID != img.ID || items[0].Images[0].Mime != "image/jpeg" {
		t.Fatalf("the queued images = %+v", items[0].Images)
	}
	if len(items[1].Images) != 0 {
		t.Fatalf("a text-only item carries images: %+v", items[1].Images)
	}
	// Removing the item frees the image: nothing references it any more.
	if err := st.QueueRemove(ctx, sess.ID, items[0].ID); err != nil {
		t.Fatal(err)
	}
	if err := st.PruneMedia(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Media(ctx, img.ID); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("an unreferenced image survived the prune: %v", err)
	}
}

func TestDeletingASessionFreesItsImages(t *testing.T) {
	st := newStore(t)
	keep, err := st.CreateSession(ctx, store.Session{Kind: "interactive", Source: "test", Cwd: "/w"})
	if err != nil {
		t.Fatal(err)
	}
	gone, err := st.CreateSession(ctx, store.Session{Kind: "interactive", Source: "test", Cwd: "/w"})
	if err != nil {
		t.Fatal(err)
	}
	a, err := st.PutMedia(ctx, "image/png", "", []byte("KEEP"))
	if err != nil {
		t.Fatal(err)
	}
	b, err := st.PutMedia(ctx, "image/png", "", []byte("DROP"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.AppendMessages(ctx, keep.ID, store.Message{Role: "user", Content: "x", Images: []store.Image{a}}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.AppendMessages(ctx, gone.ID, store.Message{Role: "user", Content: "x", Images: []store.Image{b}}); err != nil {
		t.Fatal(err)
	}
	if err := st.DeleteSessions(ctx, []string{gone.ID}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Media(ctx, a.ID); err != nil {
		t.Fatalf("the kept session's image was pruned: %v", err)
	}
	if _, err := st.Media(ctx, b.ID); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("the deleted session's image survived: %v", err)
	}
}
