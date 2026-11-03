// Package config loads the instance's config.toml and .env and resolves
// model endpoints from them.
package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

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
	Models struct {
		// Renamed maps a model name that was renamed to its new name; a pin on
		// the old one keeps working (chains are not followed).
		Renamed map[string]string `toml:"renamed"`
	} `toml:"models"`
	Agent struct {
		ReasoningEffort    any            `toml:"reasoning_effort"`
		ReasoningOverrides map[string]any `toml:"reasoning_overrides"`
	} `toml:"agent"`
	Memory struct {
		RulesCharLimit int  `toml:"rules_char_limit"`
		FactCharLimit  int  `toml:"fact_char_limit"`
		WriteApproval  bool `toml:"write_approval"`
		RAG            RAG  `toml:"rag"`
	} `toml:"memory"`
	Approvals struct {
		Mode string `toml:"mode"`
	} `toml:"approvals"`
	Security struct {
		Lockdown bool `toml:"lockdown"`
		// Path prefixes the file tools may write although they are system
		// configuration (e.g. "/etc/nixos/" on a NixOS host).
		SensitivePathExceptions []string `toml:"sensitive_path_exceptions"`
	} `toml:"security"`
	Git struct {
		AuthorName  string `toml:"author_name"`
		AuthorEmail string `toml:"author_email"`
	} `toml:"git"`
	Compaction struct {
		Enabled   *bool   `toml:"enabled"`
		Threshold float64 `toml:"threshold"`
	} `toml:"compaction"`
	Daemon struct {
		Signal   bool   `toml:"signal"`    // serve this instance's Signal chats
		Instance string `toml:"instance"`  // the instance's name, for fiona's identity
		Cron     bool   `toml:"cron"`      // run the instance's scheduled jobs
		CronTick int    `toml:"cron_tick"` // seconds between ticks (default 60; for tests)
		// Autostart false: a system unit serves this home, so a client never
		// starts a daemon of its own here (Nix sets it for backend = "soa-d").
		Autostart *bool `toml:"autostart"`
		// ImportPython: serve imports the home's Python state.db at start and
		// retires it (Nix sets it for backend = "soa-d").
		ImportPython bool `toml:"import_python"`
	} `toml:"daemon"`
	Cron struct {
		Model string `toml:"model"` // the model of jobs that pin none
	} `toml:"cron"`
	Relay struct {
		WorkerHours any `toml:"worker_hours"` // [start, end] hours; see WorkerHours
	} `toml:"relay"`
	Gateway struct {
		Model                string `toml:"model"`
		SingleUser           bool   `toml:"single_user"`
		GroupSessionsPerUser *bool  `toml:"group_sessions_per_user"`
	} `toml:"gateway"`
	Terminal struct {
		Cwd string `toml:"cwd"`
		// HomeMode "cwd": commands get HOME = Cwd, so ~ is the instance's
		// working directory (a service account's home is its state dir).
		HomeMode string `toml:"home_mode"`
	} `toml:"terminal"`
	Display struct {
		Skin string `toml:"skin"` // the TUI's skin; empty is "default"
	} `toml:"display"`
	Web struct {
		SearchFallback   string   `toml:"search_fallback"` // "duckduckgo" (default) or "none"
		DuckDuckGoURL    string   `toml:"duckduckgo_url"`
		ExtractFallbacks []string `toml:"extract_fallbacks"` // opt-in: "wayback", "jina"
		WaybackURL       string   `toml:"wayback_url"`
		JinaURL          string   `toml:"jina_url"`
		AllowPrivateURLs bool     `toml:"allow_private_urls"`
	} `toml:"web"`
	MCPServers map[string]MCPServer `toml:"mcp_servers"`
	LSP        struct {
		Enabled     *bool                `toml:"enabled"`
		WaitTimeout any                  `toml:"wait_timeout"` // seconds
		IdleTimeout any                  `toml:"idle_timeout"` // seconds; 0 never
		Servers     map[string]LSPServer `toml:"servers"`
	} `toml:"lsp"`
	Platforms struct {
		Signal struct {
			RequireMention  any `toml:"require_mention"`
			TypingIndicator any `toml:"typing_indicator"`
		} `toml:"signal"`
	} `toml:"platforms"`
	AuxTasks        map[string]AuxiliaryTask  `toml:"auxiliary"`
	CustomProviders map[string]CustomProvider `toml:"custom_providers"`

	dotenv map[string]string
}

// CommandHome is the HOME commands run with: terminal.cwd under
// terminal.home_mode = "cwd", else "" (the daemon's own).
func (c *Config) CommandHome() string {
	if strings.EqualFold(strings.TrimSpace(c.Terminal.HomeMode), "cwd") {
		return c.Terminal.Cwd
	}
	return ""
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
	ContextLength    int      `toml:"context_length"`
	// Description is what the model is good for, in a few words; the models
	// note a chat gets lists it.
	Description string `toml:"description"`
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
	if to, ok := c.Models.Renamed[model]; ok && to != "" {
		model = to
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
		ep.APIKey = c.envKey(prov.KeyEnv)
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

// AuxiliaryTask is one [auxiliary.<task>] table. provider is accepted and
// ignored.
type AuxiliaryTask struct {
	BaseURL string `toml:"base_url"`
	KeyEnv  string `toml:"key_env"`
	Model   string `toml:"model"`
	// Timeout is the task's request timeout in seconds (0: the task's own default).
	Timeout float64 `toml:"timeout"`
}

// CompactionEnabled says whether automatic compaction is on (default true).
func (c *Config) CompactionEnabled() bool {
	return c.Compaction.Enabled == nil || *c.Compaction.Enabled
}

// CompactionThreshold is the fraction of the context window at which
// automatic compaction starts (default 0.75).
func (c *Config) CompactionThreshold() float64 {
	if t := c.Compaction.Threshold; t > 0 && t <= 1 {
		return t
	}
	return 0.75
}

// RouteDescription is the configured description of a model under the model
// provider, "" when unset.
func (c *Config) RouteDescription(model string) string {
	return strings.TrimSpace(c.CustomProviders[c.Model.Provider].Models[model].Description)
}

// RouteContextLength is the configured context window of a model under the
// model provider, 0 when unset.
func (c *Config) RouteContextLength(model string) int {
	return c.CustomProviders[c.Model.Provider].Models[model].ContextLength
}

// Auxiliary resolves the endpoint of an auxiliary task (compaction,
// title_generation): each field the task leaves unset comes from fallback.
// The key is the task's key_env variable from the process environment, then
// .env.
func (c *Config) Auxiliary(task string, fallback Endpoint) Endpoint {
	t, ok := c.AuxTasks[task]
	if !ok {
		return fallback
	}
	out := fallback
	if t.BaseURL != "" {
		out.BaseURL = t.BaseURL
	}
	if t.Model != "" {
		out.Model = t.Model
	}
	if t.KeyEnv != "" {
		out.APIKey = c.envKey(t.KeyEnv)
	}
	return out
}

// envKey is the value of an environment variable: the process environment
// when set and non-empty, else .env.
func (c *Config) envKey(name string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return c.dotenv[name]
}

// AuxiliaryTimeout is an auxiliary task's configured request timeout; 0 when
// unset.
func (c *Config) AuxiliaryTimeout(task string) time.Duration {
	return time.Duration(c.AuxTasks[task].Timeout * float64(time.Second))
}

// HasAuxiliary says whether an [auxiliary.<task>] table is configured.
func (c *Config) HasAuxiliary(task string) bool {
	_, ok := c.AuxTasks[task]
	return ok
}

// RAG is the [memory.rag] table. Pointer fields are the ones whose explicit
// value (an empty prompt, a zero score) must win over a model's default.
type RAG struct {
	Enabled                bool     `toml:"enabled"`
	BaseURL                string   `toml:"base_url"`
	APIKey                 string   `toml:"api_key"`
	APIKeyEnv              string   `toml:"api_key_env"`
	Model                  string   `toml:"model"`
	TopK                   *int     `toml:"top_k"`
	MinScore               *float64 `toml:"min_score"`
	DocumentPrompt         *string  `toml:"document_prompt"`
	QueryPrompt            *string  `toml:"query_prompt"`
	SearchTimeoutSeconds   *float64 `toml:"search_timeout_seconds"`
	FailureCooldownSeconds *float64 `toml:"failure_cooldown_seconds"`
	NotesDir               string   `toml:"notes_dir"`
	MaxChunkChars          int      `toml:"max_chunk_chars"`
}

// Getenv is the value of an environment variable: the process environment
// when set and non-empty, else .env.
func (c *Config) Getenv(name string) string { return c.envKey(name) }

// ChatModel is the model for chat sessions: gateway.model when set, else the
// default model ("").
func (c *Config) ChatModel() string { return strings.TrimSpace(c.Gateway.Model) }

// WorkerHours is relay.worker_hours: the hour workers may start and the hour
// they stop (0-23, wrapping midnight). ok is false when workers may run at
// any time: the key is absent or empty, the hours are equal, or the value is
// malformed (warn then says so).
func (c *Config) WorkerHours() (start, end int, ok bool, warn string) {
	v := c.Relay.WorkerHours
	if v == nil {
		return 0, 0, false, ""
	}
	list, isList := v.([]any)
	if isList && len(list) == 0 {
		return 0, 0, false, ""
	}
	var hours [2]int
	valid := isList && len(list) == 2
	for i := 0; valid && i < 2; i++ {
		n, isInt := list[i].(int64)
		if !isInt || n < 0 || n > 23 {
			valid = false
			break
		}
		hours[i] = int(n)
	}
	if !valid {
		return 0, 0, false, fmt.Sprintf("relay.worker_hours = %v is not two hours (0-23); workers may run at any time", v)
	}
	if hours[0] == hours[1] {
		return 0, 0, false, ""
	}
	return hours[0], hours[1], true, ""
}

// SignalSetting reads a Signal setting that config.toml may spell as a
// boolean or as a string ("true", "yes", ...); set is false when absent.
func SignalSetting(v any) (value, set bool) {
	switch x := v.(type) {
	case bool:
		return x, true
	case string:
		switch strings.ToLower(strings.TrimSpace(x)) {
		case "true", "1", "yes", "on":
			return true, true
		case "false", "0", "no", "off":
			return false, true
		}
	}
	return false, false
}

// MCPServer is one [mcp_servers.<name>] table. Timeouts are in seconds.
// Values in env and headers may be any TOML scalar; they are used as text.
type MCPServer struct {
	Command        string         `toml:"command"`
	Args           []string       `toml:"args"`
	Env            map[string]any `toml:"env"`
	Cwd            string         `toml:"cwd"`
	URL            string         `toml:"url"`
	Headers        map[string]any `toml:"headers"`
	Enabled        *bool          `toml:"enabled"`
	Timeout        any            `toml:"timeout"`
	ConnectTimeout any            `toml:"connect_timeout"`
	Tools          struct {
		Include []string `toml:"include"`
		Exclude []string `toml:"exclude"`
	} `toml:"tools"`
}

// LSPServer is one [lsp.servers.<id>] table. Extensions and RootMarkers
// define a server the built-in table lacks.
type LSPServer struct {
	Disabled              bool           `toml:"disabled"`
	Command               []string       `toml:"command"`
	Env                   map[string]any `toml:"env"`
	InitializationOptions any            `toml:"initialization_options"`
	Extensions            []string       `toml:"extensions"`
	RootMarkers           []string       `toml:"root_markers"`
}
