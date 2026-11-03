package store

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
)

// Foreign names a session in another tool's store: the source ("claude-code",
// "codex-cli", "opencode") and that tool's own id for it.
type Foreign struct{ Source, ID string }

// ImportOpts are the choices of one ImportSession.
type ImportOpts struct {
	MakeCurrent bool     // the session becomes its chat's current one if that chat has none
	Foreign     *Foreign // recorded, so the same foreign session is not imported twice
	Force       bool     // import a Foreign that was already imported anyway
}

// ImportSession writes a session, its prompt text and its rows in one
// transaction. A session without an ID gets one; Created is the caller's. A
// session whose ID is already stored, or whose Foreign was already imported
// (unless Force), is left alone and reported as present; its Foreign is
// still recorded.
func (s *Store) ImportSession(ctx context.Context, sess Session, promptText string, msgs []Message, o ImportOpts) (present bool, err error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()

	if o.Foreign != nil && !o.Force {
		var id string
		switch err := tx.QueryRowContext(ctx, `SELECT session_id FROM foreign_imports WHERE source = ? AND foreign_id = ?`,
			o.Foreign.Source, o.Foreign.ID).Scan(&id); {
		case err == nil:
			return true, nil
		case !errors.Is(err, sql.ErrNoRows):
			return false, err
		}
	}
	given := sess.ID != ""
	var one int
	for {
		if !given {
			if sess.ID, err = newSessionID(); err != nil {
				return false, err
			}
		}
		err = tx.QueryRowContext(ctx, `SELECT 1 FROM sessions WHERE id = ?`, sess.ID).Scan(&one)
		if err == nil && !given {
			continue // a drawn id that is taken
		}
		break
	}
	switch {
	case err == nil:
		if o.Foreign != nil {
			if err := recordForeign(ctx, tx, *o.Foreign, sess.ID, false); err != nil {
				return false, err
			}
		}
		return true, tx.Commit()
	case !errors.Is(err, sql.ErrNoRows):
		return false, err
	}
	if promptText != "" {
		sum := sha256.Sum256([]byte(promptText))
		sess.PromptHash = hex.EncodeToString(sum[:])
		if _, err := tx.ExecContext(ctx,
			`INSERT OR IGNORE INTO system_prompts (hash, text) VALUES (?, ?)`, sess.PromptHash, promptText); err != nil {
			return false, err
		}
	}
	if sess.Scope == "" {
		sess.Scope = "common"
	}
	if err := insertSession(ctx, tx, sess); err != nil {
		return false, err
	}
	if _, err := insertMessages(ctx, tx, sess.ID, 0, msgs); err != nil {
		return false, err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE sessions SET input_tokens = ?, output_tokens = ?, cached_tokens = ?, reasoning_tokens = ?, api_calls = ?, context_tokens = ? WHERE id = ?`,
		sess.Usage.Input, sess.Usage.Output, sess.Usage.Cached, sess.Usage.Reasoning, sess.Usage.Calls, sess.ContextTokens, sess.ID); err != nil {
		return false, err
	}
	if o.MakeCurrent && sess.Chat != "" {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO chats (chat, session_id) VALUES (?, ?)
			 ON CONFLICT(chat) DO NOTHING`, sess.Chat, sess.ID); err != nil {
			return false, err
		}
	}
	if o.Foreign != nil {
		if err := recordForeign(ctx, tx, *o.Foreign, sess.ID, true); err != nil {
			return false, err
		}
	}
	return false, tx.Commit()
}

// recordForeign notes that a foreign session is the stored session id. With
// replace it overwrites an earlier record (a forced import); without, it
// keeps one.
func recordForeign(ctx context.Context, tx *sql.Tx, f Foreign, id string, replace bool) error {
	verb := "INSERT OR IGNORE"
	if replace {
		verb = "INSERT OR REPLACE"
	}
	_, err := tx.ExecContext(ctx, verb+` INTO foreign_imports (source, foreign_id, session_id) VALUES (?, ?, ?)`, f.Source, f.ID, id)
	return err
}

// ForeignImported says whether a foreign session was already imported.
func (s *Store) ForeignImported(ctx context.Context, source, foreignID string) (bool, error) {
	var id string
	err := s.db.QueryRowContext(ctx, `SELECT session_id FROM foreign_imports WHERE source = ? AND foreign_id = ?`, source, foreignID).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}
