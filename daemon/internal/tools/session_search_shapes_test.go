package tools_test

// What session_search owes a reader who has only its results: where a session
// ran, whether it is still running, and where a result stops short. Each test
// comes from a chat that went wrong without them.

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func browsedFirst(t *testing.T, sf *sessionFixture) map[string]any {
	t.Helper()
	rs, _ := sf.call(t, "session_search", map[string]any{})["results"].([]any)
	if len(rs) == 0 {
		t.Fatal("browse found nothing")
	}
	return rs[0].(map[string]any)
}

// A chat with no cwd guessed the repo's path, and one with no state called a
// live session interrupted.
func TestResultsSayWhereASessionRanAndWhetherItIsRunning(t *testing.T) {
	sf := sessionSetup(t)
	work, err := sf.st.CreateSession(ctx, store.Session{Kind: "interactive", Title: "Repo work", Cwd: "/srv/repo"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := sf.st.AppendMessages(ctx, work.ID, store.Message{Role: "user", Content: "fix the walrus parser"}); err != nil {
		t.Fatal(err)
	}
	sf.env.SessionState = func(_ context.Context, id string) string {
		if id == work.ID {
			return "running"
		}
		return "idle"
	}
	check := func(shape string, m map[string]any) {
		t.Helper()
		if str(m, "cwd") != "/srv/repo" || str(m, "state") != "running" {
			t.Errorf("%s = %v", shape, m)
		}
	}
	check("browse", browsedFirst(t, sf))
	found, _ := sf.call(t, "session_search", map[string]any{"query": "walrus"})["results"].([]any)
	if len(found) != 1 {
		t.Fatalf("discover = %v", found)
	}
	check("discover", found[0].(map[string]any))
	check("read", sf.call(t, "session_search", map[string]any{"session_id": work.ID})["session_meta"].(map[string]any))
	check("window", sf.call(t, "session_search", map[string]any{"session_id": work.ID, "around_message_id": 1})["session_meta"].(map[string]any))

	sf.env.SessionState = nil
	if m := browsedFirst(t, sf); m["state"] != nil || str(m, "cwd") != "/srv/repo" {
		t.Fatalf("with no daemon to ask, there is no state: %v", m)
	}
}

// A silent cut read as a reply that had stopped mid-sentence, and it took the
// end of a task with it.
func TestACutMessageSaysSoAndCanBeReadToTheEnd(t *testing.T) {
	sf := sessionSetup(t)
	long, _ := sf.st.CreateSession(ctx, store.Session{Kind: "interactive", Title: "Long reply"})
	text := strings.Repeat("a", 4000) + strings.Repeat("b", 4500) + "the end"
	if _, err := sf.st.AppendMessages(ctx, long.ID, store.Message{Role: "assistant", Content: text}); err != nil {
		t.Fatal(err)
	}
	first := func(r map[string]any) string {
		t.Helper()
		msgs, _ := r["messages"].([]any)
		if len(msgs) != 1 {
			t.Fatalf("messages = %v", r)
		}
		return str(msgs[0].(map[string]any), "content")
	}
	got := first(sf.call(t, "session_search", map[string]any{"session_id": long.ID}))
	if !strings.HasPrefix(got, strings.Repeat("a", 4000)+"\n[cut: ") || strings.Contains(got, "b") ||
		!strings.Contains(got, "4000 of 8507 characters") || !strings.Contains(got, "around_message_id 1, content_offset 4000") {
		t.Fatalf("a cut message ends %q", got[len(got)-160:])
	}
	if got := first(sf.call(t, "session_search", map[string]any{"session_id": long.ID, "around_message_id": 1})); !strings.Contains(got, "[cut: ") {
		t.Fatalf("no marker in a window: %q", got[len(got)-160:])
	}
	got = first(sf.call(t, "session_search", map[string]any{"session_id": long.ID, "around_message_id": 1, "content_offset": 4000}))
	if !strings.HasPrefix(got, strings.Repeat("b", 4000)+"\n[cut: ") || !strings.Contains(got, "content_offset 8000") {
		t.Fatalf("the next part ends %q", got[len(got)-160:])
	}
	got = first(sf.call(t, "session_search", map[string]any{"session_id": long.ID, "around_message_id": 1, "content_offset": 8000}))
	if got != strings.Repeat("b", 500)+"the end" {
		t.Fatalf("the last part = %q", got)
	}
	r := sf.call(t, "session_search", map[string]any{"session_id": long.ID, "around_message_id": 1, "content_offset": 9000})
	if r["success"] != false || !strings.Contains(str(r, "error"), "8507") {
		t.Fatalf("an offset past the end = %v", r)
	}
	r = sf.call(t, "session_search", map[string]any{"session_id": long.ID, "content_offset": 4000})
	if r["success"] != false || !strings.Contains(str(r, "error"), "around_message_id") {
		t.Fatalf("an offset with no message = %v", r)
	}
}

func TestAQueryInsideOneSession(t *testing.T) {
	sf := sessionSetup(t)
	long, _ := sf.st.CreateSession(ctx, store.Session{Kind: "interactive", Title: "Long one"})
	for i := range 40 {
		content := fmt.Sprintf("line %d", i+1)
		if i == 24 {
			content = "the needle is in here"
		}
		_, _ = sf.st.AppendMessages(ctx, long.ID, store.Message{Role: []string{"user", "assistant"}[i%2], Content: content})
	}
	other, _ := sf.st.CreateSession(ctx, store.Session{Kind: "interactive", Title: "Other"})
	_, _ = sf.st.AppendMessages(ctx, other.ID, store.Message{Role: "user", Content: "another needle elsewhere"})

	r := sf.call(t, "session_search", map[string]any{"session_id": long.ID, "query": "needle", "window": 2})
	msgs, _ := r["messages"].([]any)
	matches, _ := r["matches"].([]any)
	if r["success"] != true || str(r, "mode") != "session_query" || num(r, "anchor_seq") != 25 || len(msgs) != 5 || len(matches) != 1 {
		t.Fatalf("a query in a session = %v", r)
	}
	if num(msgs[0].(map[string]any), "seq") != 23 || num(matches[0].(map[string]any), "seq") != 25 {
		t.Fatalf("the window around the hit = %v", r)
	}
	r = sf.call(t, "session_search", map[string]any{"session_id": long.ID, "query": "giraffe"})
	if r["success"] != true || num(r, "count") != 0 {
		t.Fatalf("no match = %v", r)
	}
	r = sf.call(t, "session_search", map[string]any{"session_id": sf.walled, "query": "zebra"})
	if r["success"] != false || !strings.Contains(str(r, "error"), "session_id not found") {
		t.Fatalf("the wall holds for a query in a session: %v", r)
	}
}

// Six reads that passed window or detail came back byte-identical.
func TestAReadRefusesWhatItCannotDo(t *testing.T) {
	sf := sessionSetup(t)
	for _, extra := range []map[string]any{{"window": 10}, {"detail": "full"}} {
		args := map[string]any{"session_id": sf.a}
		for k, v := range extra {
			args[k] = v
		}
		r := sf.call(t, "session_search", args)
		if r["success"] != false || !strings.Contains(str(r, "error"), "around_message_id") {
			t.Errorf("%v = %v", extra, r)
		}
	}
}

func TestAReadSaysWhatItLeftOut(t *testing.T) {
	sf := sessionSetup(t)
	long, _ := sf.st.CreateSession(ctx, store.Session{Kind: "interactive", Title: "Long one"})
	for i := range 40 {
		_, _ = sf.st.AppendMessages(ctx, long.ID, store.Message{Role: []string{"user", "assistant"}[i%2], Content: fmt.Sprintf("line %d", i+1)})
	}
	r := sf.call(t, "session_search", map[string]any{"session_id": long.ID})
	if got := str(r, "omitted"); !strings.Contains(got, "10 messages") || !strings.Contains(got, "seq 20 and seq 31") || !strings.Contains(got, "around_message_id") {
		t.Fatalf("omitted = %q", got)
	}
	r = sf.call(t, "session_search", map[string]any{"session_id": sf.a})
	if r["omitted"] != nil {
		t.Fatalf("a short session leaves nothing out: %v", r)
	}
}
