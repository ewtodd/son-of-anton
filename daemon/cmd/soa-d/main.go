// Command soa-d is the Son of Anton daemon (phase 1: query and oneshot).
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/paths"
	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

const version = "soa-d 0.0.1"

const usage = `soa-d — the Son of Anton daemon

usage:
  soa-d query [--home DIR] [--model M] [--yolo] [--system FILE] [--memory | --no-memory] PROMPT
  soa-d oneshot [--home DIR] [--model M] [--yolo] [--system FILE] [--memory | --no-memory] [--usage-file PATH] PROMPT
  soa-d version
`

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// run executes one CLI invocation and returns the process exit code.
func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprint(stderr, usage)
		return 2
	}
	switch args[0] {
	case "version":
		fmt.Fprintln(stdout, version)
		return 0
	case "query":
		return runQuery(args[1:], stdout, stderr)
	case "oneshot":
		return runOneshot(args[1:], stdout, stderr)
	default:
		fmt.Fprint(stderr, usage)
		return 2
	}
}

// options are the flags query and oneshot share.
type options struct {
	home, model, system, usageFile string
	yolo                           bool
	memory                         bool
	prompt                         string
}

// parseOptions reads the flags and joins the rest into the prompt. ok is
// false (after printing usage) for a bad invocation.
func parseOptions(args []string, defaultMemory, allowUsageFile bool, stderr io.Writer) (options, bool) {
	o := options{memory: defaultMemory}
	var words []string
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--home", "--model", "--system", "--usage-file":
			if i+1 >= len(args) || (args[i] == "--usage-file" && !allowUsageFile) {
				fmt.Fprintf(stderr, "soa-d: %s needs a value\n\n%s", args[i], usage)
				return o, false
			}
			v := args[i+1]
			i++
			switch args[i-1] {
			case "--home":
				o.home = v
			case "--model":
				o.model = v
			case "--system":
				o.system = v
			default:
				o.usageFile = v
			}
		case "--yolo":
			o.yolo = true
		case "--memory":
			o.memory = true
		case "--no-memory":
			o.memory = false
		default:
			words = append(words, args[i])
		}
	}
	o.prompt = strings.Join(words, " ")
	if o.prompt == "" {
		fmt.Fprint(stderr, usage)
		return o, false
	}
	return o, true
}

// setup is everything a run needs: the open store and a runner.
type setup struct {
	st     *store.Store
	runner *agent.Runner
	model  string
}

func newSetup(o options, source string, stderr io.Writer) (*setup, error) {
	home := o.home
	if home == "" {
		var err error
		if home, err = config.Home(); err != nil {
			return nil, err
		}
	}
	cfg, err := config.Load(home)
	if err != nil {
		return nil, err
	}
	ep, err := cfg.Endpoint(o.model)
	if err != nil {
		return nil, err
	}
	var system []byte
	if o.system != "" {
		if system, err = os.ReadFile(o.system); err != nil {
			return nil, err
		}
	}
	st, err := store.Open(filepath.Join(home, "soa-d.db"))
	if err != nil {
		return nil, err
	}
	return &setup{st: st, model: ep.Model, runner: &agent.Runner{
		Store:           st,
		Client:          provider.New(ep.BaseURL, ep.APIKey),
		Model:           ep.Model,
		ReasoningEffort: ep.ReasoningEffort,
		Source:          source,
		Tools:           tools.NewRegistry(tools.Builtin()...),
		Allowlist:       st.Allowlist(),
		Policy:          paths.Policy{Home: userHome(), SoaHome: home},
		Mode:            queryMode(cfg, o.yolo),
		Prompt: func(_ context.Context, s store.Session) (string, error) {
			if o.system != "" {
				return string(system), nil
			}
			r, err := prompt.Build(prompt.Options{
				Home: home, Cwd: s.Cwd, Scope: "common", Memory: o.memory,
				Now: time.Now().In(localZone()), RulesCharLimit: cfg.Memory.RulesCharLimit,
			})
			for _, w := range r.Warnings {
				fmt.Fprintf(stderr, "soa-d: %s\n", w)
			}
			return r.Text, err
		},
	}}, nil
}

func runQuery(args []string, stdout, stderr io.Writer) int {
	o, ok := parseOptions(args, true, false, stderr)
	if !ok {
		return 2
	}
	fail := func(err error) int {
		fmt.Fprintf(stderr, "soa-d: %v\n", err)
		return 1
	}
	cwd, err := os.Getwd()
	if err != nil {
		return fail(err)
	}
	su, err := newSetup(o, "query", stderr)
	if err != nil {
		return fail(err)
	}
	defer su.st.Close()
	ctx := context.Background()
	sess, err := su.runner.NewSession(ctx, "oneshot", cwd)
	if err != nil {
		return fail(err)
	}
	defer sess.Close()
	if _, err := sess.Send(ctx, o.prompt, func(e agent.Event) {
		if e.Kind == "assistant.delta" {
			fmt.Fprint(stdout, e.Text)
		}
	}); err != nil {
		return fail(err)
	}
	fmt.Fprintln(stdout)
	return 0
}

// runOneshot is the -z replacement: a lean, unattended run that prints only
// the final answer.
func runOneshot(args []string, stdout, stderr io.Writer) int {
	o, ok := parseOptions(args, false, true, stderr)
	if !ok {
		return 2
	}
	var (
		sessionID, model string
		res              agent.Result
	)
	err := func() error {
		cwd, err := os.Getwd()
		if err != nil {
			return err
		}
		su, err := newSetup(o, "oneshot", stderr)
		if err != nil {
			return err
		}
		defer su.st.Close()
		model = su.model
		ctx := context.Background()
		sess, err := su.runner.NewSession(ctx, "oneshot", cwd)
		if err != nil {
			return err
		}
		defer sess.Close()
		sessionID = sess.ID()
		if res, err = sess.Send(ctx, o.prompt, nil); err != nil {
			return err
		}
		if strings.TrimSpace(res.Content) == "" {
			return errors.New("the model returned an empty final answer")
		}
		return nil
	}()
	if o.usageFile != "" {
		if werr := writeUsage(o.usageFile, res, model, sessionID, err); werr != nil {
			fmt.Fprintf(stderr, "soa-d oneshot: writing usage file: %v\n", werr)
		}
	}
	if err != nil {
		fmt.Fprintf(stderr, "soa-d oneshot: %v\n", err)
		return 1
	}
	fmt.Fprintln(stdout, res.Content)
	return 0
}

func writeUsage(path string, res agent.Result, model, sessionID string, runErr error) error {
	u := map[string]any{
		"input_tokens":      res.Usage.PromptTokens,
		"output_tokens":     res.Usage.CompletionTokens,
		"cache_read_tokens": res.Usage.CachedTokens,
		"reasoning_tokens":  res.Usage.ReasoningTokens,
		"total_tokens":      res.Usage.PromptTokens + res.Usage.CompletionTokens,
		"api_calls":         res.APICalls,
		"model":             model,
		"session_id":        sessionID,
		"completed":         runErr == nil,
		"failed":            runErr != nil,
	}
	if runErr != nil {
		u["failure"] = runErr.Error()
	}
	b, err := json.MarshalIndent(u, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(b, '\n'), 0o600)
}

// userHome is the OS user's home directory, for the path policy.
func userHome() string {
	if h, err := os.UserHomeDir(); err == nil {
		return h
	}
	return ""
}

// localZone is time.Local with its zone name, which time.Local alone prints
// as "Local": TZ, else the zoneinfo path /etc/localtime points at. Without
// either, time.Local.
func localZone() *time.Location {
	name := strings.TrimPrefix(os.Getenv("TZ"), ":")
	if name == "" {
		if p, err := filepath.EvalSymlinks("/etc/localtime"); err == nil {
			if i := strings.LastIndex(p, "zoneinfo/"); i >= 0 {
				name = p[i+len("zoneinfo/"):]
			}
		}
	}
	if name != "" {
		if loc, err := time.LoadLocation(name); err == nil {
			return loc
		}
	}
	return time.Local
}

// queryMode resolves the permission mode for a run: --yolo wins, otherwise
// the config's [approvals] mode.
func queryMode(cfg *config.Config, yolo bool) approval.Mode {
	if yolo {
		return approval.Yolo
	}
	return cfg.PermissionMode()
}
