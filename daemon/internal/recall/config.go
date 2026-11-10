// Package recall is per-turn recall of facts and notes: an embedding index
// over memory/facts*.toml and notes/, a keyword fallback when the embedder is
// down, and the fenced block that rides a user message.
package recall

import (
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/config"
)

// Config is the resolved [memory.rag] section.
type Config struct {
	BaseURL, APIKey, Model         string
	TopK                           int
	MinScore                       float64
	DocumentPrompt, QueryPrompt    string
	SearchTimeout, FailureCooldown time.Duration
	NotesDir                       string // "" means <home>/notes
	MaxChunkChars                  int

	// RerankModel, when set, reorders the cosine candidates (at most
	// RerankCandidates of those at MinScore or above) by <BaseURL>/rerank and
	// keeps those scoring RerankMinScore or above.
	RerankModel      string
	RerankCandidates int
	RerankMinScore   float64
	RerankTimeout    time.Duration
}

func isGemma(model string) bool { return strings.Contains(strings.ToLower(model), "embeddinggemma") }

// ConfigFrom resolves [memory.rag]; ok says recall is enabled with a base
// URL.
func ConfigFrom(c *config.Config, home string) (Config, bool) {
	r := c.Memory.RAG
	base := strings.TrimRight(strings.TrimSpace(r.BaseURL), "/")
	if !r.Enabled || base == "" {
		return Config{}, false
	}
	model := strings.TrimSpace(r.Model)
	if model == "" {
		model = "bge-m3"
	}
	cfg := Config{BaseURL: base, Model: model, TopK: 5, MinScore: 0.5,
		SearchTimeout: 3 * time.Second, FailureCooldown: time.Minute,
		NotesDir: strings.TrimSpace(r.NotesDir), MaxChunkChars: r.MaxChunkChars,
		RerankModel: strings.TrimSpace(r.RerankModel), RerankCandidates: 10, RerankMinScore: 0.5, RerankTimeout: 3 * time.Second}
	if cfg.APIKey = strings.TrimSpace(r.APIKey); cfg.APIKey == "" && strings.TrimSpace(r.APIKeyEnv) != "" {
		cfg.APIKey = c.Getenv(strings.TrimSpace(r.APIKeyEnv))
	}
	if isGemma(model) {
		cfg.MinScore = 0.65
		cfg.DocumentPrompt = "title: none | text: "
		cfg.QueryPrompt = "task: search result | query: "
	}
	if r.TopK != nil {
		cfg.TopK = *r.TopK
	}
	if r.MinScore != nil {
		cfg.MinScore = *r.MinScore
	}
	if r.DocumentPrompt != nil {
		cfg.DocumentPrompt = *r.DocumentPrompt
	}
	if r.QueryPrompt != nil {
		cfg.QueryPrompt = *r.QueryPrompt
	}
	if r.SearchTimeoutSeconds != nil {
		cfg.SearchTimeout = secs(*r.SearchTimeoutSeconds)
	}
	if r.FailureCooldownSeconds != nil {
		cfg.FailureCooldown = secs(*r.FailureCooldownSeconds)
	}
	if r.RerankCandidates != nil {
		cfg.RerankCandidates = *r.RerankCandidates
	}
	if r.RerankMinScore != nil {
		cfg.RerankMinScore = *r.RerankMinScore
	}
	if r.RerankTimeoutSeconds != nil {
		cfg.RerankTimeout = secs(*r.RerankTimeoutSeconds)
	}
	if cfg.NotesDir != "" && strings.HasPrefix(cfg.NotesDir, "~/") {
		cfg.NotesDir = filepath.Join(userHome(), cfg.NotesDir[2:])
	}
	return cfg, true
}

func secs(f float64) time.Duration {
	if f < 0 {
		f = 0
	}
	return time.Duration(f * float64(time.Second))
}

func userHome() string {
	h, _ := os.UserHomeDir()
	return h
}
