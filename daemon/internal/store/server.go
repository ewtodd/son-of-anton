package store

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"time"
)

// UpdateSessionSettings stores a session's model, reasoning effort and
// permission mode. An unknown session is an error.
func (s *Store) UpdateSessionSettings(ctx context.Context, id, model, effort, mode string) error {
	res, err := s.db.ExecContext(ctx,
		`UPDATE sessions SET model = ?, reasoning_effort = ?, mode = ? WHERE id = ?`,
		nullStr(model), nullStr(effort), nullStr(mode), id)
	if err != nil {
		return err
	}
	if n, err := res.RowsAffected(); err == nil && n == 0 {
		return fmt.Errorf("store: unknown session %q", id)
	}
	return nil
}

// QueueItem is one pending send. Kind is "user" or "notice".
type QueueItem struct {
	ID, Kind, Text string
	Created        time.Time
}

// QueuePush appends an item to the session's queue.
func (s *Store) QueuePush(ctx context.Context, sessionID, kind, text string) (QueueItem, error) {
	b := make([]byte, 6)
	if _, err := rand.Read(b); err != nil {
		return QueueItem{}, err
	}
	it := QueueItem{ID: "q_" + hex.EncodeToString(b), Kind: kind, Text: text, Created: time.Now()}
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO queue (id, session_id, kind, text, created) VALUES (?, ?, ?, ?, ?)`,
		it.ID, sessionID, kind, text, formatTime(it.Created))
	return it, err
}

// QueueItems returns the session's queue, oldest first.
func (s *Store) QueueItems(ctx context.Context, sessionID string) ([]QueueItem, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT id, kind, text, created FROM queue WHERE session_id = ? ORDER BY n`, sessionID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []QueueItem
	for rows.Next() {
		var it QueueItem
		var created string
		if err := rows.Scan(&it.ID, &it.Kind, &it.Text, &created); err != nil {
			return nil, err
		}
		if it.Created, err = parseTime(created); err != nil {
			return nil, err
		}
		out = append(out, it)
	}
	return out, rows.Err()
}

// QueueRemove deletes items by ID; unknown IDs are ignored.
func (s *Store) QueueRemove(ctx context.Context, sessionID string, ids ...string) error {
	for _, id := range ids {
		if _, err := s.db.ExecContext(ctx, `DELETE FROM queue WHERE session_id = ? AND id = ?`, sessionID, id); err != nil {
			return err
		}
	}
	return nil
}

// QueueClear empties the session's queue.
func (s *Store) QueueClear(ctx context.Context, sessionID string) error {
	_, err := s.db.ExecContext(ctx, `DELETE FROM queue WHERE session_id = ?`, sessionID)
	return err
}

// Summary is a session with what a list needs.
type Summary struct {
	Session  Session
	Updated  time.Time
	Preview  string
	Messages int
}

const previewMax = 100

// ListSessions returns the top-level sessions of the given kinds, newest
// activity first.
func (s *Store) ListSessions(ctx context.Context, kinds []string, limit int) ([]Summary, error) {
	return s.ListSessionsIn(ctx, kinds, "", limit)
}

// ListSessionsIn is ListSessions limited to one workspace (Session.Workspace);
// an empty workspace means every session.
func (s *Store) ListSessionsIn(ctx context.Context, kinds []string, workspace string, limit int) ([]Summary, error) {
	return s.listSessions(ctx, kinds, workspace, "", nil, limit)
}

// ListReadable is ListSessions with the wall: only sessions the caller can
// read (Readable), decided in the query.
func (s *Store) ListReadable(ctx context.Context, caller string, kinds []string, limit int) ([]Summary, error) {
	return s.ListReadableIn(ctx, caller, "", "", kinds, limit)
}

// ListReadableIn is ListReadable for a gateway caller walled to chat
// (ReadableFrom), limited to one workspace; an empty workspace means every
// one.
func (s *Store) ListReadableIn(ctx context.Context, caller, chat, workspace string, kinds []string, limit int) ([]Summary, error) {
	wall, args := wallSQL(caller, chat)
	return s.listSessions(ctx, kinds, workspace, wall, args, limit)
}

func (s *Store) listSessions(ctx context.Context, kinds []string, workspace, wall string, wallArgs []any, limit int) ([]Summary, error) {
	var out []Summary
	for _, kind := range kinds {
		where, args := `s.parent_id IS NULL AND s.kind = ?`, []any{kind}
		if wall != "" {
			where += ` AND ` + wall
			args = append(args, wallArgs...)
		}
		if workspace != "" {
			where, args = where+` AND s.workspace = ?`, append(args, workspace)
		}
		part, err := s.summaries(ctx, where, args...)
		if err != nil {
			return nil, err
		}
		out = append(out, part...)
	}
	sort.SliceStable(out, func(i, j int) bool {
		if !out[i].Updated.Equal(out[j].Updated) {
			return out[i].Updated.After(out[j].Updated)
		}
		return out[i].Session.ID > out[j].Session.ID
	})
	if limit > 0 && len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

// SessionSummary is ListSessions for one session of any kind.
func (s *Store) SessionSummary(ctx context.Context, id string) (Summary, error) {
	got, err := s.summaries(ctx, `s.id = ?`, id)
	if err != nil {
		return Summary{}, err
	}
	if len(got) == 0 {
		return Summary{}, ErrNotFound
	}
	return got[0], nil
}

func (s *Store) summaries(ctx context.Context, where string, args ...any) ([]Summary, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT s.id,
		(SELECT ts FROM messages WHERE session_id = s.id ORDER BY seq DESC LIMIT 1),
		(SELECT content FROM messages WHERE session_id = s.id AND role = 'user' ORDER BY seq LIMIT 1),
		(SELECT COUNT(*) FROM messages WHERE session_id = s.id)
		FROM sessions s WHERE `+where, args...)
	if err != nil {
		return nil, err
	}
	type row struct {
		id, ts, preview *string
		n               int
	}
	var found []row
	for rows.Next() {
		var r row
		if err := rows.Scan(&r.id, &r.ts, &r.preview, &r.n); err != nil {
			rows.Close()
			return nil, err
		}
		found = append(found, r)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	var out []Summary
	for _, r := range found {
		sess, err := s.Session(ctx, *r.id)
		if err != nil {
			return nil, err
		}
		sum := Summary{Session: sess, Updated: sess.Created, Messages: r.n}
		if r.ts != nil {
			if sum.Updated, err = parseTime(*r.ts); err != nil {
				return nil, err
			}
		}
		if r.preview != nil {
			sum.Preview = Preview(*r.preview)
		}
		out = append(out, sum)
	}
	return out, nil
}

// Preview cuts text to the length a session list shows.
func Preview(text string) string {
	if r := []rune(text); len(r) > previewMax {
		return string(r[:previewMax])
	}
	return text
}

// MessagesBefore returns up to limit rows with seq below beforeSeq (0 = from
// the end), oldest first, and whether older rows exist.
func (s *Store) MessagesBefore(ctx context.Context, id string, beforeSeq int64, limit int) ([]Message, bool, error) {
	got, err := s.queryMessages(ctx,
		`WHERE session_id = ? AND (? = 0 OR seq < ?) ORDER BY seq DESC LIMIT ?`, id, beforeSeq, beforeSeq, limit+1)
	if err != nil {
		return nil, false, err
	}
	more := len(got) > limit
	if more {
		got = got[:limit]
	}
	for i, j := 0, len(got)-1; i < j; i, j = i+1, j-1 {
		got[i], got[j] = got[j], got[i]
	}
	return got, more, nil
}

// MessagesAfter returns up to limit rows with seq above afterSeq, oldest
// first, and whether newer rows exist.
func (s *Store) MessagesAfter(ctx context.Context, id string, afterSeq int64, limit int) ([]Message, bool, error) {
	got, err := s.queryMessages(ctx,
		`WHERE session_id = ? AND seq > ? ORDER BY seq LIMIT ?`, id, afterSeq, limit+1)
	if err != nil {
		return nil, false, err
	}
	more := len(got) > limit
	if more {
		got = got[:limit]
	}
	return got, more, nil
}

// Compaction is a summary of a session's rows up to ThroughSeq.
type Compaction struct {
	ThroughSeq   int64
	Summary      string
	BeforeTokens int
	Created      time.Time
}

// AddCompaction records a compaction and the rebuilt prompt's hash in one
// transaction. It refuses a ThroughSeq that is not beyond the last
// compaction's, or past the last row.
func (s *Store) AddCompaction(ctx context.Context, sessionID string, c Compaction, promptHash string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	// Write first: a transaction that reads before it writes fails with
	// SQLITE_BUSY when another writer commits in between.
	res, err := tx.ExecContext(ctx, `UPDATE sessions SET prompt_hash = ? WHERE id = ?`, promptHash, sessionID)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return fmt.Errorf("store: unknown session %q", sessionID)
	}
	var last, maxSeq int64
	if err := tx.QueryRowContext(ctx, `SELECT COALESCE(MAX(through_seq), 0) FROM compactions WHERE session_id = ?`, sessionID).Scan(&last); err != nil {
		return err
	}
	if err := tx.QueryRowContext(ctx, `SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session_id = ?`, sessionID).Scan(&maxSeq); err != nil {
		return err
	}
	if c.ThroughSeq <= last || c.ThroughSeq > maxSeq {
		return fmt.Errorf("store: compaction through %d: must be beyond %d and within %d rows", c.ThroughSeq, last, maxSeq)
	}
	created := c.Created
	if created.IsZero() {
		created = time.Now()
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO compactions (session_id, through_seq, summary, before_tokens, created) VALUES (?, ?, ?, ?, ?)`,
		sessionID, c.ThroughSeq, c.Summary, c.BeforeTokens, formatTime(created)); err != nil {
		return err
	}
	return tx.Commit()
}

// Compactions returns the session's compactions, oldest first.
func (s *Store) Compactions(ctx context.Context, sessionID string) ([]Compaction, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT through_seq, summary, before_tokens, created FROM compactions WHERE session_id = ? ORDER BY through_seq`, sessionID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Compaction
	for rows.Next() {
		var c Compaction
		var created string
		if err := rows.Scan(&c.ThroughSeq, &c.Summary, &c.BeforeTokens, &created); err != nil {
			return nil, err
		}
		if c.Created, err = parseTime(created); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

var titleRank = map[string]int{"derived": 1, "llm": 2, "user": 3}

// SetTitle sets a session's title unless the current one came from a higher
// source (derived < llm < user). changed says whether it was set.
func (s *Store) SetTitle(ctx context.Context, id, title, source string) (changed bool, err error) {
	rank, ok := titleRank[source]
	if !ok {
		return false, fmt.Errorf("store: unknown title source %q", source)
	}
	// One statement, so a concurrent writer cannot make it fail with SQLITE_BUSY.
	res, err := s.db.ExecContext(ctx,
		`UPDATE sessions SET title = ?, title_source = ? WHERE id = ?
		 AND (CASE COALESCE(title_source, '') WHEN 'derived' THEN 1 WHEN 'llm' THEN 2 WHEN 'user' THEN 3 ELSE 0 END) <= ?`,
		nullStr(title), source, id, rank)
	if err != nil {
		return false, err
	}
	if n, _ := res.RowsAffected(); n > 0 {
		return true, nil
	}
	var one int
	if err := s.db.QueryRowContext(ctx, `SELECT 1 FROM sessions WHERE id = ?`, id).Scan(&one); errors.Is(err, sql.ErrNoRows) {
		return false, ErrNotFound
	} else if err != nil {
		return false, err
	}
	return false, nil
}

// ChatSession is the id of a chat's current session, ErrNotFound when the
// chat has none.
func (s *Store) ChatSession(ctx context.Context, chat string) (string, error) {
	var id string
	err := s.db.QueryRowContext(ctx, `SELECT session_id FROM chats WHERE chat = ?`, chat).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return "", ErrNotFound
	}
	return id, err
}

// SetChatSession makes id the chat's current session; an empty id leaves the
// chat without one (the next message starts a new session).
func (s *Store) SetChatSession(ctx context.Context, chat, id string) error {
	if id == "" {
		_, err := s.db.ExecContext(ctx, `DELETE FROM chats WHERE chat = ?`, chat)
		return err
	}
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO chats (chat, session_id) VALUES (?, ?)
		 ON CONFLICT(chat) DO UPDATE SET session_id = excluded.session_id`, chat, id)
	return err
}

// RelayBinding is the worker a chat last started or sent to, ErrNotFound when
// the chat has none.
func (s *Store) RelayBinding(ctx context.Context, chat string) (string, error) {
	var id string
	err := s.db.QueryRowContext(ctx, `SELECT session_id FROM relay_bindings WHERE chat = ?`, chat).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return "", ErrNotFound
	}
	return id, err
}

// SetRelayBinding binds the chat to a worker; an empty id unbinds it.
func (s *Store) SetRelayBinding(ctx context.Context, chat, id string) error {
	if id == "" {
		_, err := s.db.ExecContext(ctx, `DELETE FROM relay_bindings WHERE chat = ?`, chat)
		return err
	}
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO relay_bindings (chat, session_id) VALUES (?, ?)
		 ON CONFLICT(chat) DO UPDATE SET session_id = excluded.session_id`, chat, id)
	return err
}

// QueuedWorkers lists the worker sessions that have queued messages.
func (s *Store) QueuedWorkers(ctx context.Context) ([]string, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT DISTINCT q.session_id FROM queue q JOIN sessions s ON s.id = q.session_id WHERE s.kind = 'worker'`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out = append(out, id)
	}
	return out, rows.Err()
}

// RenameModel points every session pinned to the model old at new, and
// returns how many it changed.
func (s *Store) RenameModel(ctx context.Context, old, new string) (int64, error) {
	res, err := s.db.ExecContext(ctx, `UPDATE sessions SET model = ? WHERE model = ?`, new, old)
	if err != nil {
		return 0, err
	}
	return res.RowsAffected()
}

// SessionTree is a session's id and those of all its descendants (parent
// first). ErrNotFound when the session does not exist.
func (s *Store) SessionTree(ctx context.Context, id string) ([]string, error) {
	if _, err := s.Session(ctx, id); err != nil {
		return nil, err
	}
	ids := []string{id}
	for i := 0; i < len(ids); i++ {
		rows, err := s.db.QueryContext(ctx, `SELECT id FROM sessions WHERE parent_id = ? ORDER BY rowid`, ids[i])
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var kid string
			if err := rows.Scan(&kid); err != nil {
				rows.Close()
				return nil, err
			}
			ids = append(ids, kid)
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return nil, err
		}
	}
	return ids, nil
}

// DeleteSessions removes the sessions and everything stored for them (messages
// and their search entries, compactions, queue, relay bindings, the foreign
// import record), in one transaction.
func (s *Store) DeleteSessions(ctx context.Context, ids []string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, id := range ids {
		for _, q := range []string{
			`INSERT INTO messages_fts (messages_fts, rowid, content) SELECT 'delete', rowid, content FROM messages WHERE session_id = ?1`,
			`DELETE FROM messages WHERE session_id = ?1`,
			`DELETE FROM compactions WHERE session_id = ?1`,
			`DELETE FROM queue WHERE session_id = ?1`,
			`DELETE FROM chats WHERE session_id = ?1`,
			`DELETE FROM relay_bindings WHERE session_id = ?1`,
			`DELETE FROM foreign_imports WHERE session_id = ?1`,
			`DELETE FROM sessions WHERE id = ?1`,
		} {
			if _, err := tx.ExecContext(ctx, q, id); err != nil {
				return err
			}
		}
	}
	return tx.Commit()
}
