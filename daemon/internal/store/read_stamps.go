package store

import (
	"context"
	"encoding/json"
)

// SeenRange is a half-open range of 0-based line indexes a session read.
type SeenRange struct {
	Start int
	End   int
}

// ReadStamp is one file's read state in a session, as Read recorded it: the
// size and hash of the bytes seen, the line count, the line ranges read and
// whether the whole file was seen (an image, an empty file, or the session
// wrote the file itself). Write and Edit refuse a file whose stamp is not on
// record, or whose bytes no longer match, so the stamp must survive the
// session being unloaded between a chat's turns.
type ReadStamp struct {
	Path  string
	Size  int64
	Sum   []byte
	Lines int
	Seen  []SeenRange
	Whole bool
}

// ReadStamps returns the session's read stamps, keyed by absolute path.
func (s *Store) ReadStamps(ctx context.Context, sessionID string) (map[string]ReadStamp, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT path, size, sum, lines, seen, whole FROM read_stamps WHERE session_id = ?`, sessionID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]ReadStamp{}
	for rows.Next() {
		var st ReadStamp
		var seen string
		var whole int
		if err := rows.Scan(&st.Path, &st.Size, &st.Sum, &st.Lines, &seen, &whole); err != nil {
			return nil, err
		}
		st.Whole = whole != 0
		if err := json.Unmarshal([]byte(seen), &st.Seen); err != nil {
			return nil, err
		}
		out[st.Path] = st
	}
	return out, rows.Err()
}

// PutReadStamps replaces the session's read stamps with stamps, all in one
// transaction: a Read or Edit between messages of a chat updates the record.
func (s *Store) PutReadStamps(ctx context.Context, sessionID string, stamps map[string]ReadStamp) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback() //nolint:errcheck
	if _, err := tx.ExecContext(ctx, `DELETE FROM read_stamps WHERE session_id = ?`, sessionID); err != nil {
		return err
	}
	for _, st := range stamps {
		seen, err := json.Marshal(st.Seen)
		if err != nil {
			return err
		}
		whole := 0
		if st.Whole {
			whole = 1
		}
		sum := st.Sum
		if sum == nil {
			sum = []byte{} // a BLOB NOT NULL column: an empty blob, not NULL
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO read_stamps (session_id, path, size, sum, lines, seen, whole) VALUES (?,?,?,?,?,?,?)`,
			sessionID, st.Path, st.Size, sum, st.Lines, string(seen), whole); err != nil {
			return err
		}
	}
	return tx.Commit()
}
