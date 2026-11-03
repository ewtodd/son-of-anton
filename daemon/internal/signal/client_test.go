package signal_test

// Phase 5a: the signal-cli HTTP client, against internal/signalfake.

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/signal"
	"github.com/ewtodd/son-of-anton/daemon/internal/signalfake"
)

func TestSendTypingAndAttachments(t *testing.T) {
	fake := signalfake.New(t)
	c := signal.NewClient(fake.URL+"/", account)
	ctx := context.Background()
	if err := c.Check(ctx); err != nil {
		t.Fatal(err)
	}
	ts, err := c.Send(ctx, "group:gW", "bold and plain", []string{"0:4:BOLD"})
	if err != nil || ts == 0 {
		t.Fatalf("Send = %d, %v", ts, err)
	}
	ts2, _ := c.Send(ctx, member, "two styles", []string{"0:3:BOLD", "4:6:ITALIC"})
	if ts2 <= ts {
		t.Fatalf("timestamps %d then %d", ts, ts2)
	}
	_, _ = c.Send(ctx, member, "no style", nil)
	calls := fake.Calls("send")
	if len(calls) != 3 {
		t.Fatalf("sends = %v", calls)
	}
	if calls[0]["account"] != account || calls[0]["groupId"] != "gW" || calls[0]["recipient"] != nil ||
		calls[0]["message"] != "bold and plain" || calls[0]["textStyle"] != "0:4:BOLD" {
		t.Fatalf("group send = %v", calls[0])
	}
	if r, _ := calls[1]["recipient"].([]any); len(r) != 1 || r[0] != member || calls[1]["groupId"] != nil {
		t.Fatalf("a DM goes to recipient: %v", calls[1])
	}
	if s, _ := calls[1]["textStyles"].([]any); len(s) != 2 || calls[1]["textStyle"] != nil {
		t.Fatalf("several styles = %v", calls[1])
	}
	if _, has := calls[2]["textStyle"]; has {
		t.Fatalf("no style, no textStyle: %v", calls[2])
	}
	if _, has := calls[2]["textStyles"]; has {
		t.Fatalf("no style, no textStyles: %v", calls[2])
	}

	if err := c.Typing(ctx, "group:gW", false); err != nil {
		t.Fatal(err)
	}
	_ = c.Typing(ctx, "group:gW", true)
	ty := fake.Calls("sendTyping")
	if len(ty) != 2 || ty[0]["groupId"] != "gW" || ty[0]["stop"] == true || ty[1]["stop"] != true {
		t.Fatalf("typing = %v", ty)
	}

	fake.SetAttachment("a1", []byte("PNGDATA"), false)
	fake.SetAttachment("a2", []byte("WRAPPED"), true)
	if b, err := c.Attachment(ctx, "a1"); err != nil || string(b) != "PNGDATA" {
		t.Fatalf("Attachment = %q, %v", b, err)
	}
	if b, err := c.Attachment(ctx, "a2"); err != nil || string(b) != "WRAPPED" {
		t.Fatalf("a {data} attachment = %q, %v", b, err)
	}
	if _, err := c.Attachment(ctx, "nope"); err == nil {
		t.Fatal("an RPC error is an error")
	}
	fake.SetSendFails(true)
	if _, err := c.Send(ctx, member, "x", nil); err == nil {
		t.Fatal("a failed send is an error")
	}
}

func TestEventsReconnect(t *testing.T) {
	fake := signalfake.New(t)
	c := signal.NewClient(fake.URL, account)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var mu sync.Mutex
	var got []string
	done := make(chan error, 1)
	go func() {
		done <- c.Events(ctx, func(data []byte) {
			mu.Lock()
			got = append(got, string(data))
			mu.Unlock()
		})
	}()
	fake.Push(signalfake.DM(member, "first", 1))
	fake.WaitConnected(t)
	if fake.StreamAccount() != account {
		t.Fatalf("stream account = %q", fake.StreamAccount())
	}
	waitFor := func(n int) {
		t.Helper()
		deadline := time.Now().Add(10 * time.Second)
		for time.Now().Before(deadline) {
			mu.Lock()
			k := len(got)
			mu.Unlock()
			if k >= n {
				return
			}
			time.Sleep(10 * time.Millisecond)
		}
		t.Fatalf("got %d events, want %d", len(got), n)
	}
	waitFor(1)
	fake.DropStreams()
	fake.Push(signalfake.DM(member, "after the drop", 2))
	waitFor(2)
	if fake.Connects() < 2 {
		t.Fatalf("connects = %d; a dropped stream reconnects", fake.Connects())
	}
	mu.Lock()
	if !strings.Contains(got[0], "first") || !strings.Contains(got[1], "after the drop") {
		t.Fatalf("events = %v", got)
	}
	mu.Unlock()
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Events returns when its context ends")
	}
}
