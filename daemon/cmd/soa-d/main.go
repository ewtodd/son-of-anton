// Command soa-d is the Son of Anton daemon (phase 0: oneshot query).
package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

const version = "soa-d 0.0.1"

const usage = `soa-d — the Son of Anton daemon

usage:
  soa-d query [--home DIR] [--model M] PROMPT
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
	default:
		fmt.Fprint(stderr, usage)
		return 2
	}
}

func runQuery(args []string, stdout, stderr io.Writer) int {
	home, model := "", ""
	var promptWords []string
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--home", "--model":
			if i+1 >= len(args) {
				fmt.Fprintf(stderr, "soa-d: %s needs a value\n\n%s", args[i], usage)
				return 2
			}
			if args[i] == "--home" {
				home = args[i+1]
			} else {
				model = args[i+1]
			}
			i++
		default:
			promptWords = append(promptWords, args[i])
		}
	}
	prompt := strings.Join(promptWords, " ")
	if prompt == "" {
		fmt.Fprint(stderr, usage)
		return 2
	}

	fail := func(err error) int {
		fmt.Fprintf(stderr, "soa-d: %v\n", err)
		return 1
	}

	if home == "" {
		var err error
		home, err = config.Home()
		if err != nil {
			return fail(err)
		}
	}
	cfg, err := config.Load(home)
	if err != nil {
		return fail(err)
	}
	ep, err := cfg.Endpoint(model)
	if err != nil {
		return fail(err)
	}
	cwd, err := os.Getwd()
	if err != nil {
		return fail(err)
	}
	st, err := store.Open(filepath.Join(home, "soa-d.db"))
	if err != nil {
		return fail(err)
	}
	defer st.Close()

	promptText, err := soulPrompt(home)
	if err != nil {
		return fail(err)
	}

	runner := &agent.Runner{
		Store:           st,
		Client:          provider.New(ep.BaseURL, ep.APIKey),
		Model:           ep.Model,
		ReasoningEffort: ep.ReasoningEffort,
		Source:          "query",
		Prompt: func(context.Context, store.Session) (string, error) {
			return promptText, nil
		},
	}
	ctx := context.Background()
	sess, err := runner.NewSession(ctx, "oneshot", cwd)
	if err != nil {
		return fail(err)
	}
	if _, err := sess.Send(ctx, prompt, func(e agent.Event) {
		if e.Kind == "assistant.delta" {
			fmt.Fprint(stdout, e.Text)
		}
	}); err != nil {
		return fail(err)
	}
	fmt.Fprintln(stdout)
	return 0
}

// soulPrompt returns the home's SOUL.md, trimmed, when it has content;
// otherwise one short sentence identifying Son of Anton.
func soulPrompt(home string) (string, error) {
	data, err := os.ReadFile(filepath.Join(home, "SOUL.md"))
	if err == nil {
		if s := strings.TrimSpace(string(data)); s != "" {
			return s, nil
		}
	} else if !os.IsNotExist(err) {
		return "", err
	}
	return "You are Son of Anton, an always-on assistant daemon.", nil
}
