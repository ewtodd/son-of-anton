package tui

import "strings"

// cmdDef is one row of the command table that drives /help, completion and
// the palette (daemon/phases/09c-tui-input.md).
type cmdDef struct {
	name     string
	aliases  []string
	args     string
	desc     string
	category string
}

var cmdTable = []cmdDef{
	{"new", []string{"reset"}, "", "Start a new session", "Session"},
	{"clear", nil, "", "Start a new session and clear the screen", "Session"},
	{"resume", []string{"sessions"}, "[id]", "Resume a session", "Session"},
	{"pickup", nil, "[id or title]", "Take over a worker that fiona started", "Session"},
	{"commit", nil, "[extra instructions]", "Review uncommitted changes, write a message in the repo's style, and commit", "Session"},
	{"compact", nil, "[focus topic | --preview]", "Summarize older turns to free context (--preview shows what it would do)", "Session"},
	{"stop", nil, "", "Kill all running background processes", "Session"},
	{"sendnow", nil, "[text]", "Steer the running turn onto the oldest queued message, or onto the text after it", "Session"},
	{"sendall", nil, "", "Steer the running turn onto every queued message at once", "Session"},
	{"cancel", nil, "", "Pick queued messages to drop before they are sent", "Session"},
	{"status", nil, "", "Show the session, model, mode and usage", "Session"},
	{"model", nil, "[model]", "Switch this session's model", "Configuration"},
	{"reasoning", nil, "[level]", "Set this session's reasoning effort", "Configuration"},
	{"perm", nil, "[default|ask|lockdown|yolo]", "Set the permission mode", "Configuration"},
	{"skin", nil, "[name]", "Show or change the skin", "Configuration"},
	{"help", nil, "", "Show the commands", "Info"},
	{"quit", []string{"exit", "q"}, "", "Quit; the session keeps running in soa-d", "Exit"},
}

// helpText is /help, from the table.
func helpText() string {
	lines := []string{"commands:"}
	for _, c := range cmdTable {
		l := "  /" + c.name
		if c.args != "" {
			l += " " + c.args
		}
		lines = append(lines, l+" — "+c.desc)
	}
	lines = append(lines, "  !cmd runs a shell command here · ctrl+p opens the palette · :q quits")
	return strings.Join(lines, "\n")
}

// cmdEntry is a command or one of its aliases.
type cmdEntry struct {
	name string // as typed after the slash
	desc string // what a list shows
	cat  string
	of   string // the command an alias stands for
}

// cmdEntries lists every command with its aliases after it, in table order.
func cmdEntries() []cmdEntry {
	var out []cmdEntry
	for _, c := range cmdTable {
		out = append(out, cmdEntry{c.name, c.desc, c.category, ""})
		for _, a := range c.aliases {
			out = append(out, cmdEntry{a, "alias for /" + c.name, c.category, c.name})
		}
	}
	return out
}

// canonicalCmd is the command a name or alias stands for, or "".
func canonicalCmd(name string) string {
	for _, e := range cmdEntries() {
		if e.name == name {
			if e.of != "" {
				return e.of
			}
			return e.name
		}
	}
	return ""
}
