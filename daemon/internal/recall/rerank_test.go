package recall_test

// Reranking: the cosine candidates reordered by <BaseURL>/rerank. The query
// "NAS address pizza printer" shares words with three common items, so with
// MinScore 0.1 all three are candidates, in this cosine order (fakellm's
// WordVector): the NAS fact, the pizza fact, the printer note.

import (
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/recall"
)

const (
	nasText     = "NAS address 10.0.0.3 home network"
	pizzaText   = "Favorite food: pizza with olives"
	printerText = "Printer sits in the attic beside the router"
	threeQuery  = "NAS address pizza printer"
)

// rerankIndex opens the usual test memory with a reranker; edit adjusts the
// config first.
func rerankIndex(t *testing.T, edit func(*recall.Config)) (*recall.Index, *fakellm.Server) {
	t.Helper()
	fake := fakellm.New(t)
	home := t.TempDir()
	put(t, home, "memories/facts.toml", fact("nas-address", nasText)+fact("pizza", pizzaText))
	put(t, home, "memories/facts.gateway.toml", fact("therapist", "Therapist appointment Tuesdays, Dr Smith"))
	put(t, home, "notes/common/hosts.md", printerText)
	put(t, home, "notes/gateway/diary.md", "Felt tired after Sunday running")
	cfg := recall.Config{
		BaseURL: fake.URL, APIKey: "k", Model: "embed", TopK: 5, MinScore: 0.1,
		QueryPrompt: "q: ", DocumentPrompt: "d: ",
		SearchTimeout: 3 * time.Second, FailureCooldown: time.Minute,
		RerankModel: "rerank", RerankCandidates: 10, RerankMinScore: 0.5, RerankTimeout: 3 * time.Second,
	}
	if edit != nil {
		edit(&cfg)
	}
	x, err := recall.Open(home, cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = x.Close() })
	// printer first, then pizza; the NAS fact (the best cosine) falls under RerankMinScore.
	fake.SetRerankScore(func(_, doc string) float64 {
		switch doc {
		case printerText:
			return 0.9
		case pizzaText:
			return 0.6
		}
		return 0.2
	})
	return x, fake
}

func block(lines ...string) string {
	return header + strings.Join(lines, "\n") + "\n</recalled-notes>"
}

func TestTheRerankerOrdersAndFiltersTheCandidates(t *testing.T) {
	x, fake := rerankIndex(t, func(c *recall.Config) { c.TopK = 2 })
	got := x.Recall(ctx, "common", threeQuery)
	if want := block("[hosts.md] "+printerText, "[fact pizza] "+pizzaText); got != want {
		t.Fatalf("Recall = %q\nwant      %q", got, want)
	}
	calls := fake.RerankCalls()
	if len(calls) != 1 {
		t.Fatalf("rerank calls = %+v", calls)
	}
	want := fakellm.RerankCall{Model: "rerank", Query: threeQuery, Documents: []string{nasText, pizzaText, printerText}, TopN: 3}
	if !reflect.DeepEqual(calls[0], want) {
		t.Fatalf("rerank call = %+v\nwant          %+v (the plain query and texts, in cosine order)", calls[0], want)
	}
}

func TestTheRerankerSeesAtMostRerankCandidates(t *testing.T) {
	x, fake := rerankIndex(t, func(c *recall.Config) { c.RerankCandidates = 2 })
	got := x.Recall(ctx, "common", threeQuery)
	if want := block("[fact pizza] " + pizzaText); got != want {
		t.Fatalf("Recall = %q\nwant      %q", got, want)
	}
	if calls := fake.RerankCalls(); len(calls) != 1 || !reflect.DeepEqual(calls[0].Documents, []string{nasText, pizzaText}) {
		t.Fatalf("the reranker sees the two best by cosine: %+v", calls)
	}
}

func TestCosineOrderWhenTheRerankerIsDown(t *testing.T) {
	x, fake := rerankIndex(t, func(c *recall.Config) { c.TopK = 2 })
	fake.SetRerankStatus(503)
	cosine := block("[fact nas-address] "+nasText, "[fact pizza] "+pizzaText)
	if got := x.Recall(ctx, "common", threeQuery); got != cosine {
		t.Fatalf("Recall = %q\nwant      %q", got, cosine)
	}
	reranks, embeds := len(fake.RerankCalls()), len(fake.EmbedCalls())
	if reranks != 1 {
		t.Fatalf("rerank calls = %d; the first recall asks the reranker", reranks)
	}
	if got := x.Recall(ctx, "common", threeQuery); got != cosine {
		t.Fatalf("during the cooldown: %q", got)
	}
	if len(fake.RerankCalls()) != reranks {
		t.Fatal("after a failure the reranker is left alone for the cooldown")
	}
	if len(fake.EmbedCalls()) == embeds {
		t.Fatal("a failed reranker leaves the embedder in use")
	}
}

func TestCosineOrderWhenTheRerankerIsSlow(t *testing.T) {
	x, fake := rerankIndex(t, func(c *recall.Config) { c.TopK = 2; c.RerankTimeout = 50 * time.Millisecond })
	x.Recall(ctx, "common", "NAS address") // the index is built outside the timed call
	fake.SetRerankDelay(5 * time.Second)
	start := time.Now()
	got := x.Recall(ctx, "common", threeQuery)
	if want := block("[fact nas-address] "+nasText, "[fact pizza] "+pizzaText); got != want {
		t.Fatalf("Recall = %q\nwant      %q", got, want)
	}
	if d := time.Since(start); d > 2*time.Second {
		t.Fatalf("Recall took %v; RerankTimeout bounds the wait", d)
	}
	if n := len(fake.RerankCalls()); n != 2 {
		t.Fatalf("rerank calls = %d; both recalls asked the reranker", n)
	}
}

func TestTheRerankerReadsTheStartOfALongQuery(t *testing.T) {
	x, fake := rerankIndex(t, nil)
	query := "NAS address " + strings.Repeat("é", 1000)
	x.Recall(ctx, "common", query)
	calls := fake.RerankCalls()
	if len(calls) != 1 {
		t.Fatalf("rerank calls = %d", len(calls))
	}
	if calls[0].Query != string([]rune(query)[:500]) {
		t.Fatalf("the reranker's query is %d runes; want the first 500", len([]rune(calls[0].Query)))
	}
	embedded := false
	for _, call := range fake.EmbedCalls() {
		for _, in := range call {
			embedded = embedded || in == "q: "+query
		}
	}
	if !embedded {
		t.Fatal("the embedder still reads the whole query")
	}
}

func TestNoRerankingWithoutCandidatesOrAModel(t *testing.T) {
	x, fake := rerankIndex(t, nil)
	if got := x.Recall(ctx, "common", "zebra unicorn"); got != "" || len(fake.RerankCalls()) != 0 {
		t.Fatalf("nothing passes MinScore: Recall = %q, rerank calls %d", got, len(fake.RerankCalls()))
	}
	x, fake = rerankIndex(t, func(c *recall.Config) { c.RerankModel = "" })
	cosine := block("[fact nas-address] "+nasText, "[fact pizza] "+pizzaText, "[hosts.md] "+printerText)
	if got := x.Recall(ctx, "common", threeQuery); got != cosine || len(fake.RerankCalls()) != 0 {
		t.Fatalf("no rerank_model: Recall = %q, rerank calls %d", got, len(fake.RerankCalls()))
	}
}

func TestRerankConfigFromTheConfigFile(t *testing.T) {
	home := t.TempDir()
	put(t, home, "config.toml", "[memory.rag]\nenabled = true\nbase_url = \"http://embed.test/v1\"\nmodel = \"embed/embeddinggemma-2\"\n")
	cfg, err := config.Load(home)
	if err != nil {
		t.Fatal(err)
	}
	rc, _ := recall.ConfigFrom(cfg, home)
	if rc.RerankModel != "" || rc.RerankCandidates != 10 || rc.RerankMinScore != 0.5 || rc.RerankTimeout != 3*time.Second {
		t.Fatalf("defaults: %+v", rc)
	}
	put(t, home, "config.toml", "[memory.rag]\nenabled = true\nbase_url = \"http://embed.test/v1\"\nmodel = \"embed/embeddinggemma-2\"\n"+
		"rerank_model = \"rerank/qwen3-reranker-0.6b\"\nrerank_candidates = 8\nrerank_min_score = 0\nrerank_timeout_seconds = 2\n")
	cfg, err = config.Load(home)
	if err != nil {
		t.Fatal(err)
	}
	rc, _ = recall.ConfigFrom(cfg, home)
	if rc.RerankModel != "rerank/qwen3-reranker-0.6b" || rc.RerankCandidates != 8 || rc.RerankMinScore != 0 || rc.RerankTimeout != 2*time.Second {
		t.Fatalf("explicit values, a zero score included: %+v", rc)
	}
}
