package tui

import "strings"

// cmdDef is one row of the command table that drives /help, completion and
// the palette (daemon/phases/09c-tui-input.md). A command has one name: the
// owner asked for no aliases in the command surface ("fake news"). ":q"
// stays as its own hard-coded way to quit.
type cmdDef struct {
	name     string
	args     string
	desc     string
	category string
}

var cmdTable = []cmdDef{
	{"new", "", "Open a new session", "Session"},
	{"clear", "", "Open a new session and wipe the screen", "Session"},
	{"resume", "[id or title]", "Go back to a session, yours or one fiona started", "Session"},
	{"retitle", "[new title]", "Retitle this session to what it has become", "Session"},
	{"handoff", "", "Give this session to fiona and leave the TUI", "Session"},
	{"commit", "[extra instructions]", "Look over the uncommitted changes, write a commit message that fits the repo, and commit", "Session"},
	{"compact", "[focus topic | --preview]", "Condense older turns into a summary to make room (--preview only reports what would happen)", "Session"},
	{"stop", "", "Stop every background process", "Session"},
	{"sendnow", "[text]", "Redirect the running turn to the oldest queued message, or to the text given here", "Session"},
	{"sendall", "", "Redirect the running turn to all queued messages together", "Session"},
	{"cancel", "", "Choose queued messages to discard before they go out", "Session"},
	{"status", "", "Report the session, model, mode and usage", "Session"},
	{"model", "[model]", "Change the model for this session", "Configuration"},
	{"reasoning", "[level]", "Change how hard this session's model reasons", "Configuration"},
	{"perm", "[default|ask|lockdown|yolo]", "Change the permission mode", "Configuration"},
	{"help", "", "List the commands", "Info"},
	{"quit", "", "Leave the TUI; asks first if a turn is running", "Exit"},
}

// viewCommands are the commands of a read-only view
// (daemon/phases/12-view-socket.md): none of them changes a session.
var viewCommands = map[string]bool{"resume": true, "status": true, "help": true, "quit": true}

// helpText is /help, from the table; view lists a read-only view's.
func helpText(view bool) string {
	lines := []string{"commands:"}
	for _, c := range cmdTable {
		if view && !viewCommands[c.name] {
			continue
		}
		l := "  /" + c.name
		if c.args != "" {
			l += " " + c.args
		}
		lines = append(lines, l+" — "+c.desc)
	}
	if view {
		lines = append(lines, "  read-only view: ctrl+p runs these · ctrl+t expands a tool · ctrl+x ↓ subagents · ctrl+q quits")
	} else {
		lines = append(lines, "  !cmd runs here · ctrl+p opens the palette · ctrl+t expands a tool · :q quits")
	}
	return strings.Join(lines, "\n")
}

// cmdEntry is one command.
type cmdEntry struct {
	name string // as typed after the slash
	desc string // what a list shows
	cat  string
}

// cmdEntries lists every command in table order; view only a read-only
// view's.
func cmdEntries(view bool) []cmdEntry {
	out := make([]cmdEntry, 0, len(cmdTable))
	for _, c := range cmdTable {
		if view && !viewCommands[c.name] {
			continue
		}
		out = append(out, cmdEntry{c.name, c.desc, c.category})
	}
	return out
}

// canonicalCmd is the command a name stands for, or "".
func canonicalCmd(name string) string {
	for _, e := range cmdEntries(false) {
		if e.name == name {
			return e.name
		}
	}
	return ""
}
