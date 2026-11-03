package store

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
)

// ImportSession writes a session that already has an ID (and Created), its
// prompt text and its rows in one transaction. A session whose ID is already
// stored is left alone and reported as present. With makeCurrent, the
// session becomes its chat's current one if that chat has none.
func (s *Store) ImportSession(ctx context.Context, sess Session, promptText string, msgs []Message, makeCurrent bool) (present bool, err error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()

	var one int
	switch err := tx.QueryRowContext(ctx, `SELECT 1 FROM sessions WHERE id = ?`, sess.ID).Scan(&one); {
	case err == nil:
		return true, nil
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
	if makeCurrent && sess.Chat != "" {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO chats (chat, session_id) VALUES (?, ?)
			 ON CONFLICT(chat) DO NOTHING`, sess.Chat, sess.ID); err != nil {
			return false, err
		}
	}
	return false, tx.Commit()
}
