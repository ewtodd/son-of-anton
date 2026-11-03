package physics

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"
)

// Limits are a run's budgets.
type Limits struct {
	MaxSessions   int     `json:"max_sessions"`
	SessionTokens int     `json:"session_tokens"`
	TotalTokens   int     `json:"total_tokens"`
	Hours         float64 `json:"hours"`
}

// EventLog appends the run's EVENT_LOG.jsonl.
type EventLog struct {
	mu   sync.Mutex
	path string
}

// NewEventLog logs to path.
func NewEventLog(path string) *EventLog { return &EventLog{path: path} }

// Add appends one line; fields are the line's other keys.
func (l *EventLog) Add(kind string, fields map[string]any) {
	m := map[string]any{"kind": kind, "ts": time.Now().UTC().Format(time.RFC3339Nano)}
	for k, v := range fields {
		m[k] = v
	}
	b, err := json.Marshal(m)
	if err != nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	f, err := os.OpenFile(l.path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = f.Write(append(b, '\n'))
}

// Call is one tool call of a work session, as logs/session-N.md lists it.
type Call struct {
	Session  int     `json:"session"`
	Tool     string  `json:"tool"`
	Summary  string  `json:"summary"` // the command, or the tool's main argument
	ExitCode *int    `json:"exit_code,omitempty"`
	Duration float64 `json:"duration_s"`
	Output   string  `json:"output"`
}

const (
	logHead     = 30
	logTail     = 15
	logMaxChars = 4000
	logMaxCmd   = 2000
)

// Clip keeps the first 30 and last 15 lines of out, and at most 4,000
// characters.
func Clip(out string) string {
	lines := strings.Split(strings.TrimRight(out, "\n"), "\n")
	if len(lines) > logHead+logTail {
		omitted := len(lines) - logHead - logTail
		kept := append(append([]string{}, lines[:logHead]...), fmt.Sprintf("... [%d lines omitted] ...", omitted))
		lines = append(kept, lines[len(lines)-logTail:]...)
	}
	s := strings.Join(lines, "\n")
	if r := []rune(s); len(r) > logMaxChars {
		half := logMaxChars / 2
		s = string(r[:half]) + "\n... [clipped] ...\n" + string(r[len(r)-half:])
	}
	return s
}

// SessionLog is logs/session-N.md: every tool call of the work session.
func SessionLog(n int, calls []Call) string {
	var b strings.Builder
	fmt.Fprintf(&b, "# Work session %d\n\n%d tool calls.\n", n, len(calls))
	for i, c := range calls {
		sum := c.Summary
		if r := []rune(sum); len(r) > logMaxCmd {
			sum = string(r[:logMaxCmd]) + "..."
		}
		fmt.Fprintf(&b, "\n## %d. %s\n\n", i+1, c.Tool)
		fmt.Fprintf(&b, "```\n%s\n```\n\n", sum)
		if c.ExitCode != nil {
			fmt.Fprintf(&b, "exit code %d, ", *c.ExitCode)
		}
		fmt.Fprintf(&b, "%.1f s\n", c.Duration)
		if strings.TrimSpace(c.Output) != "" {
			fmt.Fprintf(&b, "\n```\n%s\n```\n", Clip(c.Output))
		}
	}
	return b.String()
}
