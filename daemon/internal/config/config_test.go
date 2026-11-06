package config_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/config"
)

// home writes config.toml (and .env when given) into a fresh directory.
func home(t *testing.T, configTOML, dotenv string) string {
	t.Helper()
	dir := t.TempDir()
	if configTOML != "" {
		if err := os.WriteFile(filepath.Join(dir, "config.toml"), []byte(configTOML), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if dotenv != "" {
		if err := os.WriteFile(filepath.Join(dir, ".env"), []byte(dotenv), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// The owner's real config has this shape (custom provider through maitred,
// per-model effort vocabularies) plus many sections the daemon does not read.
const sample = `
[model]
default = "auto"
provider = "custom"
reasoning_effort = "high"
supports_vision = true

[custom_providers.custom]
base_url = "http://maitred.test/v1"
key_env = "SOA_TEST_KEY"
discover_models = true

[custom_providers.custom.models."radiance/qwen3.8-27b"]
reasoning_effort = "xhigh"
reasoning_efforts = ["none", "minimal", "low", "medium", "xhigh"]

[custom_providers.custom.models."eklow/gemma"]
reasoning_efforts = ["none", "high"]

[physics]
workspace_root = "/somewhere"

[gateway]
single_user = true
`

func load(t *testing.T, dir string) *config.Config {
	t.Helper()
	cfg, err := config.Load(dir)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	return cfg
}

func TestHomeComesFromTheEnvironment(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("SON_OF_ANTON_HOME", dir)
	got, err := config.Home()
	if err != nil || got != dir {
		t.Fatalf("Home() = %q, %v; want %q", got, err, dir)
	}
}

func TestHomeDefaultsUnderTheUserHome(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("SON_OF_ANTON_HOME", "")
	t.Setenv("HOME", dir)
	got, err := config.Home()
	if err != nil || got != filepath.Join(dir, ".son-of-anton") {
		t.Fatalf("Home() = %q, %v; want %q", got, err, filepath.Join(dir, ".son-of-anton"))
	}
}

func TestDefaultModelEndpoint(t *testing.T) {
	t.Setenv("SOA_TEST_KEY", "")
	cfg := load(t, home(t, sample, "SOA_TEST_KEY=from-dotenv\n"))
	ep, err := cfg.Endpoint("")
	if err != nil {
		t.Fatal(err)
	}
	if ep.BaseURL != "http://maitred.test/v1" || ep.Model != "auto" {
		t.Fatalf("endpoint = %+v", ep)
	}
	if ep.APIKey != "from-dotenv" {
		t.Fatalf("APIKey = %q, want the .env value", ep.APIKey)
	}
	// [model].reasoning_effort describes the default model.
	if ep.ReasoningEffort != "high" {
		t.Fatalf("ReasoningEffort = %q, want high", ep.ReasoningEffort)
	}
}

func TestProcessEnvironmentBeatsDotEnv(t *testing.T) {
	t.Setenv("SOA_TEST_KEY", "from-process")
	cfg := load(t, home(t, sample, "SOA_TEST_KEY=from-dotenv\n"))
	ep, err := cfg.Endpoint("")
	if err != nil || ep.APIKey != "from-process" {
		t.Fatalf("APIKey = %q, %v; want from-process", ep.APIKey, err)
	}
}

func TestDotEnvSyntax(t *testing.T) {
	t.Setenv("SOA_TEST_KEY", "")
	dotenv := "# comment\n\nOTHER=1\nexport SOA_TEST_KEY=\"quoted value\"\n"
	cfg := load(t, home(t, sample, dotenv))
	ep, _ := cfg.Endpoint("")
	if ep.APIKey != "quoted value" {
		t.Fatalf("APIKey = %q, want the unquoted .env value", ep.APIKey)
	}
}

func TestPerModelEffortBeatsTheModelSection(t *testing.T) {
	cfg := load(t, home(t, sample, ""))
	ep, err := cfg.Endpoint("radiance/qwen3.8-27b")
	if err != nil || ep.ReasoningEffort != "xhigh" {
		t.Fatalf("ReasoningEffort = %q, %v; want xhigh", ep.ReasoningEffort, err)
	}
	if ep.Model != "radiance/qwen3.8-27b" {
		t.Fatalf("Model = %q", ep.Model)
	}
}

func TestModelSectionEffortOnlyDescribesTheDefaultModel(t *testing.T) {
	cfg := load(t, home(t, sample, ""))
	ep, err := cfg.Endpoint("eklow/gemma")
	if err != nil {
		t.Fatal(err)
	}
	if ep.ReasoningEffort != "" {
		t.Fatalf("ReasoningEffort = %q; [model].reasoning_effort must not apply to another model", ep.ReasoningEffort)
	}
}

func TestAgentOverridesAndGlobalEffort(t *testing.T) {
	cfg := load(t, home(t, sample+`
[agent]
reasoning_effort = "medium"

[agent.reasoning_overrides]
"eklow/gemma" = "none"
`, ""))
	if ep, _ := cfg.Endpoint("eklow/gemma"); ep.ReasoningEffort != "none" {
		t.Fatalf("override: ReasoningEffort = %q, want none", ep.ReasoningEffort)
	}
	if ep, _ := cfg.Endpoint("some/other-model"); ep.ReasoningEffort != "medium" {
		t.Fatalf("global: ReasoningEffort = %q, want medium", ep.ReasoningEffort)
	}
	// The override outranks the provider route's own effort, too.
	cfg = load(t, home(t, sample+`
[agent.reasoning_overrides]
"radiance/qwen3.8-27b" = "low"
`, ""))
	if ep, _ := cfg.Endpoint("radiance/qwen3.8-27b"); ep.ReasoningEffort != "low" {
		t.Fatalf("override over route: ReasoningEffort = %q, want low", ep.ReasoningEffort)
	}
}

func TestFalseMeansReasoningOff(t *testing.T) {
	cfg := load(t, home(t, sample+`
[agent]
reasoning_effort = false
`, ""))
	if ep, _ := cfg.Endpoint("some/other-model"); ep.ReasoningEffort != "none" {
		t.Fatalf("ReasoningEffort = %q, want none for false", ep.ReasoningEffort)
	}
}

func TestEffortIsClampedToTheRouteVocabulary(t *testing.T) {
	cfg := load(t, home(t, strings.Replace(sample, `default = "auto"`, `default = "eklow/gemma"`, 1), ""))
	cfg2 := load(t, home(t, strings.Replace(
		strings.Replace(sample, `default = "auto"`, `default = "eklow/gemma"`, 1),
		`reasoning_effort = "high"`, `reasoning_effort = "xhigh"`, 1), ""))
	if ep, _ := cfg.Endpoint(""); ep.ReasoningEffort != "high" {
		t.Fatalf("supported level: got %q, want high", ep.ReasoningEffort)
	}
	if ep, _ := cfg2.Endpoint(""); ep.ReasoningEffort != "high" {
		t.Fatalf("xhigh on a none/high route: got %q, want high", ep.ReasoningEffort)
	}
}

func TestClampEffort(t *testing.T) {
	cases := []struct {
		requested string
		supported []string
		want      string
	}{
		{"high", []string{"none", "minimal", "low", "medium", "xhigh"}, "medium"},
		{"max", []string{"low", "high", "max"}, "max"},
		{"xhigh", []string{"low", "high", "max"}, "high"},
		{"low", []string{"high", "max"}, "high"},      // nothing weaker: the weakest supported
		{"minimal", []string{"none", "high"}, "high"}, // none is never a degradation target
		{"none", []string{"none", "high"}, "none"},
		{"high", nil, "high"},                       // unknown vocabulary passes through
		{"", []string{"low"}, ""},                   // nothing requested
		{"turbo", []string{"low", "high"}, "turbo"}, // not a ladder level: passes through
	}
	for _, c := range cases {
		if got := config.ClampEffort(c.requested, c.supported); got != c.want {
			t.Errorf("ClampEffort(%q, %v) = %q, want %q", c.requested, c.supported, got, c.want)
		}
	}
}

func TestMissingConfigLoadsAndEndpointExplains(t *testing.T) {
	cfg := load(t, home(t, "", ""))
	if _, err := cfg.Endpoint(""); err == nil {
		t.Fatal("Endpoint with no provider configured must fail")
	}
}

func TestUnknownProviderIsNamedInTheError(t *testing.T) {
	cfg := load(t, home(t, strings.Replace(sample, `provider = "custom"`, `provider = "nope"`, 1), ""))
	_, err := cfg.Endpoint("")
	if err == nil || !strings.Contains(err.Error(), "nope") {
		t.Fatalf("err = %v, want it to name the missing provider", err)
	}
}

func TestMalformedConfigIsAnError(t *testing.T) {
	if _, err := config.Load(home(t, "[model\ndefault=", "")); err == nil {
		t.Fatal("Load must fail on invalid TOML")
	}
}

func TestPermissionMode(t *testing.T) {
	cases := []struct {
		extra string
		want  approval.Mode
	}{
		{"", approval.Default},
		{"[approvals]\nmode = \"smart\"\n", approval.Default},
		{"[approvals]\nmode = \"manual\"\n", approval.Ask},
		{"[approvals]\nmode = \"off\"\n", approval.Yolo},
		{"[approvals]\nmode = \"off\"\n[security]\nlockdown = true\n", approval.Lockdown},
		{"[security]\nlockdown = true\n", approval.Lockdown},
	}
	for _, c := range cases {
		if got := load(t, home(t, sample+c.extra, "")).PermissionMode(); got != c.want {
			t.Errorf("PermissionMode with %q = %q, want %q", c.extra, got, c.want)
		}
	}
}

func TestWorkerHours(t *testing.T) {
	for _, c := range []struct {
		toml       string
		start, end int
		ok, warn   bool
	}{
		{"", 0, 0, false, false},
		{"[relay]\nworker_hours = []\n", 0, 0, false, false},
		{"[relay]\nworker_hours = [20, 7]\n", 20, 7, true, false},
		{"[relay]\nworker_hours = [5, 5]\n", 0, 0, false, false},
		{"[relay]\nworker_hours = [20]\n", 0, 0, false, true},
		{"[relay]\nworker_hours = [20, 24]\n", 0, 0, false, true},
		{"[relay]\nworker_hours = \"night\"\n", 0, 0, false, true},
	} {
		start, end, ok, warn := load(t, home(t, c.toml, "")).WorkerHours()
		if start != c.start || end != c.end || ok != c.ok || (warn != "") != c.warn {
			t.Errorf("%q: got %d, %d, %v, %q", c.toml, start, end, ok, warn)
		}
	}
}

func TestCommandHomeIsTheWorkingDirectoryUnderCwdMode(t *testing.T) {
	for _, c := range []struct{ toml, want string }{
		{"[terminal]\ncwd = \"/srv/shop\"\nhome_mode = \"cwd\"\n", "/srv/shop"},
		{"[terminal]\ncwd = \"/srv/shop\"\n", ""},
		{"[terminal]\ncwd = \"/srv/shop\"\nhome_mode = \"auto\"\n", ""},
	} {
		home := t.TempDir()
		if err := os.WriteFile(filepath.Join(home, "config.toml"), []byte(c.toml), 0o600); err != nil {
			t.Fatal(err)
		}
		cfg, err := config.Load(home)
		if err != nil {
			t.Fatal(err)
		}
		if got := cfg.CommandHome(); got != c.want {
			t.Errorf("%q: CommandHome = %q, want %q", c.toml, got, c.want)
		}
	}
}

func TestMessageTimestampsDefaultOnAndCanBeTurnedOff(t *testing.T) {
	cfg := load(t, home(t, "[agent]\n", ""))
	if !cfg.MessageTimestampsEnabled() {
		t.Fatal("message_timestamps must default on")
	}
	cfg = load(t, home(t, "[agent]\nmessage_timestamps = false\n", ""))
	if cfg.MessageTimestampsEnabled() {
		t.Fatal("message_timestamps = false must turn it off")
	}
}

// Images ride the turns of a vision-capable model and come back as an error
// on one declared text-only: [model].supports_vision is the instance-wide
// declaration, a per-model route overrides it, unset defaults to true.
func TestSupportsVisionResolution(t *testing.T) {
	base := `[model]
default = "m"
provider = "custom"
`
	custom := `
[custom_providers.custom]
base_url = "http://maitred.test/v1"
`
	for _, c := range []struct {
		name  string
		toml  string
		model string
		want  bool
	}{
		{"unset is true", base + custom, "any-model", true},
		{"the instance-wide declaration", base + "supports_vision = false\n" + custom, "any-model", false},
		{"a per-model route wins over it", base + "supports_vision = false\n" + custom +
			"\n[custom_providers.custom.models.\"m\"]\nsupports_vision = true\n", "m", true},
		{"another model keeps the declaration", base + "supports_vision = false\n" + custom +
			"\n[custom_providers.custom.models.\"m\"]\nsupports_vision = true\n", "other", false},
		{"a renamed model resolves", base + "supports_vision = false\n" + custom +
			"\n[models]\nrenamed.\"old\" = \"m\"\n\n" +
			"[custom_providers.custom.models.\"m\"]\nsupports_vision = false\n", "old", false},
	} {
		cfg := load(t, home(t, c.toml, ""))
		if got := cfg.SupportsVision(c.model); got != c.want {
			t.Errorf("%s: SupportsVision(%q) = %v, want %v", c.name, c.model, got, c.want)
		}
	}
}
