package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/client"
)

// Exit codes of a followed run.
const (
	exitPassed  = 0
	exitFailed  = 1
	exitStopped = 3
)

// runProblem is `son-of-anton problem run|list|watch|stop|resume` (phase 11c):
// physics runs, which the daemon drives.
func runProblem(f flags, stdout, stderr io.Writer) int {
	args := f.rest
	if len(args) == 0 {
		return usageErr(stderr, "problem needs an action: run, list, watch, stop or resume")
	}
	action, args := args[0], args[1:]
	switch action {
	case "run", "list", "watch", "stop", "resume":
	default:
		return usageErr(stderr, "unknown problem action %q", action)
	}

	params := map[string]any{}
	var pos []string
	wait := false
	home := f.home
	for i := 0; i < len(args); i++ {
		a := args[i]
		name, value, joined := a, "", false
		if strings.HasPrefix(a, "--") {
			if k, v, ok := strings.Cut(a, "="); ok {
				name, value, joined = k, v, true
			}
		}
		value1 := func() (string, bool) {
			if joined {
				return value, true
			}
			if i+1 >= len(args) {
				return "", false
			}
			i++
			return args[i], true
		}
		switch {
		case action == "run" && name == "--wait":
			wait = true
		case name == "--home":
			v, ok := value1()
			if !ok {
				return usageErr(stderr, "--home needs a value")
			}
			home = v
		case action == "run" && (name == "--model" || name == "--manager-model"):
			v, ok := value1()
			if !ok || strings.TrimSpace(v) == "" {
				return usageErr(stderr, "%s needs a value", name)
			}
			params[strings.ReplaceAll(strings.TrimPrefix(name, "--"), "-", "_")] = v
		case action == "run" && (name == "--max-sessions" || name == "--session-tokens" || name == "--total-tokens"):
			v, ok := value1()
			n, err := strconv.Atoi(v)
			if !ok || err != nil || n < 1 {
				return usageErr(stderr, "%s needs a positive number", name)
			}
			params[strings.ReplaceAll(strings.TrimPrefix(name, "--"), "-", "_")] = n
		case action == "run" && name == "--hours":
			v, ok := value1()
			h, err := strconv.ParseFloat(v, 64)
			if !ok || err != nil || h <= 0 {
				return usageErr(stderr, "--hours needs a positive number")
			}
			params["hours"] = h
		case strings.HasPrefix(a, "-") && a != "-":
			return usageErr(stderr, "unknown flag %s", a)
		default:
			pos = append(pos, a)
		}
	}
	switch action {
	case "run":
		if len(pos) != 1 {
			return usageErr(stderr, "usage: problem run SPEC [flags]")
		}
	case "list":
		if len(pos) != 0 {
			return usageErr(stderr, "problem list takes no arguments")
		}
	default:
		if len(pos) != 1 {
			return usageErr(stderr, "usage: problem %s ID", action)
		}
	}
	if action == "run" {
		b, err := os.ReadFile(pos[0])
		if err != nil {
			return fail(stderr, "cannot read the spec: %v", err)
		}
		params["spec"] = string(b)
	}

	f.home = home
	h, err := homeOf(f)
	if err != nil {
		return fail(stderr, "%v", err)
	}
	c, err := client.Connect(h, "cli")
	if err != nil {
		return fail(stderr, "%v", err)
	}
	defer c.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()

	var run struct {
		ID           string `json:"id"`
		Name         string `json:"name"`
		Dir          string `json:"dir"`
		State        string `json:"state"`
		SessionsDone int    `json:"sessions_done"`
		Tokens       int    `json:"tokens"`
		Started      string `json:"started"`
	}
	switch action {
	case "list":
		var res struct {
			Runs []struct {
				ID           string `json:"id"`
				State        string `json:"state"`
				SessionsDone int    `json:"sessions_done"`
				Tokens       int    `json:"tokens"`
			} `json:"runs"`
		}
		if err := c.Call(ctx, "physics.list", nil, &res); err != nil {
			return fail(stderr, "%v", err)
		}
		if len(res.Runs) == 0 {
			fmt.Fprintln(stdout, "no runs")
		}
		for _, r := range res.Runs {
			fmt.Fprintf(stdout, "%s  %s  %d sessions  %d tokens\n", r.ID, r.State, r.SessionsDone, r.Tokens)
		}
		return 0
	case "run":
		if err := c.Call(ctx, "physics.run", params, &run); err != nil {
			return fail(stderr, "%v", err)
		}
		fmt.Fprintf(stdout, "run: %s\ndirectory: %s\n", run.ID, run.Dir)
		if !wait {
			return 0
		}
		return follow(c, run.ID, stdout, stderr)
	case "watch":
		return follow(c, pos[0], stdout, stderr)
	case "stop", "resume":
		if err := c.Call(ctx, "physics."+action, map[string]any{"run": pos[0]}, &run); err != nil {
			return fail(stderr, "%v", err)
		}
		fmt.Fprintf(stdout, "run %s: %s\n", run.ID, run.State)
		return 0
	}
	return 0
}

// exitFor is the exit code of a run that ended in state.
func exitFor(state string) int {
	switch state {
	case "passed":
		return exitPassed
	case "failed":
		return exitFailed
	}
	return exitStopped
}

// follow prints the run's progress, one line per event, and returns its exit
// code once it ends.
func follow(c *client.Client, id string, stdout, stderr io.Writer) int {
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	var res struct {
		State string   `json:"state"`
		Lines []string `json:"lines"`
	}
	if err := c.Call(ctx, "physics.watch", map[string]any{"run": id}, &res); err != nil {
		return fail(stderr, "%v", err)
	}
	for _, l := range res.Lines {
		fmt.Fprintln(stdout, l)
	}
	if res.State != "working" && res.State != "reviewing" {
		fmt.Fprintf(stdout, "run %s: %s\n", id, res.State)
		return exitFor(res.State)
	}
	done := c.Done()
	for {
		select {
		case ev, ok := <-c.Events():
			if !ok {
				return fail(stderr, "%v", errors.New("the connection to soa-d was lost; the run goes on, watch it again with: son-of-anton problem watch "+id))
			}
			if ev.Params["run"] != id {
				continue
			}
			switch ev.Kind {
			case "physics.line":
				fmt.Fprintln(stdout, ev.Params["text"])
			case "physics.end":
				state, _ := ev.Params["state"].(string)
				fmt.Fprintf(stdout, "run %s: %s\n", id, state)
				return exitFor(state)
			}
		case <-done:
			done = nil // events still queued are delivered before the channel closes
		}
	}
}
