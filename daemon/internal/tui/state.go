package tui

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"
	"unicode"
)

// sessState is the part of the daemon's session object the screen shows.
type sessState struct {
	ID, Cwd, Title, Model, Served, Effort, Mode, Status, Agent string
	Started, Created                                           time.Time
	CtxTokens, CtxLen, In, Out, Calls, Compactions, Bg         int
}

func str(m map[string]any, k string) string {
	s, _ := m[k].(string)
	return s
}

func num(m map[string]any, k string) int {
	f, _ := m[k].(float64)
	return int(f)
}

func parseSession(o map[string]any, prev sessState) sessState {
	s := sessState{
		ID: str(o, "id"), Cwd: str(o, "cwd"), Title: str(o, "title"), Model: str(o, "model"),
		Served: str(o, "served_model"), Effort: str(o, "reasoning_effort"), Mode: str(o, "mode"),
		Status: str(o, "status"), Agent: str(o, "agent"), CtxTokens: num(o, "context_tokens"), CtxLen: num(o, "context_length"),
		Compactions: num(o, "compactions"), Bg: num(o, "background_processes"),
	}
	if u, ok := o["usage"].(map[string]any); ok {
		s.In, s.Out, s.Calls = num(u, "input_tokens"), num(u, "output_tokens"), num(u, "api_calls")
	}
	if t, err := time.Parse(time.RFC3339, str(o, "created")); err == nil {
		s.Created = t
	}
	if s.Status == "running" {
		if t, err := time.Parse(time.RFC3339, str(o, "turn_started")); err == nil {
			s.Started = t
		} else if prev.ID == s.ID && prev.Status == "running" {
			s.Started = prev.Started
		} else {
			s.Started = time.Now()
		}
	}
	return s
}

// modelShort is the model as the prompt's meta row shows it.
func modelShort(model, served string) string {
	short := func(s string) string {
		if i := strings.LastIndex(s, "/"); i >= 0 {
			s = s[i+1:]
		}
		return strings.TrimSuffix(s, ".gguf")
	}
	r := short(model)
	// A routed model (maitred's auto, fiona) names the one that answered; the
	// daemon reports the model itself until one has.
	if served != "" && served != model {
		r += " → " + short(served)
	}
	if rs := []rune(r); len(rs) > 26 {
		r = string(rs[:23]) + "..."
	}
	return r
}

// modelName is the model in full, and the one that answered when that is
// another (a routed model).
func modelName(model, served string) string {
	if served != "" && served != model {
		return model + " → " + served
	}
	return model
}

type toolCall struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

// isTask says whether a tool is Task, under its name or the one it had before
// the tools took Claude Code's (task, in stored sessions).
func isTask(name string) bool { return name == "Task" || name == "task" }

// isTodo is isTask for TodoWrite (todo before).
func isTodo(name string) bool { return name == "TodoWrite" || name == "todo" }

// callsTask reports whether any of the calls is a Task.
func callsTask(calls []toolCall) bool {
	for _, c := range calls {
		if isTask(c.Name) {
			return true
		}
	}
	return false
}

// message is a stored row as session.attach and session.history return it.
type message struct {
	Seq        int64      `json:"seq"`
	Role       string     `json:"role"`
	Content    string     `json:"content"`
	Reasoning  string     `json:"reasoning"`
	ToolCalls  []toolCall `json:"tool_calls"`
	ToolCallID string     `json:"tool_call_id"`
	ToolName   string     `json:"tool_name"`
}

type queueItem struct {
	ID   string `json:"id"`
	Kind string `json:"kind"`
	Text string `json:"text"`
}

type attachResult struct {
	Session     map[string]any `json:"session"`
	Compactions []struct {
		ThroughSeq int64  `json:"through_seq"`
		Summary    string `json:"summary"`
	} `json:"compactions"`
	Kids       []map[string]any `json:"-"` // session.children, when the rows call task
	HaveKids   bool             `json:"-"`
	Messages   []message        `json:"messages"`
	MoreBefore bool             `json:"more_before"`
	Gap        bool             `json:"gap"`
	Queue      []queueItem      `json:"queue"`
	Role       string           `json:"role"`
}

// toolSummary is one line for a stored tool call (the daemon's summary, for
// history rows).
func toolSummary(name, arguments string) string {
	var args map[string]any
	if json.Unmarshal([]byte(arguments), &args) != nil {
		args = nil
	}
	if isTask(name) {
		return ellipsizeRunes(squash(taskLabel(args)), 120)
	}
	key := map[string]string{
		"Bash": "command", "Read": "file_path", "Write": "file_path", "Edit": "file_path",
		"Glob": "pattern", "Grep": "pattern", "BashOutput": "bash_id", "WebFetch": "url", "WebSearch": "query",
		"terminal": "command", "read_file": "path", "write_file": "path", "patch": "path",
		"search_files": "pattern", "clarify": "question",
	}[name]
	text := ""
	if key != "" {
		text, _ = args[key].(string)
	}
	switch name {
	case "KillShell":
		if id, _ := args["shell_id"].(string); id != "" {
			text = "kill " + id
		}
	case "AskUserQuestion":
		if qs, _ := args["questions"].([]any); len(qs) > 0 {
			q, _ := qs[0].(map[string]any)
			text, _ = q["question"].(string)
			if len(qs) > 1 {
				text += fmt.Sprintf(" (+%d more)", len(qs)-1)
			}
		}
	}
	if text == "" {
		text = arguments
		if text == "" {
			text = name
		}
	}
	return ellipsizeRunes(squash(text), 120)
}

func ellipsizeRunes(s string, n int) string {
	if r := []rune(s); len(r) > n {
		return string(r[:n])
	}
	return s
}

// resultIsError reports whether a stored tool result is a JSON object with a
// non-empty error field.
func resultIsError(content string) bool {
	var o struct {
		Error any `json:"error"`
	}
	if json.Unmarshal([]byte(content), &o) != nil {
		return false
	}
	switch e := o.Error.(type) {
	case nil:
		return false
	case string:
		return e != ""
	}
	return true
}

// titlecase upper-cases each letter that starts a word (opencode's Locale.titlecase).
func titlecase(s string) string {
	var b strings.Builder
	prevWord := false
	for _, r := range s {
		word := unicode.IsLetter(r) || unicode.IsDigit(r) || r == '_'
		if word && !prevWord {
			r = unicode.ToUpper(r)
		}
		prevWord = word
		b.WriteRune(r)
	}
	return b.String()
}

// taskLabel is a Task call's row label, as the daemon's summary has it
// (background was run_in_background's name before the rename).
func taskLabel(args map[string]any) string {
	agent, _ := args["subagent_type"].(string)
	if agent == "" {
		agent = "general-purpose"
	}
	bg := ""
	if b, _ := args["run_in_background"].(bool); b {
		bg = " (background)"
	} else if b, _ := args["background"].(bool); b {
		bg = " (background)"
	}
	desc, _ := args["description"].(string)
	return titlecase(agent) + " Task" + bg + " — " + desc
}

// subagentDuration is opencode's Locale.duration.
func subagentDuration(ms int) string {
	switch {
	case ms < 1000:
		return fmt.Sprintf("%dms", ms)
	case ms < 60_000:
		return fmt.Sprintf("%.1fs", float64(ms)/1000)
	case ms < 3_600_000:
		return fmt.Sprintf("%dm %ds", ms/60_000, ms%60_000/1000)
	case ms < 86_400_000:
		return fmt.Sprintf("%dh %dm", ms/3_600_000, ms%3_600_000/60_000)
	}
	return fmt.Sprintf("%dd %dh", ms/86_400_000, ms%86_400_000/3_600_000)
}

// doneDetail is the line under a finished task row.
func doneDetail(toolCalls, ms int) string {
	d := subagentDuration(ms)
	switch toolCalls {
	case 0:
		return "↳ " + d
	case 1:
		return "↳ 1 toolcall · " + d
	}
	return fmt.Sprintf("↳ %d toolcalls · %s", toolCalls, d)
}

var (
	taskResultRE = regexp.MustCompile(`^<task id="([^"]+)" state="([^"]+)"`)
	childTitleRE = regexp.MustCompile(`^(.*) \(@(.+) subagent\)$`)
)

// kid is a child session as session.children and child.created report it.
type kid struct {
	ID, Title, Agent, Status string
	ToolCalls, DurationMS    int
}

func parseKid(o map[string]any) kid {
	return kid{ID: str(o, "id"), Title: str(o, "title"), Agent: str(o, "agent"), Status: str(o, "status"),
		ToolCalls: num(o, "tool_calls"), DurationMS: num(o, "duration_ms")}
}

// taskTitle is "<Agent> Task — <description>" for a child, read from its title.
func (k kid) taskTitle() string {
	if f := childTitleRE.FindStringSubmatch(k.Title); f != nil {
		return titlecase(f[2]) + " Task — " + f[1]
	}
	return "Subagent"
}

// taskRes is a task result found in the stored rows.
type taskRes struct{ callID, childID, state string }
