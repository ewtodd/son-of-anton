package recall

import (
	"context"
	"fmt"
	"log"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

const messageKind = "message"

// MessageHit is a meaning-based match on one conversation message.
type MessageHit struct {
	SessionID string
	Seq       int64
	Text      string
	Score     float64
}

func messageKey(sessionID string, seq int64, chunk int) string {
	return fmt.Sprintf("%s:%s:%d#%d", messageKind, sessionID, seq, chunk)
}

// parseMessageKey is the inverse of messageKey; a session id has no colon.
func parseMessageKey(key string) (sessionID string, seq int64, ok bool) {
	rest, found := strings.CutPrefix(key, messageKind+":")
	if !found {
		return "", 0, false
	}
	rest, _, _ = strings.Cut(rest, "#")
	i := strings.LastIndex(rest, ":")
	if i < 0 {
		return "", 0, false
	}
	n, err := strconv.ParseInt(rest[i+1:], 10, 64)
	return rest[:i], n, err == nil
}

// indexable says whether a row is indexed: a user row, or an assistant row
// without tool calls, by its content only.
func indexable(m store.Message) bool {
	if strings.TrimSpace(m.Content) == "" {
		return false
	}
	switch m.Role {
	case "user":
		return true
	case "assistant":
		calls := strings.TrimSpace(string(m.ToolCalls))
		return calls == "" || calls == "null" || calls == "[]"
	}
	return false
}

// failed starts the embedder cooldown after err.
func (x *Index) failed(what string, err error) error {
	x.mu.Lock()
	x.cooldownUntil = time.Now().Add(x.cfg.FailureCooldown)
	x.mu.Unlock()
	log.Printf("recall: %s failed (%v); skipping the embedder for %.0fs", what, err, x.cfg.FailureCooldown.Seconds())
	return err
}

func (x *Index) coolingDown() bool {
	x.mu.Lock()
	defer x.mu.Unlock()
	return time.Now().Before(x.cooldownUntil)
}

// IndexMessages embeds the conversation rows of a session for session_search,
// stamped with the session's scope. A row is embedded once (keyed by session
// and seq). It does not hold the index lock while it embeds, so it never
// delays a recall.
func (x *Index) IndexMessages(ctx context.Context, sessionID, scope string, rows []store.Message) error {
	scope = canon(scope)
	var want []item
	for _, m := range rows {
		if !indexable(m) {
			continue
		}
		for i, c := range chunkText(m.Content, x.cfg.MaxChunkChars) {
			want = append(want, item{key: messageKey(sessionID, m.Seq, i), kind: messageKind, scope: scope,
				label: fmt.Sprintf("session %s message %d", sessionID, m.Seq), text: c, hash: x.hashOf(c)})
		}
	}
	if len(want) == 0 {
		return nil
	}
	x.msgMu.Lock()
	defer x.msgMu.Unlock()
	have, err := x.messageHashes(ctx, sessionID)
	if err != nil {
		return err
	}
	var fresh []item
	for _, it := range want {
		if have[it.key] != it.hash+"\x00"+scope {
			fresh = append(fresh, it)
		}
	}
	if len(fresh) == 0 {
		return nil
	}
	if x.coolingDown() {
		return fmt.Errorf("recall: the embedder is cooling down after a failure")
	}
	for start := 0; start < len(fresh); start += embedBatch {
		batch := fresh[start:min(start+embedBatch, len(fresh))]
		inputs := make([]string, len(batch))
		for i, it := range batch {
			inputs[i] = x.cfg.DocumentPrompt + it.text
		}
		vs, err := x.embed(ctx, inputs)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return x.failed("indexing conversation text", err)
		}
		tx, err := x.db.BeginTx(ctx, nil)
		if err != nil {
			return err
		}
		for i, it := range batch {
			if _, err := tx.ExecContext(ctx, `INSERT OR REPLACE INTO items (key, kind, scope, label, text, hash, vec) VALUES (?,?,?,?,?,?,?)`,
				it.key, it.kind, it.scope, it.label, it.text, it.hash, encode(vs[i])); err != nil {
				_ = tx.Rollback()
				return err
			}
		}
		if err := tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}

// messageHashes maps the keys already indexed for a session to their hash and scope (joined by a NUL).
func (x *Index) messageHashes(ctx context.Context, sessionID string) (map[string]string, error) {
	prefix := messageKind + ":" + sessionID + ":"
	rows, err := x.db.QueryContext(ctx, `SELECT key, hash, scope FROM items WHERE substr(key, 1, ?) = ?`, len(prefix), prefix)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	have := map[string]string{}
	for rows.Next() {
		var k, h, sc string
		if err := rows.Scan(&k, &h, &sc); err != nil {
			return nil, err
		}
		have[k] = h + "\x00" + sc
	}
	return have, rows.Err()
}

// SearchMessages ranks the indexed conversation text against query for the
// sessions scope can read (common: common only; gateway: all), at least
// MinScore, at most k, best first, one hit per message. An embedder that
// does not answer is an error: the caller then searches by words only.
func (x *Index) SearchMessages(ctx context.Context, scope, query string, k int) ([]MessageHit, error) {
	query = strings.TrimSpace(query)
	if query == "" || k <= 0 {
		return nil, nil
	}
	scope = canon(scope)
	if x.coolingDown() {
		return nil, fmt.Errorf("recall: the embedder is cooling down after a failure")
	}
	qvs, err := x.embed(ctx, []string{x.cfg.QueryPrompt + query})
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, x.failed("searching conversation text", err)
	}
	rows, err := x.db.QueryContext(ctx, `SELECT key, scope, text, vec FROM items WHERE kind = ?`, messageKind)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	best := map[string]MessageHit{}
	for rows.Next() {
		var key, sc, text string
		var vec []byte
		if err := rows.Scan(&key, &sc, &text, &vec); err != nil {
			return nil, err
		}
		if !visible(scope, sc) {
			continue
		}
		score := cosine(qvs[0], decode(vec))
		sid, seq, ok := parseMessageKey(key)
		if !ok || score < x.cfg.MinScore {
			continue
		}
		id := fmt.Sprintf("%s:%d", sid, seq)
		if h, dup := best[id]; !dup || score > h.Score {
			best[id] = MessageHit{SessionID: sid, Seq: seq, Text: text, Score: score}
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	out := make([]MessageHit, 0, len(best))
	for _, h := range best {
		out = append(out, h)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Score != out[j].Score {
			return out[i].Score > out[j].Score
		}
		if out[i].SessionID != out[j].SessionID {
			return out[i].SessionID < out[j].SessionID
		}
		return out[i].Seq < out[j].Seq
	})
	return out[:min(len(out), k)], nil
}
