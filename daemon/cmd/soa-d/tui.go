package main

import (
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/ewtodd/son-of-anton/daemon/internal/buildinfo"
	"github.com/ewtodd/son-of-anton/daemon/internal/client"
	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/tui"
)

// runTUI is `soa-d tui`: the terminal client of the daemon, which it starts
// when nothing listens.
func runTUI(args []string, stderr io.Writer) int {
	bad := func(format string, a ...any) int {
		fmt.Fprintf(stderr, "soa-d: "+format+"\n\n%s", append(a, usage)...)
		return 2
	}
	var (
		home, in string
		o        tui.Options
	)
	for i := 0; i < len(args); i++ {
		arg := args[i]
		switch arg {
		case "--home", "--in", "--resume", "-m", "--model", "--reasoning":
			if i+1 >= len(args) {
				return bad("%s needs a value", arg)
			}
			i++
			switch arg {
			case "--home":
				home = args[i]
			case "--in":
				in = args[i]
			case "--resume":
				o.Resume = args[i]
			case "--reasoning":
				o.Reasoning = args[i]
			default:
				o.Model = args[i]
			}
		case "-c":
			o.Continue = true
		case "--yolo":
			o.Yolo = true
		default:
			return bad("unknown tui argument %q", arg)
		}
	}
	if o.Continue && o.Resume != "" {
		return bad("-c and --resume cannot be used together")
	}
	fail := func(err error) int {
		fmt.Fprintf(stderr, "soa-d: %v\n", err)
		return 1
	}
	if home == "" {
		var err error
		if home, err = config.Home(); err != nil {
			return fail(err)
		}
	}
	cwd := in
	if cwd == "" {
		var err error
		if cwd, err = os.Getwd(); err != nil {
			return fail(err)
		}
	}
	cwd, err := filepath.Abs(cwd)
	if err != nil {
		return fail(err)
	}
	c, err := client.Connect(home, "tui")
	if err != nil {
		return fail(err)
	}
	defer c.Close()
	o.Home, o.Cwd, o.Autostart, o.Stderr = home, cwd, true, stderr
	o.Version = buildinfo.Version
	app, err := tui.Start(c, o, os.Stdin, os.Stdout)
	if err != nil {
		return fail(err)
	}
	if err := app.Wait(); err != nil {
		return fail(err)
	}
	return 0
}
