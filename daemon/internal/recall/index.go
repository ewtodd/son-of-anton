package recall

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	_ "modernc.org/sqlite"

	"github.com/ewtodd/son-of-anton/daemon/internal/memory"
)

const (
	embedBatch         = 16
	similarThreshold   = 0.92 // memory_tool.FACT_EMBEDDING_THRESHOLD
	factKind, noteKind = "fact", "note"
)

// Index is the embedding index over facts and notes, kept in
// <home>/recall.db. It is safe for concurrent use; calls are serialized.
type Index struct {
	cfg   Config
	home  string
	store *memory.Store
	db    *sql.DB
	http  *http.Client

	msgMu               sync.Mutex // serializes IndexMessages
	mu                  sync.Mutex
	cooldownUntil       time.Time
	rerankCooldownUntil time.Time
}

// Open opens (creating) the index database.
func Open(home string, cfg Config) (*Index, error) {
	path := filepath.Join(home, "recall.db")
	if f, err := os.OpenFile(path, os.O_RDONLY|os.O_CREATE, 0o600); err == nil {
		_ = f.Close()
	}
	_ = os.Chmod(path, 0o600)
	db, err := sql.Open("sqlite", path+"?_pragma=busy_timeout(5000)")
	if err != nil {
		return nil, err
	}
	if _, err := db.Exec(`CREATE TABLE IF NOT EXISTS items (
		key TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL, label TEXT NOT NULL,
		text TEXT NOT NULL, hash TEXT NOT NULL, vec BLOB NOT NULL)`); err != nil {
		_ = db.Close()
		return nil, err
	}
	return &Index{cfg: cfg, home: home, store: memory.New(home), db: db, http: &http.Client{}}, nil
}

// Close closes the database.
func (x *Index) Close() error { return x.db.Close() }

// item is one thing to embed.
type item struct {
	key, kind, scope, label, text, hash string
}

func (x *Index) hashOf(text string) string {
	sum := sha256.Sum256([]byte(x.cfg.Model + "\x00" + x.cfg.DocumentPrompt + "\x00" + text))
	return hex.EncodeToString(sum[:])
}

func (x *Index) notesRoot() string {
	if x.cfg.NotesDir != "" {
		return x.cfg.NotesDir
	}
	return filepath.Join(x.home, "notes")
}

// desired lists what the index should hold. keep are key prefixes whose
// items must stay as they are (an unparseable or unreadable source).
func (x *Index) desired() (items []item, keep []string) {
	for _, scope := range []string{"common", "gateway"} {
		facts, err := x.store.Facts(scope)
		if err != nil {
			keep = append(keep, factKind+":"+scope+":")
			continue
		}
		for _, f := range facts {
			text := strings.Join(strings.Fields(f.Text), " ")
			items = append(items, item{key: factKind + ":" + scope + ":" + f.ID, kind: factKind, scope: scope,
				label: "fact " + f.ID, text: text, hash: x.hashOf(text)})
		}
	}
	root := x.notesRoot()
	seen := map[string]bool{}
	var walk func(dir string)
	walk = func(dir string) {
		real, err := filepath.EvalSymlinks(dir)
		if err != nil || seen[real] {
			return
		}
		seen[real] = true
		ents, err := os.ReadDir(dir)
		if err != nil {
			keep = append(keep, noteKind+":"+dir+string(filepath.Separator))
			return
		}
		rel, _ := filepath.Rel(root, dir)
		top := strings.Split(rel, string(filepath.Separator))[0]
		scope := "common"
		if top != "." {
			if s, ok := memory.NormalizeScope(top); ok {
				scope = s
			}
		}
		for _, e := range ents { // ReadDir is sorted by name
			p := filepath.Join(dir, e.Name())
			if st, err := os.Stat(p); err == nil && st.IsDir() { // follows symlinks
				walk(p)
				continue
			}
			switch strings.ToLower(filepath.Ext(e.Name())) {
			case ".md", ".markdown", ".txt":
			default:
				continue
			}
			b, err := os.ReadFile(p)
			if err != nil {
				keep = append(keep, noteKind+":"+p+"#")
				continue
			}
			for i, c := range chunkText(string(b), x.cfg.MaxChunkChars) {
				items = append(items, item{key: fmt.Sprintf("%s:%s#%d", noteKind, p, i), kind: noteKind, scope: scope,
					label: e.Name(), text: c, hash: x.hashOf(c)})
			}
		}
	}
	walk(root)
	return items, keep
}

// refresh brings the index up to date, embedding only what changed.
func (x *Index) refresh(ctx context.Context) error {
	want, keep := x.desired()
	rows, err := x.db.QueryContext(ctx, `SELECT key, hash FROM items`)
	if err != nil {
		return err
	}
	have := map[string]string{}
	for rows.Next() {
		var k, h string
		if err := rows.Scan(&k, &h); err != nil {
			_ = rows.Close()
			return err
		}
		have[k] = h
	}
	if err := rows.Err(); err != nil {
		return err
	}
	_ = rows.Close()
	wanted := map[string]bool{}
	var fresh []item
	for _, it := range want {
		wanted[it.key] = true
		if have[it.key] != it.hash {
			fresh = append(fresh, it)
		}
	}
	vecs := make([][]float32, 0, len(fresh))
	for start := 0; start < len(fresh); start += embedBatch {
		batch := fresh[start:min(start+embedBatch, len(fresh))]
		inputs := make([]string, len(batch))
		for i, it := range batch {
			inputs[i] = x.cfg.DocumentPrompt + it.text
		}
		vs, err := x.embed(ctx, inputs)
		if err != nil {
			return err
		}
		vecs = append(vecs, vs...)
	}
	tx, err := x.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback() //nolint:errcheck
	for k := range have {
		if wanted[k] || kept(k, keep) || strings.HasPrefix(k, messageKind+":") {
			continue
		}
		if _, err := tx.ExecContext(ctx, `DELETE FROM items WHERE key = ?`, k); err != nil {
			return err
		}
	}
	for i, it := range fresh {
		if _, err := tx.ExecContext(ctx, `INSERT OR REPLACE INTO items (key, kind, scope, label, text, hash, vec) VALUES (?,?,?,?,?,?,?)`,
			it.key, it.kind, it.scope, it.label, it.text, it.hash, encode(vecs[i])); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func kept(key string, prefixes []string) bool {
	for _, p := range prefixes {
		if strings.HasPrefix(key, p) {
			return true
		}
	}
	return false
}

// embed POSTs one batch to <BaseURL>/embeddings under SearchTimeout.
func (x *Index) embed(ctx context.Context, inputs []string) ([][]float32, error) {
	if x.cfg.SearchTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, x.cfg.SearchTimeout)
		defer cancel()
	}
	var out struct {
		Data []struct {
			Index     int       `json:"index"`
			Embedding []float64 `json:"embedding"`
		} `json:"data"`
	}
	if err := x.post(ctx, "embeddings", map[string]any{"model": x.cfg.Model, "input": inputs}, &out); err != nil {
		return nil, err
	}
	if len(out.Data) != len(inputs) {
		return nil, fmt.Errorf("embedding endpoint returned %d vector(s) for %d input(s)", len(out.Data), len(inputs))
	}
	sort.SliceStable(out.Data, func(i, j int) bool { return out.Data[i].Index < out.Data[j].Index })
	vs := make([][]float32, len(inputs))
	for i, d := range out.Data {
		if len(d.Embedding) == 0 || (i > 0 && len(d.Embedding) != len(vs[0])) {
			return nil, errors.New("embedding endpoint returned vectors of mixed or zero width")
		}
		v := make([]float32, len(d.Embedding))
		for j, f := range d.Embedding {
			v[j] = float32(f)
		}
		vs[i] = v
	}
	return vs, nil
}

// rerankQueryChars bounds the query the reranker reads. It scores every
// candidate against the whole query, so a long message would cost seconds a
// turn; the whole message still chooses the candidates.
const rerankQueryChars = 500

// rerank scores cands against query under RerankTimeout and returns those
// scoring at least RerankMinScore, best first, each with the reranker's score.
func (x *Index) rerank(ctx context.Context, query string, cands []scored) ([]scored, error) {
	docs := make([]string, len(cands))
	for i, c := range cands {
		docs[i] = c.text
	}
	scores, err := x.scores(ctx, query, docs, x.cfg.RerankTimeout)
	if err != nil {
		return nil, err
	}
	var kept []scored
	for i, s := range scores {
		if s >= x.cfg.RerankMinScore {
			kept = append(kept, scored{s, cands[i].hit})
		}
	}
	sort.SliceStable(kept, func(i, j int) bool { return kept[i].score > kept[j].score })
	return kept, nil
}

// scores asks <BaseURL>/rerank for each document's relevance to query, in
// the documents' order, within timeout (0: none); an answer that leaves a
// document out is an error. The query goes without QueryPrompt and the
// documents without DocumentPrompt: those frame the embedder's inputs.
func (x *Index) scores(ctx context.Context, query string, docs []string, timeout time.Duration) ([]float64, error) {
	if timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	if q := []rune(query); len(q) > rerankQueryChars {
		query = string(q[:rerankQueryChars])
	}
	var out struct {
		Results []struct {
			Index int     `json:"index"`
			Score float64 `json:"relevance_score"`
		} `json:"results"`
	}
	payload := map[string]any{"model": x.cfg.RerankModel, "query": query, "documents": docs, "top_n": len(docs)}
	if err := x.post(ctx, "rerank", payload, &out); err != nil {
		return nil, err
	}
	scores := make([]float64, len(docs))
	seen := make([]bool, len(docs))
	for _, r := range out.Results {
		if r.Index < 0 || r.Index >= len(docs) || seen[r.Index] {
			return nil, fmt.Errorf("rerank endpoint returned index %d for %d document(s)", r.Index, len(docs))
		}
		seen[r.Index], scores[r.Index] = true, r.Score
	}
	if len(out.Results) != len(docs) {
		return nil, fmt.Errorf("rerank endpoint scored %d of %d document(s)", len(out.Results), len(docs))
	}
	return scores, nil
}

// Reranks says whether a reranker is configured.
func (x *Index) Reranks() bool { return x.cfg.RerankModel != "" }

// Rerank scores docs against query for session_search: one score per
// document, in order, within SessionSearchRerankTimeout. It fails without a
// reranker and while the reranker cools down after a failure; a failure
// other than a cancelled call starts that cooldown. The index lock is not
// held while the reranker answers, so a slow search never delays a recall.
func (x *Index) Rerank(ctx context.Context, query string, docs []string) ([]float64, error) {
	if !x.Reranks() {
		return nil, errors.New("recall: no reranker is configured")
	}
	x.mu.Lock()
	cooling := time.Now().Before(x.rerankCooldownUntil)
	x.mu.Unlock()
	if cooling {
		return nil, errors.New("recall: the reranker is cooling down after a failure")
	}
	scores, err := x.scores(ctx, query, docs, x.cfg.SessionSearchRerankTimeout)
	if err != nil && ctx.Err() == nil {
		x.mu.Lock()
		x.rerankCooldownUntil = time.Now().Add(x.cfg.FailureCooldown)
		x.mu.Unlock()
		log.Printf("recall: reranking session_search failed (%v); its own order for %.0fs", err, x.cfg.FailureCooldown.Seconds())
	}
	return scores, err
}

// post sends payload to <BaseURL>/<path> and decodes the JSON answer into
// out. Errors name the endpoint ("embeddings request failed: HTTP 503").
func (x *Index) post(ctx context.Context, path string, payload, out any) error {
	body, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, x.cfg.BaseURL+"/"+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if x.cfg.APIKey != "" {
		// Bifrost accepts the virtual key as x-bf-vk; the bearer form keeps
		// plain OpenAI-compatible endpoints working.
		req.Header.Set("Authorization", "Bearer "+x.cfg.APIKey)
		req.Header.Set("x-bf-vk", x.cfg.APIKey)
	}
	resp, err := x.http.Do(req)
	if err != nil {
		return fmt.Errorf("%s request failed: %w", path, err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 64<<20))
	if err != nil {
		return fmt.Errorf("%s request failed: %w", path, err)
	}
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("%s request failed: HTTP %d", path, resp.StatusCode)
	}
	if err := json.Unmarshal(raw, out); err != nil {
		return fmt.Errorf("%s response unreadable: %w", path, err)
	}
	return nil
}

func encode(v []float32) []byte {
	b := make([]byte, 4*len(v))
	for i, f := range v {
		binary.LittleEndian.PutUint32(b[4*i:], math.Float32bits(f))
	}
	return b
}

func decode(b []byte) []float32 {
	v := make([]float32, len(b)/4)
	for i := range v {
		v[i] = math.Float32frombits(binary.LittleEndian.Uint32(b[4*i:]))
	}
	return v
}

func cosine(a, b []float32) float64 {
	if len(a) != len(b) {
		return 0
	}
	var dot, na, nb float64
	for i := range a {
		dot += float64(a[i]) * float64(b[i])
		na += float64(a[i]) * float64(a[i])
		nb += float64(b[i]) * float64(b[i])
	}
	if na == 0 || nb == 0 {
		return 0
	}
	return dot / (math.Sqrt(na) * math.Sqrt(nb))
}

func visible(scope, itemScope string) bool {
	return itemScope == "common" || itemScope == scope
}

func canon(scope string) string {
	if s, ok := memory.NormalizeScope(scope); ok {
		return s
	}
	return "common"
}

// scored is an index hit.
type scored struct {
	score float64
	hit
}

// search ranks the index against a query vector for the scopes it may see.
// facts says how many facts the index holds at all.
func (x *Index) search(ctx context.Context, scope string, qv []float32, kinds ...string) (hits []scored, facts int, err error) {
	rows, err := x.db.QueryContext(ctx, `SELECT kind, scope, label, text, vec FROM items`)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	for rows.Next() {
		var kind, sc, label, text string
		var vec []byte
		if err := rows.Scan(&kind, &sc, &label, &text, &vec); err != nil {
			return nil, 0, err
		}
		if kind == factKind {
			facts++
		}
		if !visible(scope, sc) {
			continue
		}
		ok := false
		for _, k := range kinds {
			ok = ok || k == kind
		}
		if !ok {
			continue
		}
		hits = append(hits, scored{cosine(qv, decode(vec)), hit{label, text}})
	}
	return hits, facts, rows.Err()
}

// Recall returns the fenced block of facts and notes relevant to query for a
// session of the given scope, or "". It never fails: an embedder that does
// not answer starts a cooldown and facts are matched by keyword meanwhile.
func (x *Index) Recall(ctx context.Context, scope, query string) string {
	query = strings.TrimSpace(query)
	if query == "" || strings.HasPrefix(query, "/") {
		return ""
	}
	scope = canon(scope)
	x.mu.Lock()
	defer x.mu.Unlock()
	if time.Now().Before(x.cooldownUntil) {
		return format(x.keyword(query, scope))
	}
	semantic, facts, err := x.semantic(ctx, scope, query)
	if err != nil {
		if ctx.Err() != nil {
			return "" // a cancelled turn says nothing about the embedder
		}
		x.cooldownUntil = time.Now().Add(x.cfg.FailureCooldown)
		log.Printf("recall: retrieval failed (%v); skipping the embedder for %.0fs", err, x.cfg.FailureCooldown.Seconds())
		return format(x.keyword(query, scope))
	}
	if facts == 0 {
		// An index with no facts cannot recall them: use keywords.
		semantic = append(x.keyword(query, scope), semantic...)
	}
	return format(semantic)
}

func (x *Index) semantic(ctx context.Context, scope, query string) ([]hit, int, error) {
	if err := x.refresh(ctx); err != nil {
		return nil, 0, err
	}
	qvs, err := x.embed(ctx, []string{x.cfg.QueryPrompt + query})
	if err != nil {
		return nil, 0, err
	}
	all, facts, err := x.search(ctx, scope, qvs[0], factKind, noteKind)
	if err != nil {
		return nil, 0, err
	}
	var good []scored
	for _, s := range all {
		if s.score >= x.cfg.MinScore {
			good = append(good, s)
		}
	}
	sort.SliceStable(good, func(i, j int) bool { return good[i].score > good[j].score })
	good = x.reranked(ctx, query, good)
	good = good[:min(len(good), max(x.cfg.TopK, 1))]
	hits := make([]hit, len(good))
	for i, s := range good {
		hits[i] = s.hit
	}
	return hits, facts, nil
}

// reranked is good (best first by cosine) as the reranker orders its first
// RerankCandidates, when a reranker is configured. Without one, while it
// cools down, or when it fails, good is returned as it is; a failure other
// than a cancelled turn leaves the reranker alone for FailureCooldown.
func (x *Index) reranked(ctx context.Context, query string, good []scored) []scored {
	if x.cfg.RerankModel == "" || len(good) == 0 || time.Now().Before(x.rerankCooldownUntil) {
		return good
	}
	kept, err := x.rerank(ctx, query, good[:min(len(good), max(x.cfg.RerankCandidates, 1))])
	if err != nil {
		if ctx.Err() == nil {
			x.rerankCooldownUntil = time.Now().Add(x.cfg.FailureCooldown)
			log.Printf("recall: reranking failed (%v); cosine order for %.0fs", err, x.cfg.FailureCooldown.Seconds())
		}
		return good
	}
	return kept
}

// keyword is the fallback over the facts scope can see, read from the files.
func (x *Index) keyword(query, scope string) []hit {
	var docs []factDoc
	for _, sc := range []string{"common", "gateway"} {
		if !visible(scope, sc) {
			continue
		}
		facts, err := x.store.Facts(sc)
		if err != nil {
			continue
		}
		for _, f := range facts {
			docs = append(docs, factDoc{f.ID, f.Text, keywordTerms(f.Text)})
		}
	}
	return keywordHits(query, docs)
}

// Similar returns the scope's facts whose embedding is within 0.92 cosine of
// text; nil when the embedder cannot answer (the caller then uses Jaccard).
func (x *Index) Similar(ctx context.Context, scope, text string) []memory.Fact {
	scope = canon(scope)
	x.mu.Lock()
	defer x.mu.Unlock()
	if time.Now().Before(x.cooldownUntil) {
		return nil
	}
	fail := func(err error) []memory.Fact {
		x.cooldownUntil = time.Now().Add(x.cfg.FailureCooldown)
		log.Printf("recall: duplicate check failed (%v); skipping the embedder for %.0fs", err, x.cfg.FailureCooldown.Seconds())
		return nil
	}
	if err := x.refresh(ctx); err != nil {
		return fail(err)
	}
	vs, err := x.embed(ctx, []string{x.cfg.DocumentPrompt + strings.Join(strings.Fields(text), " ")})
	if err != nil {
		return fail(err)
	}
	rows, err := x.db.QueryContext(ctx, `SELECT key, text, vec FROM items WHERE kind = ? AND scope = ?`, factKind, scope)
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := []memory.Fact{}
	for rows.Next() {
		var key, t string
		var vec []byte
		if rows.Scan(&key, &t, &vec) != nil {
			return nil
		}
		if cosine(vs[0], decode(vec)) >= similarThreshold {
			out = append(out, memory.Fact{ID: strings.TrimPrefix(key, factKind+":"+scope+":"), Text: t})
		}
	}
	return out
}
