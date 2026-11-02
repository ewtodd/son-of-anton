package store_test

import (
	"fmt"
	"sync"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// A turn appends messages while a background title write lands on the same
// session (phase 3a). No append may fail: a lost row is a lost message.
func TestAppendsSurviveConcurrentWrites(t *testing.T) {
	st := newStore(t)
	s, _ := st.CreateSession(ctx, store.Session{Kind: "interactive"})
	var wg sync.WaitGroup
	errs := make(chan error, 400)
	wg.Add(2)
	go func() {
		defer wg.Done()
		for i := range 200 {
			if _, err := st.AppendMessages(ctx, s.ID, store.Message{Role: "user", Content: fmt.Sprint(i)}); err != nil {
				errs <- fmt.Errorf("append %d: %w", i, err)
			}
		}
	}()
	go func() {
		defer wg.Done()
		for i := range 200 {
			if _, err := st.SetTitle(ctx, s.ID, fmt.Sprintf("title %d", i), "user"); err != nil {
				errs <- fmt.Errorf("title %d: %w", i, err)
			}
		}
	}()
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}
	if rows, _ := st.Messages(ctx, s.ID); len(rows) != 200 {
		t.Fatalf("rows = %d, want 200", len(rows))
	}
}
