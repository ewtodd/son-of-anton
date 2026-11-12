package tools

import "strings"

// renamed maps each builtin tool's name from before the tools took Claude
// Code's names and parameters (2026-11) to the tools that replaced it and
// what to say to a model that still calls it (a resumed session's history
// shows it the old names).
var renamed = map[string]struct {
	now  []string
	hint string
}{
	"read_file":    {[]string{"Read"}, "read_file is now Read (file_path, offset, limit)"},
	"write_file":   {[]string{"Write"}, "write_file is now Write (file_path, content)"},
	"patch":        {[]string{"Edit"}, "patch is now Edit (file_path, old_string, new_string, replace_all)"},
	"search_files": {[]string{"Grep", "Glob"}, "search_files is now Grep (pattern, path, glob, type, output_mode, -i, -n, -A, -B, -C, multiline, head_limit) for text inside files, and Glob (pattern, path) for file names"},
	"terminal":     {[]string{"Bash"}, "terminal is now Bash (command, timeout in milliseconds, description, run_in_background)"},
	"process":      {[]string{"BashOutput", "KillShell"}, "process is now BashOutput (bash_id, filter) to read a background command's new output, and KillShell (shell_id) to stop it"},
	"todo":         {[]string{"TodoWrite"}, "todo is now TodoWrite (todos: the whole list, each {content, status, activeForm})"},
	"clarify":      {[]string{"AskUserQuestion"}, "clarify is now AskUserQuestion (questions: 1 to 4 of {question, header, options: [{label, description}], multiSelect})"},
	"web_search":   {[]string{"WebSearch"}, "web_search is now WebSearch (query, allowed_domains, blocked_domains)"},
	"web_extract":  {[]string{"WebFetch"}, "web_extract is now WebFetch (url, prompt), one page per call"},
	"task":         {[]string{"Task"}, "task is now Task (description, prompt, subagent_type, model)"},
}

// RenamedTool is the error for a call to name, a tool's name from before the
// renaming, when has holds a tool that replaced it; "" otherwise (an unknown
// tool, or an old one whose replacement this session does not have).
func RenamedTool(name string, has func(string) bool) string {
	r, ok := renamed[name]
	if !ok {
		return ""
	}
	for _, n := range r.now {
		if has(n) {
			return r.hint + ". Call " + strings.Join(r.now, " or ") + " instead."
		}
	}
	return ""
}
