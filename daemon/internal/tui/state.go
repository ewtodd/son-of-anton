package tui

import (
	"encoding/json"
	"strings"
	"time"
)

// sessState is the part of the daemon's session object the screen shows.
type sessState struct {
	ID, Cwd, Title, Model, Served, Effort, Mode, Status string
	Started                                             time.Time
	CtxTokens, CtxLen, In, Out, Calls                   int
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
		Status: str(o, "status"), CtxTokens: num(o, "context_tokens"), CtxLen: num(o, "context_length"),
	}
	if u, ok := o["usage"].(map[string]any); ok {
		s.In, s.Out, s.Calls = num(u, "input_tokens"), num(u, "output_tokens"), num(u, "api_calls")
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
	if model == "auto" && served != "" {
		r = "auto → " + short(served)
	}
	if rs := []rune(r); len(rs) > 26 {
		r = string(rs[:23]) + "..."
	}
	return r
}

// message is a stored row as session.attach and session.history return it.
type message struct {
	Seq       int64  `json:"seq"`
	Role      string `json:"role"`
	Content   string `json:"content"`
	Reasoning string `json:"reasoning"`
	ToolCalls []struct {
		ID        string `json:"id"`
		Name      string `json:"name"`
		Arguments string `json:"arguments"`
	} `json:"tool_calls"`
	ToolCallID string `json:"tool_call_id"`
	ToolName   string `json:"tool_name"`
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
	Messages   []message   `json:"messages"`
	MoreBefore bool        `json:"more_before"`
	Gap        bool        `json:"gap"`
	Queue      []queueItem `json:"queue"`
	Role       string      `json:"role"`
}

// toolSummary is one line for a stored tool call (the daemon's summary, for
// history rows).
func toolSummary(name, arguments string) string {
	var args map[string]any
	if json.Unmarshal([]byte(arguments), &args) != nil {
		args = nil
	}
	key := map[string]string{
		"terminal": "command", "read_file": "path", "write_file": "path", "patch": "path",
		"search_files": "pattern", "clarify": "question",
	}[name]
	text := ""
	if key != "" {
		text, _ = args[key].(string)
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
