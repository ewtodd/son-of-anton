package tools_test

// Phase 3c: session_search (adapted from ../tools/session_search_tool.py).
// Four shapes: browse (no arguments), discover (query), read (session_id) and
// window (session_id + around_message_id). The wall holds in every one: a
// common caller never finds, lists, reads or scrolls a gateway or chat
// session, and a walled session reads exactly like a missing one.

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

type sessionFixture struct {
	*fixture
	st                *store.Store
	own, a, b, walled string
}

// sessionSetup: the caller's own session, two common sessions (a older, b
// newer) and a walled gateway chat, each with a few messages.
func sessionSetup(t *testing.T) *sessionFixture {
	t.Helper()
	f := setup(t, nil)
	st, err := store.Open(filepath.Join(t.TempDir(), "soa-d.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	mk := func(s store.Session, msgs ...store.Message) string {
		created, err := st.CreateSession(ctx, s)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := st.AppendMessages(ctx, created.ID, msgs...); err != nil {
			t.Fatal(err)
		}
		return created.ID
	}
	sf := &sessionFixture{fixture: f, st: st}
	sf.own = mk(store.Session{Kind: "interactive"}, store.Message{Role: "user", Content: "the zebra in my own session"})
	sf.a = mk(store.Session{Kind: "interactive", Title: "Router setup"},
		store.Message{Role: "user", Content: "help me set up the router"},
		store.Message{Role: "assistant", Content: "use the zebra protocol on channel 6"})
	sf.b = mk(store.Session{Kind: "interactive", Title: "Printer"},
		store.Message{Role: "user", Content: "the printer jams"},
		store.Message{Role: "assistant", Content: "clean the rollers"})
	sf.walled = mk(store.Session{Kind: "chat", Scope: "gateway", Chat: "signal:g"},
		store.Message{Role: "user", Content: "my zebra therapist secret"},
		store.Message{Role: "assistant", Content: "noted privately"})
	f.env.Store, f.env.SessionID, f.env.MemoryScope = st, sf.own, "common"
	return sf
}

func sessionIDs(results []any) []string {
	var out []string
	for _, r := range results {
		out = append(out, str(r.(map[string]any), "session_id"))
	}
	return out
}

func TestTheSearchSchema(t *testing.T) {
	tool, ok := tools.NewRegistry(tools.Builtin()...).Lookup("session_search")
	if !ok {
		t.Fatal("session_search is a builtin tool")
	}
	d := tool.Schema().Function.Description
	for _, phrase := range []string{"Search or read past son-of-anton conversations", "no arguments: list recent sessions", "Query words are ANDed"} {
		if !strings.Contains(d, phrase) {
			t.Errorf("description lacks %q", phrase)
		}
	}
}

func TestBrowseListsRecentReadableSessions(t *testing.T) {
	sf := sessionSetup(t)
	r := sf.call(t, "session_search", map[string]any{})
	results, _ := r["results"].([]any)
	if r["success"] != true || str(r, "mode") != "browse" || strings.Join(sessionIDs(results), ",") != sf.b+","+sf.a {
		t.Fatalf("browse = %v; newest first, the caller's own and walled sessions left out", r)
	}
	first := results[0].(map[string]any)
	if str(first, "title") != "Printer" || num(first, "message_count") != 2 || str(first, "preview") == "" {
		t.Fatalf("browse entry = %v", first)
	}
	sf.env.MemoryScope = "gateway"
	r = sf.call(t, "session_search", map[string]any{})
	if got, _ := r["results"].([]any); len(got) != 3 {
		t.Fatalf("a gateway caller lists the chat too: %v", r)
	}
}

func TestDiscoverFindsTheMatchWithContext(t *testing.T) {
	sf := sessionSetup(t)
	msgs := []store.Message{}
	for i := range 20 {
		role := []string{"user", "assistant"}[i%2]
		text := fmt.Sprintf("filler message %d", i)
		if i == 10 {
			text = "the quokka migration plan"
		}
		msgs = append(msgs, store.Message{Role: role, Content: text})
	}
	long, _ := sf.st.CreateSession(ctx, store.Session{Kind: "interactive"})
	_, _ = sf.st.AppendMessages(ctx, long.ID, msgs...)
	other, _ := sf.st.CreateSession(ctx, store.Session{Kind: "interactive"})
	_, _ = sf.st.AppendMessages(ctx, other.ID, store.Message{Role: "user", Content: "one quokka mention"})

	r := sf.call(t, "session_search", map[string]any{"query": "quokka"})
	results, _ := r["results"].([]any)
	if r["success"] != true || str(r, "mode") != "discover" || len(results) != 2 {
		t.Fatalf("discover = %v", r)
	}
	var best, rest map[string]any
	for _, res := range results {
		m := res.(map[string]any)
		if str(m, "session_id") == long.ID {
			best = m
		} else {
			rest = m
		}
	}
	if best == nil || rest == nil || num(best["match"].(map[string]any), "seq") != 11 || !strings.Contains(str(best["match"].(map[string]any), "content"), "quokka") {
		t.Fatalf("the hit = %v", best)
	}
	if results[0].(map[string]any)["context"] == nil || results[1].(map[string]any)["context"] != nil {
		t.Fatalf("adaptive detail: only the top result carries context: %v", results)
	}
	full := sf.call(t, "session_search", map[string]any{"query": "quokka", "detail": "full"})
	for _, res := range full["results"].([]any) {
		if m := res.(map[string]any); str(m, "session_id") == long.ID {
			best = m
		}
	}
	{
		ctxRes, _ := best["context"].(map[string]any)
		around, _ := ctxRes["around"].([]any)
		if len(around) != 11 || num(around[0].(map[string]any), "seq") != 6 {
			t.Fatalf("around = %v; five on each side of the hit", around)
		}
		if op, _ := ctxRes["opening"].([]any); len(op) != 3 || num(op[0].(map[string]any), "seq") != 1 {
			t.Fatalf("opening = %v", ctxRes["opening"])
		}
		if cl, _ := ctxRes["closing"].([]any); len(cl) != 3 || num(cl[2].(map[string]any), "seq") != 20 {
			t.Fatalf("closing = %v", ctxRes["closing"])
		}
	}
	r = sf.call(t, "session_search", map[string]any{"query": "quokka", "detail": "full"})
	for _, res := range r["results"].([]any) {
		if res.(map[string]any)["context"] == nil {
			t.Fatalf("detail full: every result carries context: %v", res)
		}
	}
	if r := sf.call(t, "session_search", map[string]any{"query": "quokka", "limit": 1}); len(r["results"].([]any)) != 1 {
		t.Fatalf("limit: %v", r)
	}
}

func TestReadAndWindow(t *testing.T) {
	sf := sessionSetup(t)
	long, _ := sf.st.CreateSession(ctx, store.Session{Kind: "interactive", Title: "Long one"})
	for i := range 40 {
		_, _ = sf.st.AppendMessages(ctx, long.ID, store.Message{Role: []string{"user", "assistant"}[i%2], Content: fmt.Sprintf("line %d", i+1)})
	}
	r := sf.call(t, "session_search", map[string]any{"session_id": long.ID})
	msgs, _ := r["messages"].([]any)
	if r["success"] != true || str(r, "mode") != "read" || num(r, "message_count") != 40 || r["truncated"] != true || len(msgs) != 30 {
		t.Fatalf("read = %v", r)
	}
	if num(msgs[19].(map[string]any), "seq") != 20 || num(msgs[20].(map[string]any), "seq") != 31 {
		t.Fatalf("the first 20 and the last 10: %v", msgs)
	}
	r = sf.call(t, "session_search", map[string]any{"session_id": long.ID, "around_message_id": 15, "window": 3})
	win, _ := r["messages"].([]any)
	if str(r, "mode") != "window" || len(win) != 7 || num(win[0].(map[string]any), "seq") != 12 || num(win[6].(map[string]any), "seq") != 18 {
		t.Fatalf("window = %v", r)
	}
	r = sf.call(t, "session_search", map[string]any{"session_id": long.ID, "around_message_id": 18, "window": 3})
	if win, _ := r["messages"].([]any); num(win[0].(map[string]any), "seq") != 15 {
		t.Fatalf("scrolling forward from the window's last id: %v", r)
	}
	if r := sf.call(t, "session_search", map[string]any{"session_id": long.ID, "around_message_id": 999}); r["success"] != false {
		t.Fatalf("an anchor past the end: %v", r)
	}
}

func TestTheWallHoldsInEveryShape(t *testing.T) {
	sf := sessionSetup(t)
	notFound := "session_id not found: " + sf.walled
	r := sf.call(t, "session_search", map[string]any{"query": "zebra"})
	for _, id := range sessionIDs(r["results"].([]any)) {
		if id == sf.walled || id == sf.own {
			t.Fatalf("discover reached %s: %v", id, r)
		}
	}
	if strings.Contains(fmt.Sprint(r), "therapist") {
		t.Fatalf("walled text leaked: %v", r)
	}
	for _, args := range []map[string]any{
		{"session_id": sf.walled},
		{"session_id": sf.walled, "around_message_id": 1},
	} {
		r := sf.call(t, "session_search", args)
		if r["success"] != false || str(r, "error") != notFound {
			t.Fatalf("%v = %v; a walled session reads as not found", args, r)
		}
	}
	missing := "20260101_000000_000000"
	if r := sf.call(t, "session_search", map[string]any{"session_id": missing}); str(r, "error") != "session_id not found: "+missing {
		t.Fatalf("missing = %v", r)
	}
	sf.env.MemoryScope = "gateway"
	if r := sf.call(t, "session_search", map[string]any{"session_id": sf.walled}); r["success"] != true {
		t.Fatalf("a gateway caller reads the chat: %v", r)
	}
}

func TestToolOutputOnlyWhenAsked(t *testing.T) {
	sf := sessionSetup(t)
	_, _ = sf.st.AppendMessages(ctx, sf.b, store.Message{Role: "tool", Content: "the wombat is in the tool output", ToolCallID: "c9", ToolName: "terminal"})
	if r := sf.call(t, "session_search", map[string]any{"query": "wombat"}); len(r["results"].([]any)) != 0 {
		t.Fatalf("tool output is not searched by default: %v", r)
	}
	if r := sf.call(t, "session_search", map[string]any{"query": "wombat", "role_filter": "user,assistant,tool"}); len(r["results"].([]any)) != 1 {
		t.Fatalf("role_filter with tool: %v", r)
	}
}

func TestMeaningMatchesComeAfterTextMatches(t *testing.T) {
	sf := sessionSetup(t)
	var asked string
	sf.env.SearchMessages = func(_ context.Context, query string, k int) []tools.MessageHit {
		asked = query
		return []tools.MessageHit{
			{SessionID: sf.b, Seq: 2, Text: "clean the rollers", Score: 0.8},
			{SessionID: sf.walled, Seq: 1, Text: "my zebra therapist secret", Score: 0.9},
			{SessionID: sf.a, Seq: 2, Text: "use the zebra protocol on channel 6", Score: 0.7},
		}
	}
	r := sf.call(t, "session_search", map[string]any{"query": "zebra", "limit": 5})
	got := sessionIDs(r["results"].([]any))
	if asked != "zebra" || strings.Join(got, ",") != sf.a+","+sf.b {
		t.Fatalf("results = %v; text matches first (a), then meaning matches not already present (b), never a walled one", got)
	}
}

// One long session full of matches must not hide every other session that
// matches (review: discover took the first 200 raw hits, then grouped).
func TestOneBusySessionDoesNotHideTheRest(t *testing.T) {
	sf := sessionSetup(t)
	quiet, _ := sf.st.CreateSession(ctx, store.Session{Kind: "interactive"})
	_, _ = sf.st.AppendMessages(ctx, quiet.ID, store.Message{Role: "user", Content: "the capybara and something else entirely, said once at length"})
	busy, _ := sf.st.CreateSession(ctx, store.Session{Kind: "interactive"})
	for i := range 250 {
		_, _ = sf.st.AppendMessages(ctx, busy.ID, store.Message{Role: "user", Content: fmt.Sprintf("capybara %d", i)})
	}
	got := sessionIDs(sf.call(t, "session_search", map[string]any{"query": "capybara", "limit": 3})["results"].([]any))
	if len(got) != 2 {
		t.Fatalf("discover found %v; both sessions that mention it", got)
	}
}
