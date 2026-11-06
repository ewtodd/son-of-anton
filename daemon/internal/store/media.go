// Images: the media table (content-addressed bytes) and the helpers the
// transcript and the queue use to reference and hydrate them.
package store

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
)

// PutMedia stores an image's bytes under their hash and returns the
// reference. Storing the same bytes again keeps the one row.
func (s *Store) PutMedia(ctx context.Context, mime, name string, data []byte) (Image, error) {
	sum := sha256.Sum256(data)
	img := Image{ID: hex.EncodeToString(sum[:]), Mime: mime, Name: name}
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO media (id, mime, name, data) VALUES (?, ?, ?, ?)
		 ON CONFLICT(id) DO NOTHING`, img.ID, mime, nullStr(name), data)
	return img, err
}

// Media returns the bytes of an image reference; ErrNotFound when the id is
// unknown (the row was pruned).
func (s *Store) Media(ctx context.Context, id string) ([]byte, error) {
	var data []byte
	err := s.db.QueryRowContext(ctx, `SELECT data FROM media WHERE id = ?`, id).Scan(&data)
	if err == sql.ErrNoRows {
		return nil, ErrNotFound
	}
	return data, err
}

// PruneMedia deletes the media rows nothing references: no stored message's
// images column and no queue row names them. Loose rows can exist when a turn
// died between storing an image and appending its row, and after sessions are
// deleted.
func (s *Store) PruneMedia(ctx context.Context) error {
	_, err := s.db.ExecContext(ctx, `DELETE FROM media WHERE id NOT IN (
		SELECT json_extract(je.value, '$.id')
		  FROM messages m, json_each(COALESCE(m.images, json_array())) je
		 WHERE json_extract(je.value, '$.id') IS NOT NULL
		UNION
		SELECT json_extract(je.value, '$.id')
		  FROM queue q, json_each(COALESCE(q.images, json_array())) je
		 WHERE json_extract(je.value, '$.id') IS NOT NULL
	)`)
	return err
}
