package store

import (
	"context"
	"strings"
	"time"
)

// Readable is the wall: a gateway caller reads every session; any other
// caller (common, or unknown, which counts as the terminal) reads only
// sessions of scope common with no chat stamp. wallSQL is the same rule as a
// condition on the sessions table aliased s.
func Readable(caller string, s Session) bool {
	if caller == "gateway" {
		return true
	}
	return (s.Scope == "" || s.Scope == "common") && s.Chat == ""
}

func wallSQL(caller string) string {
	if caller == "gateway" {
		return ""
	}
	return `s.scope = 'common' AND COALESCE(s.chat, '') = ''`
}

// MessageQuery is one message search.
type MessageQuery struct {
	Query   string   // an FTS5 expression
	Caller  string   // the wall: rows of sessions the caller cannot read never match
	Roles   []string // e.g. user, assistant (and tool when asked)
	Sort    string   // "" (relevance), "newest", "oldest"
	Exclude string   // a session id to leave out (the caller's own)
	Limit   int
}

// SearchMessages full-text-searches message content. The wall, the roles and
// Exclude are conditions of the query itself, so rows the caller may not
// read can never crowd out ones it may. An FTS5 syntax error retries once
// with every term quoted.
func (s *Store) SearchMessages(ctx context.Context, q MessageQuery) ([]Hit, error) {
	hits, err := s.searchMessages(ctx, q, q.Query)
	if err != nil && ctx.Err() == nil {
		if quoted := quoteTerms(q.Query); quoted != "" && quoted != q.Query {
			return s.searchMessages(ctx, q, quoted)
		}
	}
	return hits, err
}

func quoteTerms(query string) string {
	terms := strings.Fields(query)
	for i, t := range terms {
		terms[i] = `"` + strings.ReplaceAll(t, `"`, `""`) + `"`
	}
	return strings.Join(terms, " ")
}

func (s *Store) searchMessages(ctx context.Context, q MessageQuery, match string) ([]Hit, error) {
	limit := q.Limit
	if limit <= 0 {
		limit = 10
	}
	roles := q.Roles
	if len(roles) == 0 {
		roles = []string{"user", "assistant"}
	}
	where := `messages_fts MATCH ? AND m.role IN (` + strings.TrimSuffix(strings.Repeat("?,", len(roles)), ",") + `)`
	args := []any{match}
	for _, r := range roles {
		args = append(args, r)
	}
	if w := wallSQL(q.Caller); w != "" {
		where += ` AND ` + w
	}
	if q.Exclude != "" {
		where += ` AND m.session_id <> ?`
		args = append(args, q.Exclude)
	}
	order := `rank`
	switch q.Sort {
	case "newest":
		order = `m.rowid DESC`
	case "oldest":
		order = `m.rowid ASC`
	}
	args = append(args, limit)
	rows, err := s.db.QueryContext(ctx,
		`SELECT m.session_id, m.seq, snippet(messages_fts, 0, '«', '»', '…', 12), m.role, m.ts
		 FROM messages_fts
		 JOIN messages m ON m.rowid = messages_fts.rowid
		 JOIN sessions s ON s.id = m.session_id
		 WHERE `+where+` ORDER BY `+order+` LIMIT ?`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Hit
	for rows.Next() {
		var h Hit
		var ts string
		if err := rows.Scan(&h.SessionID, &h.Seq, &h.Snippet, &h.Role, &ts); err != nil {
			return nil, err
		}
		if h.TS, err = time.Parse(time.RFC3339Nano, ts); err != nil {
			return nil, err
		}
		out = append(out, h)
	}
	return out, rows.Err()
}

// SessionIDs lists every session id, newest first.
func (s *Store) SessionIDs(ctx context.Context) ([]string, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT id FROM sessions ORDER BY id DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}
