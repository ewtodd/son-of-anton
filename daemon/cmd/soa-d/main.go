// Command soa-d is the Son of Anton daemon (phase 1: query and oneshot).
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/buildinfo"
	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/lsp"
	"github.com/ewtodd/son-of-anton/daemon/internal/mcp"
	"github.com/ewtodd/son-of-anton/daemon/internal/nixmerge"
	"github.com/ewtodd/son-of-anton/daemon/internal/paths"
	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/recall"
	"github.com/ewtodd/son-of-anton/daemon/internal/server"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// version is what `soa-d version` names after the command.
func version() string { return "soa-d " + buildinfo.Version }

const usage = `soa-d — the Son of Anton daemon

usage:
  soa-d query [--home DIR] [--model M] [--reasoning LEVEL] [--yolo] [--system FILE] [--memory | --no-memory] PROMPT
  soa-d oneshot [--home DIR] [--model M] [--reasoning LEVEL] [--yolo] [--system FILE] [--memory | --no-memory] [--usage-file PATH] PROMPT
  soa-d serve [--home DIR]
  soa-d sandbox-run [--home DIR] --run DIR [--data PATH]... -- COMMAND...
  soa-d tui [--home DIR] [--in DIR] [-c | --resume ID|latest] [-m MODEL] [--reasoning LEVEL] [--yolo]
  soa-d import --from DIR [--home DIR]
  soa-d import (--from-claude [ROOT] | --from-codex [ROOT] | --from-opencode [DB]) [--home DIR] [--force]
  soa-d config merge DECLARED CONFIG [--state STATE] [--adopt] [--dry-run]
  soa-d rules merge DECLARED RULES --state STATE
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
		fmt.Fprintln(stdout, version())
		return 0
	case "query":
		return runQuery(args[1:], stdout, stderr)
	case "oneshot":
		return runOneshot(args[1:], stdout, stderr)
	case "import":
		return runImport(args[1:], stdout, stderr)
	case "sandbox-run":
		return runSandbox(args[1:], stdout, stderr)
	case "serve":
		return runServe(args[1:], stderr)
	case "tui":
		return runTUI(args[1:], stderr)
	case "config":
		return nixmerge.RunConfig(args[1:], stderr)
	case "rules":
		return nixmerge.RunRules(args[1:], stderr)
	default:
		fmt.Fprint(stderr, usage)
		return 2
	}
}

// options are the flags query and oneshot share.
type options struct {
	home, model, system, usageFile, reasoning string
	yolo                                      bool
	memory                                    bool
	prompt                                    string
}

// parseOptions reads the flags and joins the rest into the prompt. ok is
// false (after printing usage) for a bad invocation.
func parseOptions(args []string, defaultMemory, allowUsageFile bool, stderr io.Writer) (options, bool) {
	o := options{memory: defaultMemory}
	var words []string
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--home", "--model", "--system", "--usage-file", "--reasoning":
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
			case "--reasoning":
				o.reasoning = v
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
	idx    *recall.Index // nil: no recall
	mcp    *mcp.Manager  // nil: no MCP server is enabled
	lsp    *lsp.Service  // nil: LSP is disabled
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
	if o.reasoning != "" {
		ep.ReasoningEffort = o.reasoning
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
	mem, idx := recall.Memory(cfg, home, source == "query" && o.memory)
	mcpm := server.MCPManager(cfg, home)
	lspS := server.LSPService(cfg)
	base := tools.Builtin()
	if mcpm != nil {
		base = append(base, tools.MCPTools(mcpm.Names())...)
	}
	su := &setup{st: st, mcp: mcpm, lsp: lspS, model: ep.Model, runner: &agent.Runner{
		Store:             st,
		Client:            provider.New(ep.BaseURL, ep.APIKey),
		Model:             ep.Model,
		ReasoningEffort:   ep.ReasoningEffort,
		Source:            source,
		Tools:             tools.NewRegistry(base...),
		Allowlist:         st.Allowlist(),
		Policy:            paths.Policy{Home: userHome(), SoaHome: home, WriteExceptions: cfg.Security.SensitivePathExceptions},
		CommandHome:       cfg.CommandHome(),
		Mode:              queryMode(cfg, o.yolo),
		MessageTimestamps: cfg.MessageTimestampsEnabled(),
		Memory:            mem,
		MemoryScope:       "common",
		MemoryApproval:    cfg.Memory.WriteApproval,
		Web:               server.WebConfig(cfg, home),
		MCP:               mcpm,
		LSP:               lspS,
		Vision:            cfg.SupportsVision,
		Prompt: func(_ context.Context, s store.Session) (string, error) {
			if o.system != "" {
				return string(system), nil
			}
			r, err := prompt.Build(prompt.Options{
				Home: home, Cwd: s.Cwd, Scope: "common", Memory: o.memory,
				Now: time.Now().In(prompt.LocalZone()), RulesCharLimit: cfg.Memory.RulesCharLimit,
			})
			for _, w := range r.Warnings {
				fmt.Fprintf(stderr, "soa-d: %s\n", w)
			}
			return r.Text, err
		},
	}}
	if su.runner.Mode != approval.Yolo {
		su.runner.NoScripts = "execute_code is not available in this run: nobody is here to approve a script. Start the run with --yolo to allow it."
	}
	// A full-memory query recalls; a lean oneshot never does.
	if idx != nil {
		su.idx = idx
		su.runner.Recall = func(ctx context.Context, text string) string { return idx.Recall(ctx, "common", text) }
	}
	return su, nil
}

// close releases the store and the recall index.
func (su *setup) close() {
	if su.mcp != nil {
		su.mcp.Close()
	}
	su.lsp.Close()
	if su.idx != nil {
		_ = su.idx.Close()
	}
	_ = su.st.Close()
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
	defer su.close()
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
		defer su.close()
		model = su.model
		ctx := context.Background()
		sess, err := su.runner.NewSession(ctx, "oneshot", cwd)
		if err != nil {
			return err
		}
		defer sess.Close()
		sessionID = sess.ID()
		res, err = sess.Send(ctx, o.prompt, nil)
		return err
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

// queryMode resolves the permission mode for a run: --yolo wins, otherwise
// the config's [approvals] mode.
func queryMode(cfg *config.Config, yolo bool) approval.Mode {
	if yolo {
		return approval.Yolo
	}
	return cfg.PermissionMode()
}

// runServe is the daemon: listen first (a second daemon on the same home
// stops here), then serve until SIGINT or SIGTERM.
func runServe(args []string, stderr io.Writer) int {
	home := ""
	for i := 0; i < len(args); i++ {
		if args[i] != "--home" || i+1 >= len(args) {
			fmt.Fprint(stderr, usage)
			return 2
		}
		home = args[i+1]
		i++
	}
	if home == "" {
		var err error
		if home, err = config.Home(); err != nil {
			fmt.Fprintf(stderr, "soa-d serve: %v\n", err)
			return 1
		}
	}
	// Nothing the daemon runs may gain privileges (sudo, su, any setuid or
	// setcap program), however the daemon was started: a unit, or a TUI. The
	// flag is per thread and Go forks from any thread, so every thread gets
	// it (AllThreadsSyscall needs a build without cgo, as soa-d always is).
	if _, _, errno := syscall.AllThreadsSyscall(syscall.SYS_PRCTL, unix.PR_SET_NO_NEW_PRIVS, 1, 0); errno != 0 {
		fmt.Fprintf(stderr, "soa-d serve: setting no_new_privs: %v\n", errno)
		return 1
	}
	ln, err := server.Listen(home)
	if errors.Is(err, server.ErrRunning) {
		fmt.Fprintf(stderr, "soa-d serve: already running (%s)\n", filepath.Join(home, "run", "soa-d.sock"))
		return 1
	}
	if err != nil {
		fmt.Fprintf(stderr, "soa-d serve: %v\n", err)
		return 1
	}
	retirePython(home, stderr)
	srv, err := server.New(server.Options{Home: home, Version: buildinfo.Version})
	if err != nil {
		_ = ln.Close()
		fmt.Fprintf(stderr, "soa-d serve: %v\n", err)
		return 1
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	fmt.Fprintf(stderr, "soa-d serve: listening on %s\n", ln.Addr())
	err = srv.Serve(ctx, ln)
	fmt.Fprintln(stderr, "soa-d serve: stopped")
	if err != nil {
		fmt.Fprintf(stderr, "soa-d serve: %v\n", err)
		return 1
	}
	return 0
}
