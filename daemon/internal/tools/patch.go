package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

type patchTool struct{}

func (patchTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "patch",
		Description: "Change part of an existing file by swapping one piece of text for another. " +
			"Give enough of the surrounding text in old_string that it occurs only once, or set replace_all to change every occurrence. " +
			"Differences in indentation or runs of spaces do not stop a match. " +
			"The reply is a unified diff of what changed.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "path": {"type": "string", "description": "The file to modify."},
    "old_string": {"type": "string", "description": "The text to be replaced. It must occur exactly once in the file, unless replace_all is true."},
    "new_string": {"type": "string", "description": "What to put in its place. It cannot equal old_string."},
    "replace_all": {"type": "boolean", "description": "Change every occurrence rather than insisting on a single one. Defaults to false.", "default": false}
  },
  "required": ["path", "old_string", "new_string"]
}`),
	}}
}

type patchArgs struct {
	Path       string `json:"path"`
	OldString  string `json:"old_string"`
	NewString  string `json:"new_string"`
	ReplaceAll bool   `json:"replace_all"`
	Mode       string `json:"mode"`
}

// span is a half-open byte range in the file content.
type span struct{ start, end int }

func (patchTool) Run(ctx context.Context, env *Env, args json.RawMessage) (string, error) {
	var a patchArgs
	if err := json.Unmarshal(args, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	if a.Mode == "patch" {
		return errorResult("Multi-file patch text is not accepted; send path, old_string and new_string"), nil
	}
	if a.Path == "" {
		return errorResult("path is required"), nil
	}
	if a.OldString == "" {
		return errorResult("old_string cannot be empty"), nil
	}
	if a.OldString == a.NewString {
		return errorResult("new_string is identical to old_string, so there is nothing to change"), nil
	}
	abs, err := env.Policy.Resolve(env.State.Cwd(), a.Path)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	check := env.Policy.CheckWrite(abs)
	if check.Denied {
		return errorResult(check.Reason), nil
	}
	if v := env.Gate.Write(ctx, abs, check.Protected); !v.Allowed {
		return errorResult(v.Message), nil
	}
	content, err := envReadFile(env, abs)
	if err != nil {
		return errorResult("cannot read " + a.Path + ": " + err.Error()), nil
	}
	text := string(content)

	spans, strategy := findMatches(text, a.OldString)
	if len(spans) == 0 {
		// An already-applied patch is a no-op success.
		if len(a.NewString) >= 8 && strings.Contains(text, a.NewString) {
			out, _ := jsonResult(map[string]any{"success": true, "no_change": true, "resolved_path": abs})
			return out, nil
		}
		return errorResult(fmt.Sprintf("old_string does not occur in %s (%s)", a.Path, strategyHint())), nil
	}
	if len(spans) > 1 && !a.ReplaceAll {
		lines := matchLineNumbers(text, spans)
		return errorResult(fmt.Sprintf("old_string occurs %d times in %s (lines %s); include more of the surrounding text so only one matches, or set replace_all",
			len(spans), a.Path, strings.Join(lines, ", "))), nil
	}
	if len(spans) > 1 && a.ReplaceAll && strategy != "exact" {
		return errorResult("replace_all needs an exact match; this one only matched loosely, so add context and replace a single block"), nil
	}

	newText := applySpans(text, spans, a.NewString, a.OldString, strategy)
	diff := unifiedDiff(a.Path, text, newText)
	edit := env.LSP.Begin(abs)
	warning := stalenessWarning(env, abs)
	if _, _, err := envWriteAtomic(env, abs, newText); err != nil {
		return errorResult(err.Error()), nil
	}
	result := map[string]any{
		"success":       true,
		"diff":          diff,
		"resolved_path": abs,
	}
	if warning != "" {
		result["_warning"] = warning
	}
	if report := edit.Finish(ctx, newText); report != "" {
		result["lsp_diagnostics"] = report
	}
	refreshStamp(env, abs)
	return jsonResult(result)
}

func strategyHint() string {
	return "looked for an exact match, then ignoring indentation, then ignoring whitespace runs"
}

// findMatches locates old in text with the three strategies, in order:
// exact substring, line-trimmed (re-indented), whitespace-normalized.
// It returns the matched spans and the strategy that found them.
func findMatches(text, old string) ([]span, string) {
	if s := exactSpans(text, old); len(s) > 0 {
		return s, "exact"
	}
	if s, ok := lineTrimmedSpans(text, old); ok && len(s) > 0 {
		return s, "line-trimmed"
	}
	if s, ok := whitespaceSpans(text, old); ok && len(s) > 0 {
		return s, "whitespace-normalized"
	}
	return nil, "none"
}

// exactSpans finds every occurrence of old in text.
func exactSpans(text, old string) []span {
	var out []span
	base := 0
	for {
		i := strings.Index(text, old)
		if i < 0 {
			return out
		}
		out = append(out, span{base + i, base + i + len(old)})
		base += i + len(old)
		text = text[i+len(old):]
	}
}

// lineTrimmedSpans matches old's lines against file lines with leading
// whitespace stripped on both sides.
func lineTrimmedSpans(text, old string) ([]span, bool) {
	oldLines := splitLines(old)
	if len(oldLines) == 0 {
		return nil, false
	}
	fileLines := lineOffsets(text)
	var out []span
	for i := 0; i+len(oldLines) <= len(fileLines); i++ {
		matched := true
		for j, ol := range oldLines {
			if strings.TrimLeft(fileLines[i+j].text, " \t") != strings.TrimLeft(ol, " \t") {
				matched = false
				break
			}
		}
		if !matched {
			continue
		}
		start := fileLines[i].offset
		end := lineEnd(fileLines[i+len(oldLines)-1], text)
		out = append(out, span{start, end})
		i += len(oldLines) - 1
	}
	return out, true
}

// whitespaceSpans matches old against text with runs of spaces/tabs collapsed
// to single spaces, mapping the normalized positions back to the original.
func whitespaceSpans(text, old string) ([]span, bool) {
	normText, ranges := collapseWS(text)
	normOld, _ := collapseWS(old)
	if normOld == "" {
		return nil, false
	}
	var out []span
	rest := normText
	base := 0
	for {
		i := strings.Index(rest, normOld)
		if i < 0 {
			return out, true
		}
		nStart, nEnd := base+i, base+i+len(normOld)
		out = append(out, span{ranges[nStart].start, ranges[nEnd-1].end})
		rest = rest[i+len(normOld):]
		base = nEnd
	}
}

// wsRange is a byte range in the original string.
type wsRange struct{ start, end int }

// collapseWS replaces each run of spaces/tabs with a single space and returns
// the normalized string plus, for each normalized character, the byte range
// it occupies in the original string (a collapsed space covers its whole run).
func collapseWS(s string) (string, []wsRange) {
	var b strings.Builder
	var ranges []wsRange
	runStart := -1
	for i := 0; i < len(s); {
		r := s[i]
		if r == ' ' || r == '	' {
			if runStart < 0 {
				runStart = i
			}
			i++
			continue
		}
		if runStart >= 0 {
			b.WriteByte(' ')
			ranges = append(ranges, wsRange{runStart, i})
			runStart = -1
		}
		b.WriteByte(r)
		ranges = append(ranges, wsRange{i, i + 1})
		i++
	}
	return b.String(), ranges
}

type fileLine struct {
	text   string
	offset int
}

// lineOffsets splits text into lines with their byte offsets.
func lineOffsets(text string) []fileLine {
	var out []fileLine
	offset := 0
	for _, line := range strings.Split(text, "\n") {
		out = append(out, fileLine{line, offset})
		offset += len(line) + 1
	}
	return out
}

// lineEnd returns the end offset of a line's text (not including its newline).
func lineEnd(fl fileLine, text string) int {
	return fl.offset + len(fl.text)
}

// applySpans replaces each span with new, re-indenting new for fuzzy matches.
func applySpans(text string, spans []span, new, old, strategy string) string {
	var b strings.Builder
	prev := 0
	for _, sp := range spans {
		b.WriteString(text[prev:sp.start])
		b.WriteString(reindent(new, old, text, sp, strategy))
		prev = sp.end
	}
	b.WriteString(text[prev:])
	return b.String()
}

// reindent adjusts new's leading whitespace to the file's for fuzzy matches:
// the first matched file line's leading whitespace replaces the leading
// whitespace of old's first line on every new line that starts with it.
func reindent(new, old, text string, sp span, strategy string) string {
	if strategy == "exact" {
		return new
	}
	matched := text[sp.start:sp.end]
	oldFirst := old
	if i := strings.IndexByte(old, '\n'); i >= 0 {
		oldFirst = old[:i]
	}
	matchFirst := matched
	if i := strings.IndexByte(matched, '\n'); i >= 0 {
		matchFirst = matched[:i]
	}
	oldIndent := leadingWS(oldFirst)
	matchIndent := leadingWS(matchFirst)
	if oldIndent == "" || oldIndent == matchIndent {
		return new
	}
	var b strings.Builder
	lines := strings.Split(new, "\n")
	for i, line := range lines {
		if strings.HasPrefix(line, oldIndent) {
			line = matchIndent + strings.TrimPrefix(line, oldIndent)
		}
		if i > 0 {
			b.WriteByte('\n')
		}
		b.WriteString(line)
	}
	return b.String()
}

func leadingWS(s string) string {
	i := 0
	for i < len(s) && (s[i] == ' ' || s[i] == '\t') {
		i++
	}
	return s[:i]
}

// matchLineNumbers returns up to 5 1-based line numbers for the spans.
func matchLineNumbers(text string, spans []span) []string {
	var out []string
	for _, sp := range spans {
		if len(out) >= 5 {
			break
		}
		n := strings.Count(text[:sp.start], "\n") + 1
		out = append(out, fmt.Sprint(n))
	}
	return out
}

// unifiedDiff renders a unified diff with three lines of context, using a
// line-level LCS. Files are small; the O(n*m) table is fine.
func unifiedDiff(path, old, new string) string {
	oldLines := strings.Split(old, "\n")
	newLines := strings.Split(new, "\n")
	ops := lcsOps(oldLines, newLines)

	type hunk struct {
		oldStart, oldCount, newStart, newCount int
		body                                   []string
	}
	var hunks []hunk
	const ctx = 3
	for i := 0; i < len(ops); i++ {
		if ops[i] == ' ' {
			continue
		}
		// Extent of this change run.
		j := i
		for j < len(ops) && ops[j] != ' ' {
			j++
		}
		lo, hi := i-ctx, j+ctx
		if lo < 0 {
			lo = 0
		}
		if hi > len(ops) {
			hi = len(ops)
		}
		// Count old/new lines in [lo, hi).
		var body []string
		oldStart, oldCount, newStart, newCount := 0, 0, 0, 0
		oldSeen, newSeen := 0, 0
		for k := 0; k < lo; k++ {
			if ops[k] == ' ' || ops[k] == '-' {
				oldSeen++
			}
			if ops[k] == ' ' || ops[k] == '+' {
				newSeen++
			}
		}
		oldStart, newStart = oldSeen, newSeen
		for k := lo; k < hi; k++ {
			switch ops[k] {
			case ' ':
				body = append(body, " "+lineAt(oldLines, oldSeen))
				oldSeen++
				newSeen++
				oldCount++
				newCount++
			case '-':
				body = append(body, "-"+lineAt(oldLines, oldSeen))
				oldSeen++
				oldCount++
			case '+':
				body = append(body, "+"+lineAt(newLines, newSeen))
				newSeen++
				newCount++
			}
		}
		hunks = append(hunks, hunk{oldStart + 1, oldCount, newStart + 1, newCount, body})
		i = j - 1
		// Merge overlapping context of adjacent hunks.
	}
	// Merge hunks whose context overlaps.
	var merged []hunk
	for _, h := range hunks {
		if len(merged) > 0 {
			p := &merged[len(merged)-1]
			if h.oldStart <= p.oldStart+p.oldCount+ctx*2 {
				p.body = append(p.body, h.body...)
				p.oldCount += h.oldCount
				p.newCount += h.newCount
				continue
			}
		}
		merged = append(merged, h)
	}

	var b strings.Builder
	fmt.Fprintf(&b, "--- a/%s\n+++ b/%s\n", path, path)
	for _, h := range merged {
		fmt.Fprintf(&b, "@@ -%d,%d +%d,%d @@\n", h.oldStart, h.oldCount, h.newStart, h.newCount)
		for _, l := range h.body {
			b.WriteString(l)
			b.WriteByte('\n')
		}
	}
	return b.String()
}

// lcsOps returns the edit script as ' ' (equal), '-' (delete) and '+' (add).
func lcsOps(a, b []string) []byte {
	n, m := len(a), len(b)
	// Cap the table for pathologically large inputs; patch targets are small.
	if n*m > 4_000_000 {
		return naiveReplaceOps(a, b)
	}
	dp := make([][]int32, n+1)
	for i := range dp {
		dp[i] = make([]int32, m+1)
	}
	for i := n - 1; i >= 0; i-- {
		for j := m - 1; j >= 0; j-- {
			if a[i] == b[j] {
				dp[i][j] = dp[i+1][j+1] + 1
			} else if dp[i+1][j] >= dp[i][j+1] {
				dp[i][j] = dp[i+1][j]
			} else {
				dp[i][j] = dp[i][j+1]
			}
		}
	}
	var out []byte
	i, j := 0, 0
	for i < n && j < m {
		switch {
		case a[i] == b[j]:
			out = append(out, ' ')
			i++
			j++
		case dp[i+1][j] >= dp[i][j+1]:
			out = append(out, '-')
			i++
		default:
			out = append(out, '+')
			j++
		}
	}
	for ; i < n; i++ {
		out = append(out, '-')
	}
	for ; j < m; j++ {
		out = append(out, '+')
	}
	return out
}

// naiveReplaceOps is the fallback for huge inputs: replace everything.
func naiveReplaceOps(a, b []string) []byte {
	out := make([]byte, 0, len(a)+len(b))
	for range a {
		out = append(out, '-')
	}
	for range b {
		out = append(out, '+')
	}
	return out
}

func lineAt(lines []string, i int) string {
	if i < 0 || i >= len(lines) {
		return ""
	}
	return lines[i]
}
