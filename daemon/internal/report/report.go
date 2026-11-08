// Package report renders a session's stored transcript as a PDF
// (daemon/phases/13-reports.md). Build turns the session into the data the
// template reads; typst, with the cmarker package for the markdown, lays it
// out. No model is involved.
package report

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
)

//go:embed report.typ
var template []byte

// Session is what a report is made of: a session, its messages and its
// subagents. The command fills it from the socket, the daemon from the store.
type Session struct {
	ID, Kind, Title, Cwd, Model string
	Agent                       string // a subagent's type
	Created, Updated            time.Time
	InputTokens, OutputTokens   int
	Messages                    []Message
	Children                    []Session
}

// Message is a stored message, in the shape session.history gives it.
type Message struct {
	Seq        int64      `json:"seq"`
	Role       string     `json:"role"`
	Content    string     `json:"content"`
	Reasoning  string     `json:"reasoning"`
	ToolCallID string     `json:"tool_call_id"`
	ToolName   string     `json:"tool_name"`
	TS         string     `json:"ts"` // RFC 3339
	ToolCalls  []ToolCall `json:"tool_calls"`
	Images     []Image    `json:"images"`
}

// ToolCall is one tool call of an assistant message.
type ToolCall struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

// Image is an image attached to a message.
type Image struct {
	ID   string `json:"id"`
	Mime string `json:"mime"`
	Name string `json:"name"`
}

// Doc is the data the template reads.
type Doc struct {
	Title   string      `json:"title"`
	ID      string      `json:"id"`
	Kind    string      `json:"kind"`
	Cwd     string      `json:"cwd"`
	Facts   [][2]string `json:"facts"`
	Entries []Entry     `json:"entries"`
}

// Entry is one block of the report: a user request, an assistant reply, a
// note, a tool call, or a subagent with its own entries.
type Entry struct {
	Type    string  `json:"type"`
	TS      string  `json:"ts"`
	Text    string  `json:"text"`
	Name    string  `json:"name"`
	Summary string  `json:"summary"`
	Output  string  `json:"output"`
	More    int     `json:"more"` // output lines left out
	Error   bool    `json:"error"`
	Title   string  `json:"title"`
	Agent   string  `json:"agent"`
	Entries []Entry `json:"entries"`
}

const (
	outputLines = 12
	outputRunes = 1500
	stampLayout = "2006-01-02 15:04"
)

var (
	taskResultRE = regexp.MustCompile(`^<task id="([^"]+)"`)
	childTitleRE = regexp.MustCompile(`^(.*) \(@.+ subagent\)$`)
	ansiRE       = regexp.MustCompile(`\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[@-_]`)
)

// quiet are the tools whose result the report leaves out: the call's line
// says what it did, and the result is a file, an edit or another session's
// transcript (a task's subagent follows its call).
var quiet = map[string]bool{"read_file": true, "write_file": true, "patch": true, "todo": true, "memory": true, "task": true}

// Build is the report's data for s, its times in loc.
func Build(s Session, loc *time.Location) Doc {
	title := s.Title
	if title == "" {
		title = "(untitled)"
	}
	d := Doc{Title: title, ID: s.ID, Kind: s.Kind, Cwd: s.Cwd, Facts: [][2]string{}}
	fact := func(k, v string) { d.Facts = append(d.Facts, [2]string{k, v}) }
	if s.Model != "" {
		fact("Model", s.Model)
	}
	if !s.Created.IsZero() {
		fact("Started", s.Created.In(loc).Format(stampLayout))
	}
	if !s.Updated.IsZero() {
		fact("Last activity", s.Updated.In(loc).Format(stampLayout))
	}
	requests, calls := 0, 0
	for _, m := range s.Messages {
		if m.Role == "user" && !isNote(m.Content) {
			requests++
		}
		calls += len(m.ToolCalls)
	}
	fact("Requests", strconv.Itoa(requests))
	fact("Tool calls", strconv.Itoa(calls))
	if s.InputTokens+s.OutputTokens > 0 {
		fact("Tokens", commas(s.InputTokens)+" in · "+commas(s.OutputTokens)+" out")
	}
	d.Entries = entries(s, loc)
	return d
}

func entries(s Session, loc *time.Location) []Entry {
	results := map[string]Message{}
	for _, m := range s.Messages {
		if m.Role == "tool" {
			results[m.ToolCallID] = m
		}
	}
	children := map[string]Session{}
	for _, c := range s.Children {
		children[c.ID] = c
	}
	claimed := map[string]bool{}
	var out []Entry
	for _, m := range s.Messages {
		text := strings.TrimSpace(m.Content)
		switch m.Role {
		case "user":
			if isNote(text) {
				out = append(out, Entry{Type: "note", Text: text})
			} else {
				out = append(out, Entry{Type: "user", TS: stamp(m.TS, loc), Text: withImages(text, len(m.Images))})
			}
		case "assistant":
			switch {
			case text == "[response interrupted]" || text == "[response failed]":
				out = append(out, Entry{Type: "note", Text: text})
			case text != "" || len(m.Images) > 0:
				out = append(out, Entry{Type: "assistant", TS: stamp(m.TS, loc), Text: withImages(text, len(m.Images))})
			}
			for _, c := range m.ToolCalls {
				r, ok := results[c.ID]
				e := Entry{Type: "tool", Name: c.Name, Summary: Summary(c.Name, c.Arguments)}
				if ok {
					e.Error = IsError(c.Name, r.Content)
					e.Output, e.More = clip(output(c.Name, r.Content))
				}
				out = append(out, e)
				if f := taskResultRE.FindStringSubmatch(r.Content); c.Name == "task" && f != nil {
					if child, ok := children[f[1]]; ok && !claimed[f[1]] {
						claimed[f[1]] = true
						out = append(out, subagent(child, loc))
					}
				}
			}
		}
	}
	for _, c := range s.Children {
		if !claimed[c.ID] {
			out = append(out, subagent(c, loc))
		}
	}
	return out
}

func subagent(c Session, loc *time.Location) Entry {
	title := c.Title
	if f := childTitleRE.FindStringSubmatch(title); f != nil {
		title = f[1]
	}
	es := entries(c, loc)
	if es == nil {
		es = []Entry{}
	}
	return Entry{Type: "subagent", Title: title, Agent: c.Agent, Entries: es}
}

// isNote says a user message is the daemon's, not the user's: a background
// receipt.
func isNote(text string) bool {
	return strings.HasPrefix(text, "[background receipt]") || strings.HasPrefix(text, "[Background process")
}

func withImages(text string, n int) string {
	if n == 0 {
		return text
	}
	note := "[1 image]"
	if n > 1 {
		note = "[" + strconv.Itoa(n) + " images]"
	}
	if text == "" {
		return note
	}
	return text + "\n\n" + note
}

func stamp(ts string, loc *time.Location) string {
	t, err := time.Parse(time.RFC3339Nano, ts)
	if err != nil {
		return ""
	}
	return t.In(loc).Format(stampLayout)
}

// output is what a tool's result shows: its error, else its output (with a
// non-zero exit code first), nothing for a quiet tool, else the result.
func output(tool, result string) string {
	var r struct {
		Error    any     `json:"error"`
		Output   *string `json:"output"`
		ExitCode *int    `json:"exit_code"`
	}
	if json.Unmarshal([]byte(result), &r) == nil {
		if e, ok := r.Error.(string); ok && e != "" {
			return e
		}
		if r.Output != nil {
			out := strings.TrimRight(*r.Output, "\n")
			if r.ExitCode != nil && *r.ExitCode != 0 {
				return strings.TrimRight("exit "+strconv.Itoa(*r.ExitCode)+"\n"+out, "\n")
			}
			return out
		}
	}
	if quiet[tool] {
		return ""
	}
	return strings.TrimRight(result, "\n")
}

// clip cleans text for print and cuts it to outputLines lines and
// outputRunes characters; more is the count of lines left out.
func clip(text string) (string, int) {
	text = clean(text)
	if text == "" {
		return "", 0
	}
	lines := strings.Split(text, "\n")
	keep := lines[:min(len(lines), outputLines)]
	out := strings.Join(keep, "\n")
	cut := false
	if r := []rune(out); len(r) > outputRunes {
		out, cut = string(r[:outputRunes]), true
	}
	kept := strings.Count(out, "\n") + 1
	more := len(lines) - kept
	if cut && more == 0 {
		out += "…"
	}
	return out, more
}

// clean removes terminal escapes and control characters but newline and tab.
func clean(s string) string {
	s = ansiRE.ReplaceAllString(s, "")
	s = strings.ReplaceAll(s, "\r\n", "\n")
	return strings.Map(func(r rune) rune {
		if r == '\n' || r == '\t' || !unicode.IsControl(r) {
			return r
		}
		return -1
	}, s)
}

func commas(n int) string {
	s := strconv.Itoa(n)
	neg := strings.HasPrefix(s, "-")
	s = strings.TrimPrefix(s, "-")
	for i := len(s) - 3; i > 0; i -= 3 {
		s = s[:i] + "," + s[i:]
	}
	if neg {
		s = "-" + s
	}
	return s
}

// FileName is a report's file name: its title as a slug, else its id.
func FileName(s Session) string {
	var b strings.Builder
	dash := false
	for _, r := range strings.ToLower(s.Title) {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			if dash && b.Len() > 0 {
				b.WriteByte('-')
			}
			b.WriteRune(r)
			dash = false
		} else {
			dash = true
		}
	}
	slug := []rune(b.String())
	if len(slug) > 60 {
		slug = []rune(strings.TrimRight(string(slug[:60]), "-"))
	}
	if len(slug) == 0 {
		return s.ID + ".pdf"
	}
	return string(slug) + ".pdf"
}

// Render is the session's report as a PDF, its times in loc. It runs typst
// ($SON_OF_ANTON_TYPST, else typst on PATH) with the cmarker package.
func Render(ctx context.Context, s Session, loc *time.Location) ([]byte, error) {
	typst := os.Getenv("SON_OF_ANTON_TYPST")
	if typst == "" {
		typst = "typst"
	}
	bin, err := exec.LookPath(typst)
	if err != nil {
		return nil, fmt.Errorf("a report needs typst with the cmarker package, which the son-of-anton package carries: %w", err)
	}
	data, err := json.Marshal(Build(s, loc))
	if err != nil {
		return nil, err
	}
	dir, err := os.MkdirTemp("", "soa-report")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(dir)
	for name, b := range map[string][]byte{"report.typ": template, "report.json": data} {
		if err := os.WriteFile(filepath.Join(dir, name), b, 0o600); err != nil {
			return nil, err
		}
	}
	ctx, cancel := context.WithTimeout(ctx, time.Minute)
	defer cancel()
	pdf := filepath.Join(dir, "report.pdf")
	cmd := exec.CommandContext(ctx, bin, "compile", "--ignore-system-fonts", "--root", dir,
		filepath.Join(dir, "report.typ"), pdf)
	if out, err := cmd.CombinedOutput(); err != nil {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return nil, errors.New("typst took longer than a minute")
		}
		return nil, fmt.Errorf("typst (it needs the cmarker package, which the son-of-anton package carries): %v: %s", err, strings.TrimSpace(string(out)))
	}
	return os.ReadFile(pdf)
}
