package server

import (
	"path/filepath"
	"strings"

	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// WebConfig is the [web] table and the SearXNG and extract-service settings,
// with the defaults for the rest.
// WebConfig builds the web tools' settings for the instance at home.
func WebConfig(cfg *config.Config, home string) *tools.Web {
	w := cfg.Web
	orDefault := func(v, def string) string {
		if v = strings.TrimSpace(v); v != "" {
			return v
		}
		return def
	}
	out := &tools.Web{
		SearXNG:        strings.TrimSpace(cfg.Getenv("SEARXNG_URL")),
		ExtractService: strings.TrimRight(strings.TrimSpace(cfg.Getenv("WEB_EXTRACT_URL")), "/"),
		Wayback:        orDefault(w.WaybackURL, "https://archive.org/wayback/available"),
		Jina:           orDefault(w.JinaURL, "https://r.jina.ai/"),
		Fallbacks:      w.ExtractFallbacks,
		AllowPrivate:   w.AllowPrivateURLs,
		CacheDir:       filepath.Join(home, "cache", "web_extract"),
	}
	if strings.ToLower(strings.TrimSpace(w.SearchFallback)) != "none" {
		out.DuckDuckGo = orDefault(w.DuckDuckGoURL, "https://html.duckduckgo.com/html/")
	}
	return out
}

// VisionConfig is where vision_analyze sends images: [auxiliary.vision], the
// session's own endpoint and model for what it leaves unset.
func VisionConfig(cfg *config.Config, ep config.Endpoint) *tools.Vision {
	aux := cfg.Auxiliary("vision", ep)
	return &tools.Vision{BaseURL: aux.BaseURL, APIKey: aux.APIKey, Model: aux.Model, Timeout: cfg.AuxiliaryTimeout("vision")}
}
