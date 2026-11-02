package config_test

// Phase 3a settings: compaction, a model's context length, and the
// auxiliary endpoints for summaries and titles.

import (
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/config"
)

func TestCompactionAndAuxiliarySettings(t *testing.T) {
	t.Setenv("MAIN_KEY", "")
	t.Setenv("TITLE_KEY", "")
	dir := home(t, `
[model]
default = "m"
provider = "p"

[custom_providers.p]
base_url = "http://main.test/v1"
key_env = "MAIN_KEY"

[custom_providers.p.models."m"]
context_length = 65536

[compaction]
enabled = false
threshold = 0.6

[auxiliary.title_generation]
base_url = "http://titles.test/v1"
key_env = "TITLE_KEY"
model = "llama/little-titles"
provider = "custom"
`, "MAIN_KEY=main-secret\nTITLE_KEY=title-secret\n")
	cfg, err := config.Load(dir)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.CompactionEnabled() || cfg.CompactionThreshold() != 0.6 {
		t.Fatalf("compaction = %v, %v", cfg.CompactionEnabled(), cfg.CompactionThreshold())
	}
	if cfg.RouteContextLength("m") != 65536 || cfg.RouteContextLength("other") != 0 {
		t.Fatalf("RouteContextLength: m %d, other %d", cfg.RouteContextLength("m"), cfg.RouteContextLength("other"))
	}
	main, err := cfg.Endpoint("m")
	if err != nil {
		t.Fatal(err)
	}
	titles := cfg.Auxiliary("title_generation", main)
	if titles.BaseURL != "http://titles.test/v1" || titles.APIKey != "title-secret" || titles.Model != "llama/little-titles" {
		t.Fatalf("title endpoint = %+v", titles)
	}
	if got := cfg.Auxiliary("compaction", main); got != main {
		t.Fatalf("an unset auxiliary task uses the session's endpoint: %+v vs %+v", got, main)
	}

	dflt, err := config.Load(home(t, "", ""))
	if err != nil {
		t.Fatal(err)
	}
	if !dflt.CompactionEnabled() || dflt.CompactionThreshold() != 0.75 {
		t.Fatalf("defaults: enabled %v, threshold %v; want on at 0.75", dflt.CompactionEnabled(), dflt.CompactionThreshold())
	}
}

func TestAnAuxiliaryTaskInheritsWhatItLeavesOut(t *testing.T) {
	t.Setenv("MAIN_KEY", "")
	dir := home(t, `
[model]
default = "m"
provider = "p"

[custom_providers.p]
base_url = "http://main.test/v1"
key_env = "MAIN_KEY"

[auxiliary.compaction]
model = "summarizer"
`, "MAIN_KEY=main-secret\n")
	cfg, err := config.Load(dir)
	if err != nil {
		t.Fatal(err)
	}
	main, _ := cfg.Endpoint("m")
	got := cfg.Auxiliary("compaction", main)
	if got.BaseURL != main.BaseURL || got.APIKey != "main-secret" || got.Model != "summarizer" {
		t.Fatalf("compaction endpoint = %+v; a task with only a model uses the session's base URL and key", got)
	}
}
