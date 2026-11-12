package report

import (
	"encoding/json"
	"strconv"
	"strings"
	"unicode"
)

const summaryMax = 120

// Summary is the one-line description of a tool call: what the daemon's
// tool events show, and a report's tool lines. A stored call may name a tool
// by its name from before the tools took Claude Code's (read_file, terminal,
// ...), with that tool's arguments.
func Summary(tool, args string) string {
	var a map[string]any
	_ = json.Unmarshal([]byte(args), &a)
	s := func(k string) string { v, _ := a[k].(string); return v }
	var out string
	switch tool {
	case "Bash", "terminal":
		out = s("command")
	case "Read", "Write", "Edit":
		out = s("file_path")
	case "read_file", "write_file", "patch":
		out = s("path")
	case "Glob", "Grep", "search_files":
		out = s("pattern")
	case "BashOutput":
		out = s("bash_id")
	case "KillShell":
		out = "kill " + s("shell_id")
	case "process":
		out = strings.TrimSpace(s("action") + " " + s("session_id"))
	case "AskUserQuestion":
		if qs, ok := a["questions"].([]any); ok && len(qs) > 0 {
			q, _ := qs[0].(map[string]any)
			out, _ = q["question"].(string)
			if len(qs) > 1 {
				out += " (+" + strconv.Itoa(len(qs)-1) + " more)"
			}
		}
	case "clarify":
		out = s("question")
	case "WebFetch":
		out = s("url")
	case "WebSearch":
		out = s("query")
	case "TodoWrite", "todo":
		if items, ok := a["todos"].([]any); ok {
			out = itemCount(len(items))
		}
	case "Task", "task":
		out = taskLabel(a)
	}
	if out == "" {
		out = args
	}
	r := []rune(strings.Join(strings.Fields(out), " "))
	if len(r) > summaryMax {
		r = r[:summaryMax]
	}
	return string(r)
}

// isTask says whether tool is the Task tool, under its name or the one it
// had before (task).
func isTask(tool string) bool { return tool == "Task" || tool == "task" }

func itemCount(n int) string {
	if n == 1 {
		return "1 item"
	}
	return strconv.Itoa(n) + " items"
}

// IsError reports whether a tool result is a JSON object with a non-empty
// error field, or a task result whose state is error.
func IsError(tool, result string) bool {
	if isTask(tool) && strings.HasPrefix(result, `<task id="`) {
		if line, _, _ := strings.Cut(result, "\n"); strings.Contains(line, `state="error"`) {
			return true
		}
	}
	var r struct {
		Error any `json:"error"`
	}
	if json.Unmarshal([]byte(result), &r) != nil {
		return false
	}
	switch e := r.Error.(type) {
	case nil:
		return false
	case string:
		return e != ""
	default:
		return true
	}
}

// taskLabel is a Task call's row: "<Agent> Task — <description>", with
// "(background)" after "Task" for a background one (run_in_background, or
// the background of a call stored before the rename).
func taskLabel(a map[string]any) string {
	typ, _ := a["subagent_type"].(string)
	if typ == "" {
		typ = "general-purpose"
	}
	desc, _ := a["description"].(string)
	bg := ""
	if b, _ := a["run_in_background"].(bool); b {
		bg = " (background)"
	} else if b, _ := a["background"].(bool); b {
		bg = " (background)"
	}
	return titlecase(typ) + " Task" + bg + " — " + desc
}

// titlecase upper-cases each letter that starts a word (opencode's titlecase).
func titlecase(s string) string {
	r := []rune(s)
	start := true
	for i, c := range r {
		if start {
			r[i] = unicode.ToUpper(c)
		}
		start = !(c == '_' || unicode.IsLetter(c) || unicode.IsDigit(c))
	}
	return string(r)
}
