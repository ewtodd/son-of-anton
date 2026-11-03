package lsp

import (
	"fmt"
	"strconv"
	"strings"
	"unicode"
)

const (
	maxPerFile  = 20
	maxTotal    = 4000
	maxMessage  = 300
	maxCode     = 80
	maxSource   = 80
	errorSev    = 1
	truncMarker = "\n…[truncated]"
)

// Diagnostic is the part of an LSP diagnostic the report uses.
type Diagnostic struct {
	Range struct {
		Start struct {
			Line      int `json:"line"`
			Character int `json:"character"`
		} `json:"start"`
	} `json:"range"`
	Severity int    `json:"severity"`
	Code     any    `json:"code"`
	Source   string `json:"source"`
	Message  string `json:"message"`
}

// key is what tells one diagnostic from another when the edit moved it.
type key struct {
	severity              int
	code, source, message string
}

func (d Diagnostic) key() key {
	return key{d.severity(), codeText(d.Code), d.Source, d.Message}
}

// severity treats a missing severity as an error, as the Python does.
func (d Diagnostic) severity() int {
	if d.Severity == 0 {
		return errorSev
	}
	return d.Severity
}

func codeText(v any) string {
	switch x := v.(type) {
	case nil:
		return ""
	case float64:
		return strconv.FormatFloat(x, 'f', -1, 64)
	case string:
		return x
	default:
		return fmt.Sprint(x)
	}
}

// newErrors returns the errors in after beyond the ones before already had
// for the same key, in after's order. An error that only moved is not new;
// one more of the same is.
func newErrors(before, after []Diagnostic) []Diagnostic {
	seen := map[key]int{}
	for _, d := range before {
		if d.severity() == errorSev {
			seen[d.key()]++
		}
	}
	var out []Diagnostic
	for _, d := range after {
		if d.severity() != errorSev {
			continue
		}
		k := d.key()
		if seen[k] > 0 {
			seen[k]--
			continue
		}
		out = append(out, d)
	}
	return out
}

// sanitize makes a language server's text safe to put in a tool result:
// one line, no control characters, cut to limit characters, and with
// < > & escaped so it cannot close the block or open a tag.
func sanitize(s string, limit int) string {
	s = strings.NewReplacer("\r", " ", "\n", " ").Replace(s)
	s = strings.Map(func(r rune) rune {
		if r == ' ' || unicode.IsPrint(r) {
			return r
		}
		return -1
	}, s)
	s = strings.TrimSpace(s)
	if r := []rune(s); len(r) > limit {
		s = string(r[:limit])
	}
	return strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;").Replace(s)
}

func formatDiagnostic(d Diagnostic) string {
	out := fmt.Sprintf("ERROR [%d:%d] %s", d.Range.Start.Line+1, d.Range.Start.Character+1, sanitize(d.Message, maxMessage))
	if code := sanitize(codeText(d.Code), maxCode); code != "" {
		out += " [" + code + "]"
	}
	if src := sanitize(d.Source, maxSource); src != "" {
		out += " (" + src + ")"
	}
	return out
}

// Report formats errors as the block the model reads, or "" for none.
func Report(path string, errs []Diagnostic) string {
	if len(errs) == 0 {
		return ""
	}
	shown := errs[:min(len(errs), maxPerFile)]
	lines := make([]string, len(shown))
	for i, d := range shown {
		lines[i] = formatDiagnostic(d)
	}
	body := strings.Join(lines, "\n")
	if extra := len(errs) - len(shown); extra > 0 {
		body += fmt.Sprintf("\n... and %d more", extra)
	}
	safe := strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", `"`, "&quot;", "'", "&#x27;").Replace(path)
	out := "LSP diagnostics introduced by this edit:\n<diagnostics file=\"" + safe + "\">\n" + body + "\n</diagnostics>"
	if r := []rune(out); len(r) > maxTotal {
		out = string(r[:maxTotal-len([]rune(truncMarker))]) + truncMarker
	}
	return out
}
