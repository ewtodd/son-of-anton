// Package config loads the instance's config.toml and .env and resolves
// model endpoints from them.
package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/BurntSushi/toml"

	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
)

// Config is the subset of <home>/config.toml the daemon reads. Unknown keys
// and sections are ignored.
type Config struct {
	Model struct {
		Default         string `toml:"default"`
		Provider        string `toml:"provider"`
		ReasoningEffort any    `toml:"reasoning_effort"`
	} `toml:"model"`
	Agent struct {
		ReasoningEffort    any            `toml:"reasoning_effort"`
		ReasoningOverrides map[string]any `toml:"reasoning_overrides"`
	} `toml:"agent"`
	Memory struct {
		RulesCharLimit int `toml:"rules_char_limit"`
	} `toml:"memory"`
	Approvals struct {
		Mode string `toml:"mode"`
	} `toml:"approvals"`
	Security struct {
		Lockdown bool `toml:"lockdown"`
	} `toml:"security"`
	Git struct {
		AuthorName  string `toml:"author_name"`
		AuthorEmail string `toml:"author_email"`
	} `toml:"git"`
	CustomProviders map[string]CustomProvider `toml:"custom_providers"`

	dotenv map[string]string
}

// PermissionMode resolves the [approvals] mode (and the [security] lockdown
// override) into an approval.Mode. An unknown or empty mode is "default".
func (c Config) PermissionMode() approval.Mode {
	if c.Security.Lockdown {
		return approval.Lockdown
	}
	switch c.Approvals.Mode {
	case "manual":
		return approval.Ask
	case "off":
		return approval.Yolo
	default:
		return approval.Default
	}
}

// CustomProvider is one [custom_providers.<name>] table.
type CustomProvider struct {
	BaseURL string                `toml:"base_url"`
	KeyEnv  string                `toml:"key_env"`
	Models  map[string]ModelRoute `toml:"models"`
}

// ModelRoute is one models.<model> table inside a provider.
type ModelRoute struct {
	ReasoningEffort  any      `toml:"reasoning_effort"`
	ReasoningEfforts []string `toml:"reasoning_efforts"`
}

// Endpoint is a resolved, ready-to-use model endpoint.
type Endpoint struct {
	BaseURL         string
	APIKey          string
	Model           string
	ReasoningEffort string
}

// Home returns the instance home: $SON_OF_ANTON_HOME when set and non-empty,
// else $HOME/.son-of-anton.
func Home() (string, error) {
	if h := os.Getenv("SON_OF_ANTON_HOME"); h != "" {
		return h, nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("config: cannot resolve home: %w", err)
	}
	return filepath.Join(home, ".son-of-anton"), nil
}

// Load reads <home>/config.toml (missing = empty config) and <home>/.env.
func Load(home string) (*Config, error) {
	cfg := &Config{dotenv: map[string]string{}}

	tomlPath := filepath.Join(home, "config.toml")
	data, err := os.ReadFile(tomlPath)
	if err != nil {
		if !os.IsNotExist(err) {
			return nil, fmt.Errorf("config: reading %s: %w", tomlPath, err)
		}
	} else if err := toml.Unmarshal(data, cfg); err != nil {
		return nil, fmt.Errorf("config: parsing %s: %w", tomlPath, err)
	}

	envPath := filepath.Join(home, ".env")
	envData, err := os.ReadFile(envPath)
	if err != nil {
		if !os.IsNotExist(err) {
			return nil, fmt.Errorf("config: reading %s: %w", envPath, err)
		}
	} else {
		cfg.dotenv = parseDotEnv(string(envData))
	}
	return cfg, nil
}

// Endpoint resolves the endpoint for model ("" = the configured default
// model). The API key is the process env var named by key_env when set and
// non-empty, else the .env value. The reasoning effort is resolved by
// precedence (agent override, route, model section, agent global) and clamped
// to the route's reasoning_efforts vocabulary.
func (c *Config) Endpoint(model string) (Endpoint, error) {
	if model == "" {
		model = c.Model.Default
	}
	providerName := c.Model.Provider
	prov, ok := c.CustomProviders[providerName]
	if !ok {
		return Endpoint{}, fmt.Errorf(
			"model provider %q is not configured: no [custom_providers.%s] in config.toml",
			providerName, providerName)
	}
	ep := Endpoint{BaseURL: prov.BaseURL, Model: model}
	if prov.KeyEnv != "" {
		if v := os.Getenv(prov.KeyEnv); v != "" {
			ep.APIKey = v
		} else if v, ok := c.dotenv[prov.KeyEnv]; ok {
			ep.APIKey = v
		}
	}
	ep.ReasoningEffort = c.resolveEffort(model, &prov)
	if route, ok := prov.Models[model]; ok {
		ep.ReasoningEffort = ClampEffort(ep.ReasoningEffort, route.ReasoningEfforts)
	}
	return ep, nil
}

// resolveEffort picks the first source that declares a level:
// agent.reasoning_overrides[model], the route's reasoning_effort,
// [model].reasoning_effort (only for the default model), agent.reasoning_effort.
func (c *Config) resolveEffort(model string, prov *CustomProvider) string {
	if v, ok := c.Agent.ReasoningOverrides[model]; ok {
		return normalizeEffort(v)
	}
	if route, ok := prov.Models[model]; ok && route.ReasoningEffort != nil {
		return normalizeEffort(route.ReasoningEffort)
	}
	if model == c.Model.Default && c.Model.ReasoningEffort != nil {
		return normalizeEffort(c.Model.ReasoningEffort)
	}
	if c.Agent.ReasoningEffort != nil {
		return normalizeEffort(c.Agent.ReasoningEffort)
	}
	return ""
}

// normalizeEffort maps a config value onto a wire level. A TOML false, or
// "off", "no", "false" (case-insensitive), means "none".
func normalizeEffort(v any) string {
	switch x := v.(type) {
	case nil:
		return ""
	case bool:
		if x {
			return "true"
		}
		return "none"
	case string:
		s := strings.ToLower(strings.TrimSpace(x))
		switch s {
		case "off", "no", "false":
			return "none"
		default:
			return s
		}
	default:
		return fmt.Sprintf("%v", x)
	}
}

// parseDotEnv parses KEY=VALUE lines, skipping blanks and # comments, an
// optional "export " prefix, and one pair of surrounding quotes.
func parseDotEnv(text string) map[string]string {
	out := map[string]string{}
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimPrefix(line, "export ")
		eq := strings.IndexByte(line, '=')
		if eq <= 0 {
			continue
		}
		key := strings.TrimSpace(line[:eq])
		val := strings.TrimSpace(line[eq+1:])
		if len(val) >= 2 {
			if (val[0] == '"' && val[len(val)-1] == '"') || (val[0] == '\'' && val[len(val)-1] == '\'') {
				val = val[1 : len(val)-1]
			}
		}
		out[key] = val
	}
	return out
}

// Routes lists the model routes configured under the model provider, with
// their reasoning_efforts (nil without a vocabulary).
func (c *Config) Routes() map[string][]string {
	out := map[string][]string{}
	for name, route := range c.CustomProviders[c.Model.Provider].Models {
		out[name] = route.ReasoningEfforts
	}
	return out
}
