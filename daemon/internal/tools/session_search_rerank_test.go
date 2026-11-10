package tools_test

// session_search with a reranker: text and meaning matches ranked together
// by relevance, none dropped; sorted searches and a failing reranker keep
// the old order.

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

const frame = "Find the past conversation about: "

// rerankSetup is sessionSetup with the meaning matches of
// TestMeaningMatchesComeAfterTextMatches (b, the walled chat, a) and a
// reranker scoring each document with score, recording what it was sent.
func rerankSetup(t *testing.T, score func(doc string) float64) (*sessionFixture, *[]string, *[]string) {
	t.Helper()
	sf := sessionSetup(t)
	sf.env.SearchMessages = func(_ context.Context, _ string, _ int) []tools.MessageHit {
		return []tools.MessageHit{
			{SessionID: sf.b, Seq: 2, Text: "clean the rollers", Score: 0.8},
			{SessionID: sf.walled, Seq: 1, Text: "my zebra therapist secret", Score: 0.9},
			{SessionID: sf.a, Seq: 2, Text: "use the zebra protocol on channel 6", Score: 0.7},
		}
	}
	var queries, docs []string
	sf.env.Rerank = func(_ context.Context, q string, ds []string) ([]float64, error) {
		queries = append(queries, q)
		docs = append(docs, ds...)
		out := make([]float64, len(ds))
		for i, d := range ds {
			out[i] = score(d)
		}
		return out, nil
	}
	return sf, &queries, &docs
}

func TestRerankingRanksTextAndMeaningMatchesTogether(t *testing.T) {
	sf, queries, docs := rerankSetup(t, func(doc string) float64 {
		if strings.Contains(doc, "rollers") {
			return 0.9
		}
		return 0.2
	})
	got := sessionIDs(sf.call(t, "session_search", map[string]any{"query": "zebra", "limit": 5})["results"].([]any))
	if strings.Join(got, ",") != sf.b+","+sf.a {
		t.Fatalf("results = %v; the reranker's order (b, then the text match a)", got)
	}
	want := []string{"Conversation \"Router setup\"\nassistant: use the zebra protocol on channel 6", "Conversation \"Printer\"\nassistant: clean the rollers"}
	if !reflect.DeepEqual(*queries, []string{frame + "zebra"}) || !reflect.DeepEqual(*docs, want) {
		t.Fatalf("sent %q / %q; the framed query, each match with its session's title and role, never the walled one", *queries, *docs)
	}
}

func TestRerankingNeverDropsAMatch(t *testing.T) {
	sf, _, _ := rerankSetup(t, func(string) float64 { return 0.001 })
	got := sessionIDs(sf.call(t, "session_search", map[string]any{"query": "zebra", "limit": 5})["results"].([]any))
	if strings.Join(got, ",") != sf.a+","+sf.b {
		t.Fatalf("results = %v; every match stays, ties in the old order", got)
	}
}

func TestSortedSearchesAreNotReranked(t *testing.T) {
	sf, queries, _ := rerankSetup(t, func(string) float64 { return 0.9 })
	got := sessionIDs(sf.call(t, "session_search", map[string]any{"query": "zebra", "limit": 5, "sort": "newest"})["results"].([]any))
	if len(*queries) != 0 || strings.Join(got, ",") != sf.a+","+sf.b {
		t.Fatalf("results = %v, reranker asked %d time(s); a sort is the caller's order", got, len(*queries))
	}
}

func TestAFailingRerankerKeepsTheOrder(t *testing.T) {
	sf, _, _ := rerankSetup(t, nil)
	sf.env.Rerank = func(context.Context, string, []string) ([]float64, error) { return nil, errors.New("down") }
	got := sessionIDs(sf.call(t, "session_search", map[string]any{"query": "zebra", "limit": 5})["results"].([]any))
	if strings.Join(got, ",") != sf.a+","+sf.b {
		t.Fatalf("results = %v; text matches first, then meaning matches, as without a reranker", got)
	}
}

func TestTheRerankerGetsTheQueryAsWords(t *testing.T) {
	sf, queries, _ := rerankSetup(t, func(string) float64 { return 0.9 })
	sf.call(t, "session_search", map[string]any{"query": `"zebra protocol" OR channel* NOT rollers`})
	if !reflect.DeepEqual(*queries, []string{frame + "zebra protocol channel"}) {
		t.Fatalf("reranker query = %q; FTS5 quotes, stars and operators dropped, NOT with its word", *queries)
	}
}

func TestRerankingPicksTheAnchorInsideASession(t *testing.T) {
	sf, _, _ := rerankSetup(t, func(doc string) float64 {
		switch {
		case strings.Contains(doc, "cherries"):
			return 0.9
		case strings.Contains(doc, "apples"):
			return 0.5
		}
		return 0.1
	})
	s, _ := sf.st.CreateSession(ctx, store.Session{Kind: "interactive"})
	_, _ = sf.st.AppendMessages(ctx, s.ID, store.Message{Role: "user", Content: "kiwi apples"},
		store.Message{Role: "assistant", Content: "kiwi bananas"}, store.Message{Role: "user", Content: "kiwi cherries"})
	r := sf.call(t, "session_search", map[string]any{"session_id": s.ID, "query": "kiwi"})
	var seqs []int
	for _, m := range r["matches"].([]any) {
		seqs = append(seqs, num(m.(map[string]any), "seq"))
	}
	if !reflect.DeepEqual(seqs, []int{3, 1, 2}) || num(r, "anchor_seq") != 3 {
		t.Fatalf("matches %v, anchor %v; the reranker's order, its best as the anchor", seqs, r["anchor_seq"])
	}
}

func TestTheRerankerReadsALongMessageAroundTheMatch(t *testing.T) {
	sf, _, docs := rerankSetup(t, func(string) float64 { return 0.9 })
	sf.env.SearchMessages = nil
	long := strings.Repeat("filler words here ", 300) + "the quokka plan" + strings.Repeat(" more filler", 300)
	s, _ := sf.st.CreateSession(ctx, store.Session{Kind: "interactive"})
	_, _ = sf.st.AppendMessages(ctx, s.ID, store.Message{Role: "user", Content: long})
	sf.call(t, "session_search", map[string]any{"query": "quokka"})
	if len(*docs) != 1 || !strings.HasPrefix((*docs)[0], "user: ") {
		t.Fatalf("sent %q; one untitled match with its role", *docs)
	}
	if body := strings.TrimPrefix((*docs)[0], "user: "); len([]rune(body)) != 1200 || !strings.Contains(body, "the quokka plan") {
		t.Fatalf("sent %d characters; want 1,200 around the match", len([]rune(body)))
	}
}
