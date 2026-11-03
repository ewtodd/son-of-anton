// Package foreign reads other coding agents' sessions (Claude Code, Codex
// CLI, opencode) straight from their own stores, read-only. It is a port of
// the Python's son_of_anton_cli/foreign_sessions.py: the same parsers, so the
// same turns, titles and usage.
package foreign

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// Turn is one conversation turn. Timestamp is epoch seconds, nil when the
// store gave none.
type Turn struct {
	Role, Content string
	Timestamp     *float64
}

// Usage is what a store knows of a session's cost. LastPrompt is the context
// size at the final call; every field is zero when the store carried nothing.
type Usage struct {
	Input, Output, CacheRead, CacheWrite, Reasoning, Calls, LastPrompt int
}

// Session is one parsed foreign session. Title is the title guess: the
// store's own title or summary, else the first user line.
type Session struct {
	Cwd, Title, ID string
	Turns          []Turn
	Usage          Usage
}

const titleMax = 60

// wrapperTag matches user-message texts that are injected context wrappers,
// not typed input.
var wrapperTag = regexp.MustCompile(`(?i)^<(?:user_instructions|environment_context|recommended_plugins|` +
	`skills_instructions|permissions[_-]instructions|turn_context|` +
	`command-name|command-message|command-args|local-command-stdout|` +
	`local-command-caveat|ide_opened_file|ide_selection|` +
	`bash-input|bash-stdout|bash-stderr|system-reminder)\b`)

func isWrapperText(text string) bool {
	return wrapperTag.MatchString(strings.TrimLeftFunc(text, isSpace))
}

// isSpace is Python's str.isspace for the characters that matter here.
func isSpace(r rune) bool {
	return strings.ContainsRune(" \t\n\r\v\f\x1c\x1d\x1e\x1f\x85      　", r) ||
		(r >= ' ' && r <= ' ')
}

func strip(s string) string { return strings.TrimFunc(s, isSpace) }

// firstLine is the first line of s as Python's splitlines sees lines.
func firstLine(s string) string {
	for i, r := range s {
		switch r {
		case '\n', '\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', ' ', ' ':
			return s[:i]
		}
	}
	return s
}

// truncRunes cuts s to n characters.
func truncRunes(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n])
}

// truthy is Python's truth value of a decoded JSON value.
func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case float64:
		return x != 0
	case string:
		return x != ""
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}

// orName is `value or fallback` for a name field, as the f-string shows it.
func orName(v any, fallback string) string {
	if !truthy(v) {
		return fallback
	}
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprint(v)
}

func str(m map[string]any, key string) (string, bool) {
	s, ok := m[key].(string)
	return s, ok
}

func dict(v any) (map[string]any, bool) {
	m, ok := v.(map[string]any)
	return m, ok
}

// readJSONLines decodes the JSON objects of a JSONL file, skipping blank and
// unparseable lines and lines that are not objects. An unreadable file is
// empty, as in the Python.
func readJSONLines(path string) []map[string]any {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	var out []map[string]any
	r := bufio.NewReaderSize(f, 1<<16)
	for {
		line, err := r.ReadBytes('\n')
		if s := strings.TrimSpace(string(line)); s != "" {
			var obj map[string]any
			if json.Unmarshal([]byte(s), &obj) == nil && obj != nil {
				out = append(out, obj)
			}
		}
		if err != nil {
			if !errors.Is(err, io.EOF) {
				return out
			}
			return out
		}
	}
}

// flattenBlocks flattens a message content (a string or a block list) to
// plain text. Tool activity becomes a bracketed summary; unknown blocks are
// skipped.
func flattenBlocks(content any) string {
	if s, ok := content.(string); ok {
		return s
	}
	list, ok := content.([]any)
	if !ok {
		return ""
	}
	var parts []string
	for _, b := range list {
		block, ok := dict(b)
		if !ok {
			if s, ok := b.(string); ok {
				parts = append(parts, s)
			}
			continue
		}
		switch block["type"] {
		case "text", "input_text", "output_text":
			if s, ok := str(block, "text"); ok && s != "" {
				parts = append(parts, s)
			}
		case "tool_use":
			parts = append(parts, "[ran tool: "+orName(block["name"], "tool")+"]")
		case "image":
			parts = append(parts, "[image]")
		}
	}
	var kept []string
	for _, p := range parts {
		if p = strip(p); p != "" {
			kept = append(kept, p)
		}
	}
	return strings.Join(kept, "\n\n")
}

// isoLayouts are the forms of an ISO-8601 time a store writes; a time with no
// zone is local.
var isoLayouts = []string{
	"2006-01-02T15:04:05.999999999Z07:00",
	"2006-01-02 15:04:05.999999999Z07:00",
	"2006-01-02T15:04:05.999999999Z0700",
	"2006-01-02T15:04:05.999999999",
	"2006-01-02 15:04:05.999999999",
	"2006-01-02T15:04Z07:00",
	"2006-01-02T15:04",
	"2006-01-02",
}

// coerceTimestamp is epoch seconds from a store's timestamp: an ISO-8601
// string, epoch seconds or epoch milliseconds. nil when absent or unreadable.
func coerceTimestamp(v any) *float64 {
	var secs float64
	switch x := v.(type) {
	case nil:
		return nil
	case bool:
		if x {
			secs = 1
		}
		return &secs
	case float64:
		secs = x
	case int64:
		secs = float64(x)
	case []byte:
		return coerceTimestamp(string(x))
	case string:
		text := strip(x)
		if text == "" {
			return nil
		}
		if f, err := strconv.ParseFloat(text, 64); err == nil {
			return &f
		}
		text = strings.Replace(text, "Z", "+00:00", 1)
		for _, layout := range isoLayouts {
			if t, err := time.ParseInLocation(layout, text, time.Local); err == nil {
				f := float64(t.Unix()*1_000_000+int64(t.Nanosecond()/1000)) / 1e6
				return &f
			}
		}
		return nil
	default:
		return nil
	}
	if secs > 1_000_000_000_000 { // milliseconds
		secs /= 1000
	}
	return &secs
}

// coerceInt is a JSON or SQLite numeric field as an int; ok is false when
// the field is absent or not a number.
func coerceInt(v any) (int, bool) {
	switch x := v.(type) {
	case float64:
		if math.IsNaN(x) || math.IsInf(x, 0) {
			return 0, false
		}
		return int(x), true
	case int64:
		return int(x), true
	case string:
		n, err := strconv.Atoi(strip(x))
		return n, err == nil
	case []byte:
		return coerceInt(string(x))
	}
	return 0, false
}

func intOr0(v any) int { n, _ := coerceInt(v); return n }

// mergeTurns merges consecutive same-role turns and guarantees the first is a
// user turn: a leading assistant reply gets a stub user turn, the only place
// one is ever made. A merged turn keeps the latest timestamp it swallowed.
func mergeTurns(raw []rawTurn) []Turn {
	var merged []Turn
	for _, t := range raw {
		text := strip(t.text)
		if text == "" {
			continue
		}
		if n := len(merged); n > 0 && merged[n-1].Role == t.role {
			merged[n-1].Content += "\n\n" + text
			if t.ts != nil {
				merged[n-1].Timestamp = t.ts
			}
		} else {
			merged = append(merged, Turn{Role: t.role, Content: text, Timestamp: t.ts})
		}
	}
	if len(merged) > 0 && merged[0].Role == "assistant" {
		stub := Turn{Role: "user", Content: "(imported conversation begins with an assistant reply)"}
		merged = append([]Turn{stub}, merged...)
	}
	return merged
}

type rawTurn struct {
	role, text string
	ts         *float64
}

// firstUserLine is the first non-empty line of the first user turn, cut to
// twice the title length.
func firstUserLine(turns []rawTurn) string {
	for _, t := range turns {
		if t.role != "user" {
			continue
		}
		if line := strip(firstLine(strip(t.text))); line != "" {
			return truncRunes(line, titleMax*2)
		}
	}
	return ""
}

// usageOf clamps the totals at zero and drops a last-prompt size that is not
// positive; a store that carried nothing gives the zero Usage.
func usageOf(u Usage) Usage {
	u.Input, u.Output = max(0, u.Input), max(0, u.Output)
	u.CacheRead, u.CacheWrite = max(0, u.CacheRead), max(0, u.CacheWrite)
	u.Reasoning, u.Calls = max(0, u.Reasoning), max(0, u.Calls)
	if u.LastPrompt < 0 {
		u.LastPrompt = 0
	}
	return u
}

// partsCompare orders paths as Python orders Paths: part by part.
func sortPaths(paths []string) {
	sort.Slice(paths, func(i, j int) bool {
		a, b := strings.Split(paths[i], string(os.PathSeparator)), strings.Split(paths[j], string(os.PathSeparator))
		for k := 0; k < len(a) && k < len(b); k++ {
			if a[k] != b[k] {
				return a[k] < b[k]
			}
		}
		return len(a) < len(b)
	})
}
