package server

import (
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/lsp"
)

// LSPService builds the language-server service from [lsp], or returns nil
// when it is disabled. The caller closes it.
func LSPService(cfg *config.Config) *lsp.Service {
	c := lsp.Config{
		Disabled:    cfg.LSP.Enabled != nil && !*cfg.LSP.Enabled,
		WaitTimeout: seconds(cfg.LSP.WaitTimeout, 0),
		Servers:     map[string]lsp.ServerConfig{},
	}
	// idle_timeout = 0 means never; unset or negative is the default.
	switch x := cfg.LSP.IdleTimeout.(type) {
	case int64:
		c.IdleTimeout = idle(float64(x))
	case float64:
		c.IdleTimeout = idle(x)
	}
	for id, s := range cfg.LSP.Servers {
		c.Servers[id] = lsp.ServerConfig{Disabled: s.Disabled, Command: s.Command, Env: stringMap(s.Env),
			InitOptions: s.InitializationOptions, Extensions: s.Extensions, RootMarkers: s.RootMarkers}
	}
	return lsp.New(c)
}

// idle converts idle_timeout seconds: 0 is never (negative to lsp.Config),
// anything below is the default (zero).
func idle(n float64) time.Duration {
	switch {
	case n == 0:
		return -1
	case n < 0:
		return 0
	}
	return time.Duration(n * float64(time.Second))
}
