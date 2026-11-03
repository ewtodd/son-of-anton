// Package store is the daemon's SQLite state: sessions, system prompts,
// the message transcript and full-text search.
package store

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

// ErrNotFound is returned when a session or prompt does not exist.
var ErrNotFound = errors.New("store: not found")

// Store is an open state database.
type Store struct {
	db *sql.DB
}

// Open opens (creating if needed) the SQLite database at path, enables WAL
// and migrates it to the latest schema version. The database holds
// conversations and the prompt's rules, so it and its WAL files are owner-only.
func Open(path string) (*Store, error) {
	if f, err := os.OpenFile(path, os.O_RDONLY|os.O_CREATE, 0o600); err == nil {
		_ = f.Close()
	}
	for _, p := range []string{path, path + "-wal", path + "-shm"} {
		_ = os.Chmod(p, 0o600) // the WAL files may not exist yet
	}
	// busy_timeout is per connection: set it in the DSN so every pooled
	// connection waits for a concurrent writer instead of failing at once.
	// Transactions begin IMMEDIATE: one that reads before it writes (as
	// AppendMessages does) would otherwise fail with SQLITE_BUSY, without
	// waiting, when another connection commits in between.
	db, err := sql.Open("sqlite", path+"?_pragma=busy_timeout(5000)&_txlock=immediate")
	if err != nil {
		return nil, err
	}
	if _, err := db.Exec("PRAGMA journal_mode=WAL"); err != nil {
		db.Close()
		return nil, fmt.Errorf("store: enabling WAL: %w", err)
	}
	if _, err := db.Exec("PRAGMA busy_timeout=5000"); err != nil {
		db.Close()
		return nil, fmt.Errorf("store: setting busy_timeout: %w", err)
	}
	if err := migrate(db); err != nil {
		db.Close()
		return nil, err
	}
	return &Store{db: db}, nil
}

// Close closes the database.
func (s *Store) Close() error { return s.db.Close() }

// SchemaVersion returns the applied schema version.
func (s *Store) SchemaVersion() (int, error) {
	var v int
	err := s.db.QueryRow("SELECT version FROM schema_version WHERE rowid = 1").Scan(&v)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, ErrNotFound
	}
	return v, err
}

// migrations are applied in order; index i+1 is version i+1.
var migrations = []string{
	`CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version (version) VALUES (1);

CREATE TABLE sessions (
	id          TEXT PRIMARY KEY,
	kind        TEXT NOT NULL,
	parent_id   TEXT,
	source      TEXT,
	title       TEXT,
	cwd         TEXT,
	model       TEXT,
	prompt_hash TEXT,
	created     TEXT NOT NULL,
	ended       TEXT
);

CREATE TABLE system_prompts (
	hash TEXT PRIMARY KEY,
	text TEXT NOT NULL
);

CREATE TABLE messages (
	rowid        INTEGER PRIMARY KEY,
	session_id   TEXT NOT NULL REFERENCES sessions(id),
	seq          INTEGER NOT NULL,
	role         TEXT NOT NULL,
	content      TEXT NOT NULL DEFAULT '',
	api_content  TEXT,
	reasoning    TEXT,
	tool_calls   TEXT,
	tool_call_id TEXT,
	tool_name    TEXT,
	ts           TEXT NOT NULL,
	UNIQUE (session_id, seq)
);

CREATE VIRTUAL TABLE messages_fts USING fts5(
	content,
	content='messages',
	content_rowid='rowid'
);`,
	`CREATE TABLE approval_allow (
	key        TEXT PRIMARY KEY,
	created_at TEXT NOT NULL
);
UPDATE schema_version SET version = 2;`,
	`ALTER TABLE sessions ADD COLUMN reasoning_effort TEXT;
ALTER TABLE sessions ADD COLUMN mode TEXT;
CREATE TABLE queue (
	n          INTEGER PRIMARY KEY AUTOINCREMENT,
	id         TEXT NOT NULL UNIQUE,
	session_id TEXT NOT NULL REFERENCES sessions(id),
	kind       TEXT NOT NULL,
	text       TEXT NOT NULL,
	created    TEXT NOT NULL
);
CREATE INDEX queue_session ON queue (session_id, n);
UPDATE schema_version SET version = 3;`,
	`ALTER TABLE sessions ADD COLUMN workspace TEXT;
UPDATE sessions SET workspace = cwd WHERE workspace IS NULL;
CREATE INDEX sessions_workspace ON sessions (workspace);
UPDATE schema_version SET version = 4;`,
	`CREATE TABLE compactions (
	session_id    TEXT NOT NULL REFERENCES sessions(id),
	through_seq   INTEGER NOT NULL,
	summary       TEXT NOT NULL,
	before_tokens INTEGER NOT NULL DEFAULT 0,
	created       TEXT NOT NULL,
	PRIMARY KEY (session_id, through_seq)
);
ALTER TABLE sessions ADD COLUMN title_source TEXT;
UPDATE schema_version SET version = 5;`,
	`ALTER TABLE sessions ADD COLUMN scope TEXT NOT NULL DEFAULT 'common';
ALTER TABLE sessions ADD COLUMN chat TEXT;
UPDATE schema_version SET version = 6;`,
	`ALTER TABLE sessions ADD COLUMN agent TEXT;
UPDATE schema_version SET version = 7;`,
	`ALTER TABLE sessions ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN reasoning_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN api_calls INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN context_tokens INTEGER NOT NULL DEFAULT 0;
UPDATE schema_version SET version = 8;`,
}

func migrate(db *sql.DB) error {
	current, err := appliedVersion(db)
	if err != nil {
		return err
	}
	for v := current + 1; v <= len(migrations); v++ {
		tx, err := db.Begin()
		if err != nil {
			return err
		}
		if _, err := tx.Exec(migrations[v-1]); err != nil {
			tx.Rollback()
			return fmt.Errorf("store: applying schema v%d: %w", v, err)
		}
		if err := tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}

func appliedVersion(db *sql.DB) (int, error) {
	var v int
	err := db.QueryRow("SELECT version FROM schema_version WHERE rowid = 1").Scan(&v)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, nil
	}
	if err != nil {
		// A fresh database has no schema_version table yet.
		if strings.Contains(err.Error(), "no such table: schema_version") {
			return 0, nil
		}
		return 0, err
	}
	return v, nil
}

// Session is one stored conversation.
type Session struct {
	ID       string
	Kind     string
	ParentID string
	Source   string
	Title    string
	Cwd      string
	// Workspace is where -c looks: the git root of Cwd, else Cwd itself.
	Workspace  string
	Model      string
	PromptHash string
	Created    time.Time
	Ended      time.Time
	// ReasoningEffort and Mode are the session's own settings ("" = the
	// configured default).
	ReasoningEffort string
	Mode            string
	// TitleSource says where Title came from: derived, llm or user.
	TitleSource string
	// Scope is "common" (the default) or "gateway"; Chat is a chat stamp such
	// as "signal:<id>". Together they are the wall (Readable).
	Scope string
	Chat  string
	// Agent is a child session's subagent type ("explore"); "" otherwise.
	Agent string
	// Usage is the session's token totals over all its turns; ContextTokens is
	// what its last request held.
	Usage         Usage
	ContextTokens int
}

// Usage is a session's token and request totals.
type Usage struct{ Input, Output, Cached, Reasoning, Calls int }

// newSessionID returns YYYYmmdd_HHMMSS_ plus six random hex chars (local
// time).
func newSessionID() (string, error) {
	b := make([]byte, 3)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return time.Now().Format("20060102_150405") + "_" + hex.EncodeToString(b), nil
}

// CreateSession stores a new session, generating its ID and Created.
func (s *Store) CreateSession(ctx context.Context, sess Session) (Session, error) {
	for {
		id, err := newSessionID()
		if err != nil {
			return Session{}, err
		}
		if sess.Created.IsZero() {
			sess.Created = time.Now()
		}
		sess.ID = id
		if sess.Scope == "" {
			sess.Scope = "common"
		}
		_, err = s.db.ExecContext(ctx,
			`INSERT INTO sessions (id, kind, parent_id, source, title, cwd, model, prompt_hash, created, ended, reasoning_effort, mode, workspace, scope, chat, title_source, agent)
			 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			sess.ID, sess.Kind, nullStr(sess.ParentID), nullStr(sess.Source), nullStr(sess.Title),
			nullStr(sess.Cwd), nullStr(sess.Model), nullStr(sess.PromptHash),
			formatTime(sess.Created), nullTime(sess.Ended), nullStr(sess.ReasoningEffort), nullStr(sess.Mode), nullStr(sess.Workspace), sess.Scope, nullStr(sess.Chat), nullStr(sess.TitleSource), nullStr(sess.Agent))
		if err == nil {
			return sess, nil
		}
		if !strings.Contains(err.Error(), "UNIQUE constraint failed") {
			return Session{}, err
		}
		// ID collision: draw a new one.
	}
}

// Session loads one session by ID.
func (s *Store) Session(ctx context.Context, id string) (Session, error) {
	var (
		sess     Session
		parentID sql.NullString
		source   sql.NullString
		title    sql.NullString
		cwd      sql.NullString
		model    sql.NullString
		hash     sql.NullString
		created  sql.NullString
		ended    sql.NullString
		effort   sql.NullString
		mode     sql.NullString
		work     sql.NullString
		tsource  sql.NullString
		chat     sql.NullString
		agent    sql.NullString
	)
	err := s.db.QueryRowContext(ctx,
		`SELECT id, kind, parent_id, source, title, cwd, model, prompt_hash, created, ended, reasoning_effort, mode, workspace, title_source, scope, chat, agent, input_tokens, output_tokens, cached_tokens, reasoning_tokens, api_calls, context_tokens
		 FROM sessions WHERE id = ?`, id).
		Scan(&sess.ID, &sess.Kind, &parentID, &source, &title, &cwd, &model, &hash, &created, &ended, &effort, &mode, &work, &tsource, &sess.Scope, &chat, &agent,
			&sess.Usage.Input, &sess.Usage.Output, &sess.Usage.Cached, &sess.Usage.Reasoning, &sess.Usage.Calls, &sess.ContextTokens)
	if errors.Is(err, sql.ErrNoRows) {
		return Session{}, ErrNotFound
	}
	if err != nil {
		return Session{}, err
	}
	sess.ParentID = parentID.String
	sess.Source = source.String
	sess.Title = title.String
	sess.Cwd = cwd.String
	sess.Model = model.String
	sess.PromptHash = hash.String
	sess.ReasoningEffort = effort.String
	sess.Mode = mode.String
	sess.Workspace = work.String
	sess.TitleSource = tsource.String
	sess.Chat = chat.String
	sess.Agent = agent.String
	sess.Created, err = parseTime(created.String)
	if err != nil {
		return Session{}, err
	}
	if ended.Valid {
		sess.Ended, err = parseTime(ended.String)
		if err != nil {
			return Session{}, err
		}
	}
	return sess, nil
}

// PutPrompt stores a system prompt and returns its sha256 hex hash. The text
// is stored only once, by hash.
func (s *Store) PutPrompt(ctx context.Context, text string) (string, error) {
	sum := sha256.Sum256([]byte(text))
	hash := hex.EncodeToString(sum[:])
	if _, err := s.db.ExecContext(ctx,
		`INSERT OR IGNORE INTO system_prompts (hash, text) VALUES (?, ?)`, hash, text); err != nil {
		return "", err
	}
	return hash, nil
}

// Prompt returns the stored text for a prompt hash.
func (s *Store) Prompt(ctx context.Context, hash string) (string, error) {
	var text string
	err := s.db.QueryRowContext(ctx,
		`SELECT text FROM system_prompts WHERE hash = ?`, hash).Scan(&text)
	if errors.Is(err, sql.ErrNoRows) {
		return "", ErrNotFound
	}
	return text, err
}

// Message is one transcript row.
type Message struct {
	Seq        int64
	Role       string
	Content    string
	APIContent string
	Reasoning  string
	ToolCalls  json.RawMessage
	ToolCallID string
	ToolName   string
	TS         time.Time
}

// AppendMessages appends messages to a session's transcript in one
// transaction, numbering them from 1 per session. It returns the assigned
// seqs. A message with a TS keeps it; the others are stamped with the time of
// the call. Appending to an unknown session fails and writes nothing.
func (s *Store) AppendMessages(ctx context.Context, sessionID string, msgs ...Message) ([]int64, error) {
	if len(msgs) == 0 {
		return nil, nil
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	var count int
	if err := tx.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM sessions WHERE id = ?`, sessionID).Scan(&count); err != nil {
		return nil, err
	}
	if count == 0 {
		return nil, fmt.Errorf("store: unknown session %q", sessionID)
	}

	var nextSeq int64
	if err := tx.QueryRowContext(ctx,
		`SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session_id = ?`, sessionID).Scan(&nextSeq); err != nil {
		return nil, err
	}

	now := time.Now()
	seqs := make([]int64, 0, len(msgs))
	for i := range msgs {
		nextSeq++
		m := &msgs[i]
		ts := now
		if !m.TS.IsZero() {
			ts = m.TS
		}
		res, err := tx.ExecContext(ctx,
			`INSERT INTO messages (session_id, seq, role, content, api_content, reasoning, tool_calls, tool_call_id, tool_name, ts)
			 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			sessionID, nextSeq, m.Role, m.Content, nullStr(m.APIContent), nullStr(m.Reasoning),
			nullStr(string(m.ToolCalls)), nullStr(m.ToolCallID), nullStr(m.ToolName), formatTime(ts))
		if err != nil {
			return nil, err
		}
		rowid, err := res.LastInsertId()
		if err != nil {
			return nil, err
		}
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO messages_fts (rowid, content) VALUES (?, ?)`, rowid, m.Content); err != nil {
			return nil, err
		}
		m.Seq = nextSeq
		m.TS = ts
		seqs = append(seqs, nextSeq)
	}
	return seqs, tx.Commit()
}

// Messages returns the session's transcript in seq order.
func (s *Store) Messages(ctx context.Context, sessionID string) ([]Message, error) {
	return s.queryMessages(ctx, `WHERE session_id = ? ORDER BY seq`, sessionID)
}

func (s *Store) queryMessages(ctx context.Context, tail string, args ...any) ([]Message, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT seq, role, content, api_content, reasoning, tool_calls, tool_call_id, tool_name, ts FROM messages `+tail, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []Message
	for rows.Next() {
		var (
			m          Message
			apiContent sql.NullString
			reasoning  sql.NullString
			toolCalls  sql.NullString
			toolCallID sql.NullString
			toolName   sql.NullString
			ts         string
		)
		if err := rows.Scan(&m.Seq, &m.Role, &m.Content, &apiContent, &reasoning,
			&toolCalls, &toolCallID, &toolName, &ts); err != nil {
			return nil, err
		}
		m.APIContent = apiContent.String
		m.Reasoning = reasoning.String
		if toolCalls.Valid {
			m.ToolCalls = json.RawMessage(toolCalls.String)
		}
		m.ToolCallID = toolCallID.String
		m.ToolName = toolName.String
		if m.TS, err = parseTime(ts); err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

// Hit is one search result.
type Hit struct {
	SessionID string
	Seq       int64
	Snippet   string
	Role      string
	TS        time.Time

	rowid int64 // the message row, for ordering a per-session search
}

// Search full-text-searches message content.
func (s *Store) Search(ctx context.Context, query string, limit int) ([]Hit, error) {
	if limit <= 0 {
		limit = 10
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT m.session_id, m.seq, snippet(messages_fts, 0, '«', '»', '…', 12)
		 FROM messages_fts
		 JOIN messages m ON m.rowid = messages_fts.rowid
		 WHERE messages_fts MATCH ?
		 ORDER BY rank
		 LIMIT ?`, query, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []Hit
	for rows.Next() {
		var h Hit
		if err := rows.Scan(&h.SessionID, &h.Seq, &h.Snippet); err != nil {
			return nil, err
		}
		out = append(out, h)
	}
	return out, rows.Err()
}

// Allowlist is the durable set of approval pattern keys the user chose to
// remember with "always". It persists across daemon restarts in soa-d.db.
// It satisfies the approval.Allowlist interface.
type Allowlist struct {
	db *sql.DB
}

// Allowlist returns the store's approval allowlist.
func (s *Store) Allowlist() *Allowlist { return &Allowlist{db: s.db} }

// Has reports whether key was approved "always".
func (a *Allowlist) Has(key string) bool {
	var one int
	err := a.db.QueryRow("SELECT 1 FROM approval_allow WHERE key = ?", key).Scan(&one)
	return err == nil
}

// Add records key as approved "always". Adding a key twice is a no-op.
func (a *Allowlist) Add(key string) error {
	_, err := a.db.Exec(
		"INSERT OR IGNORE INTO approval_allow (key, created_at) VALUES (?, ?)",
		key, formatTime(time.Now()),
	)
	return err
}

func nullStr(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func nullTime(t time.Time) any {
	if t.IsZero() {
		return nil
	}
	return formatTime(t)
}

func formatTime(t time.Time) string { return t.Format(time.RFC3339Nano) }

func parseTime(s string) (time.Time, error) {
	return time.Parse(time.RFC3339Nano, s)
}

// Children lists a session's child sessions (subagents) in creation order.
func (s *Store) Children(ctx context.Context, parentID string) ([]Session, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT id FROM sessions WHERE parent_id = ? ORDER BY rowid`, parentID)
	if err != nil {
		return nil, err
	}
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return nil, err
		}
		ids = append(ids, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	out := make([]Session, 0, len(ids))
	for _, id := range ids {
		sess, err := s.Session(ctx, id)
		if err != nil {
			return nil, err
		}
		out = append(out, sess)
	}
	return out, nil
}

// AddUsage adds u to the session's totals and, when contextTokens is above
// zero, sets the session's context to it.
func (s *Store) AddUsage(ctx context.Context, id string, u Usage, contextTokens int) error {
	res, err := s.db.ExecContext(ctx,
		`UPDATE sessions SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ?,
		 cached_tokens = cached_tokens + ?, reasoning_tokens = reasoning_tokens + ?, api_calls = api_calls + ?,
		 context_tokens = CASE WHEN ? > 0 THEN ? ELSE context_tokens END WHERE id = ?`,
		u.Input, u.Output, u.Cached, u.Reasoning, u.Calls, contextTokens, contextTokens, id)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return fmt.Errorf("store: unknown session %q", id)
	}
	return nil
}

// RunStats is a session's tool calls and how long its transcript spans.
type RunStats struct {
	ToolCalls int
	Duration  time.Duration
}

// RunStats counts the session's tool rows and measures its first row to its
// last (0 under two rows).
func (s *Store) RunStats(ctx context.Context, id string) (RunStats, error) {
	var rs RunStats
	var first, last sql.NullString
	err := s.db.QueryRowContext(ctx,
		`SELECT (SELECT COUNT(*) FROM messages WHERE session_id = ?1 AND role = 'tool'),
		        (SELECT ts FROM messages WHERE session_id = ?1 ORDER BY seq LIMIT 1),
		        (SELECT ts FROM messages WHERE session_id = ?1 ORDER BY seq DESC LIMIT 1)`, id).
		Scan(&rs.ToolCalls, &first, &last)
	if err != nil {
		return RunStats{}, err
	}
	if !first.Valid || first.String == last.String {
		return rs, nil
	}
	t0, err := parseTime(first.String)
	if err != nil {
		return RunStats{}, err
	}
	t1, err := parseTime(last.String)
	if err != nil {
		return RunStats{}, err
	}
	rs.Duration = t1.Sub(t0)
	return rs, nil
}
