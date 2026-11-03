package store_test

// Phase 8g: schema v13 drops the tables of the reverted active hours.

import (
	"database/sql"
	"path/filepath"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func TestTheActiveHoursTablesAreGone(t *testing.T) {
	path := filepath.Join(t.TempDir(), "soa-d.db")
	st, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if v, err := st.SchemaVersion(); err != nil || v < 13 {
		t.Fatalf("schema version %d, %v", v, err)
	}
	st.Close()
	db, err := sql.Open("sqlite", "file:"+path+"?mode=ro")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var n int
	if err := db.QueryRow(`SELECT count(*) FROM sqlite_master WHERE name IN ('held_messages', 'held_asked')`).Scan(&n); err != nil || n != 0 {
		t.Fatalf("%d held tables remain (%v)", n, err)
	}
}
