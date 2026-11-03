package config_test

// Phase 8g: [models] renamed resolves an old model name to its new one.

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/config"
)

func TestARenamedModelResolvesToItsNewName(t *testing.T) {
	home := t.TempDir()
	cfg := "[model]\ndefault = \"auto\"\nprovider = \"custom\"\n\n[custom_providers.custom]\nbase_url = \"http://127.0.0.1:9/v1\"\n\n" +
		"[custom_providers.custom.models.\"unholy/glm5.3-flash\"]\nreasoning_efforts = [\"low\", \"high\", \"max\"]\n\n" +
		"[models.renamed]\n\"unholy/glm53\" = \"unholy/glm5.3-flash\"\n"
	if err := os.WriteFile(filepath.Join(home, "config.toml"), []byte(cfg), 0o600); err != nil {
		t.Fatal(err)
	}
	c, err := config.Load(home)
	if err != nil {
		t.Fatal(err)
	}
	ep, err := c.Endpoint("unholy/glm53")
	if err != nil || ep.Model != "unholy/glm5.3-flash" {
		t.Fatalf("Endpoint(unholy/glm53) = %+v, %v", ep, err)
	}
	if ep, _ := c.Endpoint("auto"); ep.Model != "auto" {
		t.Fatalf("an unrenamed model changed: %+v", ep)
	}
}
