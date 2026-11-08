package main

// son-of-anton view: watch an instance's sessions through its view socket
// (daemon/phases/12-view-socket.md). It reaches the daemon through the socket
// alone: it never starts one and never reads the instance home, which a
// viewer cannot read.

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/client"
)

func runView(args []string, stdout, stderr io.Writer) int {
	var (
		socket, instance, out string
		all, limitSet, outSet bool
		limit                 = 50
		pos                   []string
	)
	for i := 0; i < len(args); i++ {
		a := args[i]
		name, value, joined := a, "", false
		if strings.HasPrefix(a, "--") {
			name, value, joined = strings.Cut(a, "=")
			if !joined {
				name = a
			}
		}
		switch {
		case name == "--socket" || name == "--limit" || name == "-o" || name == "--output":
			if !joined {
				if i+1 >= len(args) {
					return usageErr(stderr, "%s needs a value", name)
				}
				i++
				value = args[i]
			}
			switch name {
			case "--socket":
				socket = value
				continue
			case "-o", "--output":
				out, outSet = value, true
				continue
			}
			n, err := strconv.Atoi(value)
			if err != nil || n < 1 {
				return usageErr(stderr, "--limit needs a positive number")
			}
			limit, limitSet = n, true
		case a == "--all":
			all = true
		case strings.HasPrefix(a, "-") && a != "-":
			return usageErr(stderr, "unknown flag %s", a)
		default:
			pos = append(pos, a)
		}
	}
	if socket == "" {
		if len(pos) == 0 {
			return usageErr(stderr, "view needs an instance name or --socket PATH")
		}
		p, err := client.ViewSocketPath(pos[0])
		if err != nil {
			return usageErr(stderr, "%v", err)
		}
		socket, instance, pos = p, pos[0], pos[1:]
	}
	action, id := "list", ""
	switch {
	case len(pos) == 0, len(pos) == 1 && pos[0] == "list":
	case pos[0] == "show" || pos[0] == "report":
		if len(pos) != 2 {
			return usageErr(stderr, "usage: view INSTANCE|--socket PATH %s ID", pos[0])
		}
		action, id = pos[0], pos[1]
	case len(pos) == 1:
		action, id = "open", pos[0]
	default:
		return usageErr(stderr, "view takes one session: view INSTANCE|--socket PATH [show|report] ID")
	}
	if action != "list" && (all || limitSet) {
		return usageErr(stderr, "--all and --limit go with list")
	}
	if outSet && action != "report" {
		return usageErr(stderr, "-o goes with report")
	}
	if action == "open" {
		return execSoaD([]string{"tui", "--socket", socket, "--view", "--resume", id}, stderr)
	}

	c, err := client.DialSocket(socket, "cli")
	if err != nil {
		msg := fmt.Sprintf("cannot reach the view socket %s: %v", socket, err)
		if errors.Is(err, fs.ErrPermission) {
			msg += " (watching needs membership of the instance's viewerGroup)"
		} else if instance != "" && errors.Is(err, fs.ErrNotExist) {
			msg += " (is " + instance + " running, with a viewerGroup?)"
		}
		return fail(stderr, "%s", msg)
	}
	defer c.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	switch action {
	case "show":
		return viewShow(ctx, c, id, stdout, stderr)
	case "report":
		return writeReport(ctx, c, id, out, stdout, stderr)
	}
	want := []string{"interactive"}
	if all {
		want = []string{"interactive", "oneshot", "cron", "chat", "child"}
	}
	var res struct {
		Sessions []struct {
			ID      string `json:"id"`
			Kind    string `json:"kind"`
			Status  string `json:"status"`
			Title   string `json:"title"`
			Cwd     string `json:"cwd"`
			Updated string `json:"updated"`
		} `json:"sessions"`
	}
	if err := c.Call(ctx, "session.list", map[string]any{"kinds": want, "limit": limit}, &res); err != nil {
		return fail(stderr, "%v", err)
	}
	if len(res.Sessions) == 0 {
		fmt.Fprintln(stdout, "no sessions")
		return 0
	}
	homeDir, _ := os.UserHomeDir()
	for _, s := range res.Sessions {
		title := s.Title
		if title == "" {
			title = "(untitled)"
		}
		fmt.Fprintf(stdout, "%s  %s  %s  %s  %s  %s\n", s.ID, s.Kind, s.Status, localTime(s.Updated, "2006-01-02 15:04"), title, tilde(s.Cwd, homeDir))
	}
	return 0
}

// localTime is an RFC 3339 time in the local zone and layout, or as given
// when it does not parse.
func localTime(s, layout string) string {
	if t, err := time.Parse(time.RFC3339Nano, s); err == nil {
		return t.Local().Format(layout)
	}
	return s
}

// viewShow prints a session's stored transcript, oldest first.
func viewShow(ctx context.Context, c *client.Client, id string, stdout, stderr io.Writer) int {
	msgs, err := history(ctx, c, id)
	if err != nil {
		return fail(stderr, "%v", err)
	}
	var b strings.Builder
	for i, m := range msgs {
		if i > 0 {
			b.WriteString("\n")
		}
		switch m.Role {
		case "tool":
			fmt.Fprintf(&b, "[tool %s]\n", m.ToolName)
		default:
			fmt.Fprintf(&b, "[%s %s]\n", m.Role, localTime(m.TS, "2006-01-02 15:04:05"))
		}
		if text := strings.TrimRight(m.Content, "\n"); text != "" {
			b.WriteString(text + "\n")
		}
		for _, tc := range m.ToolCalls {
			fmt.Fprintf(&b, "-> %s %s\n", tc.Name, tc.Arguments)
		}
	}
	if _, err := io.WriteString(stdout, b.String()); err != nil {
		return 1
	}
	return 0
}
