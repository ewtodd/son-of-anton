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
// when nothing listens. With --socket it dials that socket alone (a view
// socket, daemon/phases/12-view-socket.md): no daemon is started and no home
// is used. --view makes it read-only.
func runTUI(args []string, stderr io.Writer) int {
	bad := func(format string, a ...any) int {
		fmt.Fprintf(stderr, "soa-d: "+format+"\n\n%s", append(a, usage)...)
		return 2
	}
	var (
		home, in, socket string
		o                tui.Options
	)
	for i := 0; i < len(args); i++ {
		arg := args[i]
		switch arg {
		case "--home", "--in", "--resume", "-m", "--model", "--reasoning", "--socket":
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
			case "--socket":
				socket = args[i]
			default:
				o.Model = args[i]
			}
		case "-c":
			o.Continue = true
		case "--yolo":
			o.Yolo = true
		case "--view":
			o.View = true
		default:
			return bad("unknown tui argument %q", arg)
		}
	}
	switch {
	case o.Continue && o.Resume != "":
		return bad("-c and --resume cannot be used together")
	case socket != "" && home != "":
		return bad("--socket and --home cannot be used together: the socket is all a view needs")
	case o.View && o.Resume == "":
		return bad("--view needs --resume ID|latest")
	case o.View && (o.Model != "" || o.Reasoning != "" || o.Yolo):
		return bad("--view only watches: -m, --reasoning and --yolo would change the session")
	}
	fail := func(err error) int {
		fmt.Fprintf(stderr, "soa-d: %v\n", err)
		return 1
	}
	var c *client.Client
	if socket != "" {
		var err error
		if c, err = client.DialSocket(socket, "tui"); err != nil {
			return fail(fmt.Errorf("cannot reach %s: %w", socket, err))
		}
		o.Socket = socket
	} else {
		if home == "" {
			var err error
			if home, err = config.Home(); err != nil {
				return fail(err)
			}
		}
		var err error
		if c, err = client.Connect(home, "tui"); err != nil {
			return fail(err)
		}
		o.Home, o.Autostart = home, true
	}
	defer c.Close()
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
	o.Cwd, o.Stderr = cwd, stderr
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
