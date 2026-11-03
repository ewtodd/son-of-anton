package store

import "context"

// Held is a chat message saved while the instance was off duty.
type Held struct {
	Sender, Text string
	TS           int64
}

// HoldMessage saves a message for the chat (phase 5e), after the ones it
// already holds. The chat's open question, if any, is stale: it is dropped
// so the next opening asks again with this message in the summary.
func (s *Store) HoldMessage(ctx context.Context, chat string, m Held) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO held_messages (chat, seq, sender, text, ts)
		 VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM held_messages WHERE chat = ?), ?, ?, ?)`,
		chat, chat, m.Sender, m.Text, m.TS); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM held_asked WHERE chat = ?`, chat); err != nil {
		return err
	}
	return tx.Commit()
}

// HeldMessages returns the chat's held messages, oldest first.
func (s *Store) HeldMessages(ctx context.Context, chat string) ([]Held, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT sender, text, ts FROM held_messages WHERE chat = ? ORDER BY seq`, chat)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Held
	for rows.Next() {
		var m Held
		if err := rows.Scan(&m.Sender, &m.Text, &m.TS); err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

// ClearHeld discards the chat's held messages and its open question.
func (s *Store) ClearHeld(ctx context.Context, chat string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	if _, err := tx.ExecContext(ctx, `DELETE FROM held_messages WHERE chat = ?`, chat); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM held_asked WHERE chat = ?`, chat); err != nil {
		return err
	}
	return tx.Commit()
}

// HeldChats lists the chats that hold messages.
func (s *Store) HeldChats(ctx context.Context) ([]string, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT DISTINCT chat FROM held_messages ORDER BY chat`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var c string
		if err := rows.Scan(&c); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

// HeldAsked says whether the chat has been asked about its held messages.
func (s *Store) HeldAsked(ctx context.Context, chat string) (bool, error) {
	var n int
	err := s.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM held_asked WHERE chat = ?`, chat).Scan(&n)
	return n > 0, err
}

// SetHeldAsked marks the chat as asked, or not.
func (s *Store) SetHeldAsked(ctx context.Context, chat string, asked bool) error {
	q := `DELETE FROM held_asked WHERE chat = ?`
	if asked {
		q = `INSERT OR IGNORE INTO held_asked (chat) VALUES (?)`
	}
	_, err := s.db.ExecContext(ctx, q, chat)
	return err
}
