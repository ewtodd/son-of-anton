package server

import (
	"fmt"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/mcp"
)

const (
	defaultMCPTimeout        = 300 * time.Second
	defaultMCPConnectTimeout = 60 * time.Second
)

// MCPManager starts the enabled [mcp_servers] and returns the manager that
// reaches them, or nil when none is enabled. The caller closes it.
func MCPManager(cfg *config.Config, home string) *mcp.Manager {
	var servers []mcp.Server
	for name, c := range cfg.MCPServers {
		if c.Enabled != nil && !*c.Enabled {
			continue
		}
		servers = append(servers, mcp.Server{
			Name: name, Command: c.Command, Args: c.Args, Env: stringMap(c.Env), Cwd: c.Cwd,
			URL: c.URL, Headers: stringMap(c.Headers),
			Timeout:        seconds(c.Timeout, defaultMCPTimeout),
			ConnectTimeout: seconds(c.ConnectTimeout, defaultMCPConnectTimeout),
			Include:        c.Tools.Include, Exclude: c.Tools.Exclude,
		})
	}
	if len(servers) == 0 {
		return nil
	}
	return mcp.New(home, servers, cfg.Getenv)
}

func stringMap(in map[string]any) map[string]string {
	out := make(map[string]string, len(in))
	for k, v := range in {
		out[k] = fmt.Sprint(v)
	}
	return out
}

// seconds reads a TOML number of seconds; anything else, or a value that is
// not positive, is the default.
func seconds(v any, def time.Duration) time.Duration {
	var n float64
	switch x := v.(type) {
	case int64:
		n = float64(x)
	case float64:
		n = x
	}
	if n <= 0 {
		return def
	}
	return time.Duration(n * float64(time.Second))
}
