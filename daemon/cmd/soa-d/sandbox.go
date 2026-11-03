package main

import (
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/sandbox"
)

// sandboxFailed is the exit code of a sandbox-run that could not run the
// command at all (a command's own exit code is passed through).
const sandboxFailed = 125

// runSandbox is `soa-d sandbox-run --run DIR [--data PATH]... -- COMMAND...`:
// one command in the physics sandbox, with the configured runtime.
func runSandbox(args []string, stdout, stderr io.Writer) int {
	var home, runDir string
	var data []string
	i := 0
loop:
	for ; i < len(args); i++ {
		switch args[i] {
		case "--home", "--run", "--data":
			if i+1 >= len(args) {
				fmt.Fprintf(stderr, "soa-d: %s needs a value\n\n%s", args[i], usage)
				return 2
			}
			i++
			switch args[i-1] {
			case "--home":
				home = args[i]
			case "--run":
				runDir = args[i]
			default:
				data = append(data, args[i])
			}
		case "--":
			i++
			break loop
		default:
			fmt.Fprintf(stderr, "soa-d: sandbox-run: unexpected %q (the command goes after --)\n\n%s", args[i], usage)
			return 2
		}
	}
	command := args[i:]
	if runDir == "" || len(command) == 0 {
		fmt.Fprint(stderr, usage)
		return 2
	}
	fail := func(err error) int {
		fmt.Fprintf(stderr, "soa-d: sandbox-run: %v\n", err)
		return sandboxFailed
	}

	if home == "" {
		var err error
		if home, err = config.Home(); err != nil {
			return fail(err)
		}
	}
	cfg, err := config.Load(home)
	if err != nil {
		return fail(err)
	}
	if cfg.Physics.Python == "" {
		return fail(errors.New("physics.python is not set, so there is no runtime to run the command with"))
	}
	pol, err := sandboxPolicy(cfg.Physics.Python, cfg.Physics.DataDirs, runDir, data)
	if err != nil {
		return fail(err)
	}
	cmd, err := pol.Command(nil, pol.Bash(), "-c", `exec "$@"`, "sandbox-run")
	if err != nil {
		return fail(err)
	}
	cmd.Args = append(cmd.Args, command...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, stdout, stderr
	if err := cmd.Run(); err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			return ee.ExitCode()
		}
		return fail(err)
	}
	return 0
}

// sandboxPolicy checks the run directory and the data paths against the
// configured data directories and builds the policy.
func sandboxPolicy(python string, dataDirs []string, runDir string, data []string) (sandbox.Policy, error) {
	run, err := filepath.Abs(runDir)
	if err != nil {
		return sandbox.Policy{}, err
	}
	if fi, err := os.Stat(run); err != nil || !fi.IsDir() {
		return sandbox.Policy{}, fmt.Errorf("--run %s is not an existing directory", runDir)
	}
	if run, err = filepath.EvalSymlinks(run); err != nil {
		return sandbox.Policy{}, err
	}
	var allowed []string
	for _, d := range dataDirs {
		if real, err := filepath.EvalSymlinks(d); err == nil {
			allowed = append(allowed, real)
		}
	}
	var binds []string
	for _, d := range data {
		abs, err := filepath.Abs(d)
		if err != nil {
			return sandbox.Policy{}, err
		}
		real, err := filepath.EvalSymlinks(abs)
		if err != nil {
			return sandbox.Policy{}, fmt.Errorf("--data %s: %v", d, err)
		}
		ok := false
		for _, a := range allowed {
			if real == a || strings.HasPrefix(real, a+"/") {
				ok = true
			}
		}
		if !ok {
			return sandbox.Policy{}, fmt.Errorf("--data %s is not under physics.data_dirs", d)
		}
		binds = append(binds, real)
	}
	return sandbox.Policy{
		RunDir:  run,
		Data:    binds,
		Runtime: filepath.Dir(python),
		Home:    filepath.Join(run, ".home"),
	}, nil
}
