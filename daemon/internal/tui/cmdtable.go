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
	{"new", []string{"reset"}, "", "Open a new session", "Session"},
	{"clear", nil, "", "Open a new session and wipe the screen", "Session"},
	{"resume", []string{"sessions"}, "[id or title]", "Go back to a session, yours or one fiona started", "Session"},
	{"retitle", []string{"rename"}, "", "Retitle this session, named by its own model where the conversation stands", "Session"},
	{"handoff", nil, "", "Give this session to fiona and leave the TUI", "Session"},
	{"commit", nil, "[extra instructions]", "Look over the uncommitted changes, write a commit message that fits the repo, and commit", "Session"},
	{"compact", nil, "[focus topic | --preview]", "Condense older turns into a summary to make room (--preview only reports what would happen)", "Session"},
	{"stop", nil, "", "Stop every background process", "Session"},
	{"sendnow", nil, "[text]", "Redirect the running turn to the oldest queued message, or to the text given here", "Session"},
	{"sendall", nil, "", "Redirect the running turn to all queued messages together", "Session"},
	{"cancel", nil, "", "Choose queued messages to discard before they go out", "Session"},
	{"status", nil, "", "Report the session, model, mode and usage", "Session"},
	{"model", nil, "[model]", "Change the model for this session", "Configuration"},
	{"reasoning", nil, "[level]", "Change how hard this session's model reasons", "Configuration"},
	{"perm", nil, "[default|ask|lockdown|yolo]", "Change the permission mode", "Configuration"},
	{"skin", nil, "[name]", "Show or switch the skin", "Configuration"},
	{"help", nil, "", "List the commands", "Info"},
	{"quit", []string{"exit", "q"}, "", "Leave the TUI; asks first if a turn is running", "Exit"},
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
	lines = append(lines, "  !cmd runs here · ctrl+p opens the palette · ctrl+t expands a tool · :q quits")
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
