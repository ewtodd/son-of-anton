package server

import (
	"encoding/json"
	"strconv"
	"strings"
	"unicode"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

const summaryMax = 120

// summarize is the one-line description of a tool call.
func summarize(tool, args string) string {
	var a map[string]any
	_ = json.Unmarshal([]byte(args), &a)
	s := func(k string) string { v, _ := a[k].(string); return v }
	var out string
	switch tool {
	case "terminal":
		out = s("command")
	case "read_file", "write_file", "patch":
		out = s("path")
	case "search_files":
		out = s("pattern")
	case "process":
		out = strings.TrimSpace(s("action") + " " + s("session_id"))
	case "clarify":
		out = s("question")
	case "todo":
		if items, ok := a["todos"].([]any); ok {
			out = itemCount(len(items))
		}
	case "task":
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

func itemCount(n int) string {
	if n == 1 {
		return "1 item"
	}
	return strconv.Itoa(n) + " items"
}

// isError reports whether a tool result is a JSON object with a non-empty
// error field, or a task result whose state is error.
func isError(tool, result string) bool {
	if tool == "task" && strings.HasPrefix(result, `<task id="`) {
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

func usageObj(u provider.Usage) map[string]any {
	return map[string]any{"prompt_tokens": u.PromptTokens, "completion_tokens": u.CompletionTokens,
		"reasoning_tokens": u.ReasoningTokens, "cached_tokens": u.CachedTokens}
}

// taskLabel is a task call's row: "<Agent> Task — <description>", with
// "(background)" after "Task" for a background one.
func taskLabel(a map[string]any) string {
	typ, _ := a["subagent_type"].(string)
	if typ == "" {
		typ = "general"
	}
	desc, _ := a["description"].(string)
	bg := ""
	if b, _ := a["background"].(bool); b {
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
