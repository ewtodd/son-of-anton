package recall

import (
	"context"
	"log"

	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/memory"
)

// Memory builds the daemon's memory store with the configured limits. When
// recall is on it also opens the index and lets the store ask it for
// near-duplicate facts (only with withIndex); idx is nil otherwise (or when the index cannot be
// opened, which is logged: memory still works, by keyword).
func Memory(cfg *config.Config, home string, withIndex bool) (store *memory.Store, idx *Index) {
	store = memory.New(home)
	if n := cfg.Memory.RulesCharLimit; n > 0 {
		store.RulesCharLimit = n
	}
	if n := cfg.Memory.FactCharLimit; n > 0 {
		store.FactCharLimit = n
	}
	rc, ok := ConfigFrom(cfg, home)
	if !ok || !withIndex {
		return store, nil
	}
	idx, err := Open(home, rc)
	if err != nil {
		log.Printf("recall: index not opened: %v", err)
		return store, nil
	}
	store.Similar = func(scope, text string) []memory.Fact {
		return idx.Similar(context.Background(), scope, text)
	}
	return store, idx
}
