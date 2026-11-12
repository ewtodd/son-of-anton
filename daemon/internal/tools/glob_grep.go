package tools

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

const (
	globMaxFiles     = 100
	grepDefaultLimit = 250
	grepLineCap      = 500
)

// Glob and Grep run on ripgrep, so both skip hidden files and what
// .gitignore and its kin ignore. ripgrep matches a --glob holding a slash
// against the path relative to its own working directory, not the directory
// searched, so path globs are matched here (globMatch) and rg gets only
// their last segment, to narrow what it lists.

type globTool struct{}

func (globTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "Glob",
		Description: "List the files whose paths match a glob pattern, the most recently modified first. " +
			"* matches within one path segment, ** across any number of them, and {a,b} either alternative: " +
			"\"**/*.go\", \"src/**/*.{ts,tsx}\", \"docs/*.md\". A pattern with no slash matches file names at any depth. " +
			"At most 100 paths come back. Use Grep to search inside files.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "pattern": {"type": "string", "description": "The glob to match paths against, relative to path."},
    "path": {"type": "string", "description": "The directory to search in. Defaults to the working directory."}
  },
  "required": ["pattern"]
}`),
	}}
}

func (globTool) Run(ctx context.Context, env *Env, args json.RawMessage) (string, error) {
	var a struct {
		Pattern string `json:"pattern"`
		Path    string `json:"path"`
	}
	if err := json.Unmarshal(args, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	pattern := strings.TrimSpace(a.Pattern)
	if pattern == "" {
		return errorResult("pattern is required"), nil
	}
	root := a.Path
	if root == "" {
		root = env.State.Cwd()
	}
	// An absolute or ~/ pattern carries its own directory. One whose first
	// segment is already a wildcard ("/*.go") names none: its slash anchors it
	// at path, and the whole filesystem is never searched.
	if strings.HasPrefix(pattern, "/") || strings.HasPrefix(pattern, "~/") {
		if dir, rest := splitGlobRoot(pattern); dir != "/" {
			root, pattern = dir, rest
		}
	}
	abs, isDir, msg := searchRoot(env, root)
	if msg != "" {
		return errorResult(msg), nil
	}
	var all []string
	if !isDir {
		if globMatch(pattern, filepath.Base(abs)) && env.Policy.CheckRead(abs) == nil {
			all = append(all, abs)
		}
	} else {
		cmdArgs := []string{"--files", "--sortr", "modified"}
		for _, g := range prefilterGlobs(pattern) {
			cmdArgs = append(cmdArgs, "--glob", g)
		}
		out, err := runRg(ctx, env, append(cmdArgs, "--", abs)...)
		if err != nil {
			return errorResult(err.Error()), nil
		}
		for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
			if line == "" {
				continue
			}
			rel, err := filepath.Rel(abs, line)
			if err != nil || !globMatch(pattern, filepath.ToSlash(rel)) || env.Policy.CheckRead(line) != nil {
				continue
			}
			all = append(all, line)
		}
	}
	shown := all[:min(len(all), globMaxFiles)]
	if shown == nil {
		shown = []string{}
	}
	return jsonResult(map[string]any{"files": shown, "total_count": len(all), "truncated": len(all) > len(shown)})
}

// splitGlobRoot splits an absolute (or ~/) pattern at its first segment
// holding a wildcard: the directory before it, and the rest as a pattern
// anchored there.
func splitGlobRoot(pattern string) (root, rest string) {
	segs := strings.Split(pattern, "/")
	i := 0
	for i < len(segs)-1 && !strings.ContainsAny(segs[i], "*?[{") {
		i++
	}
	root = strings.Join(segs[:i], "/")
	if root == "" {
		root = "/"
	}
	return root, "/" + strings.Join(segs[i:], "/")
}

// searchRoot resolves and checks the directory or file a search starts at,
// or says why it cannot.
func searchRoot(env *Env, p string) (abs string, isDir bool, msg string) {
	abs, err := env.Policy.Resolve(env.State.Cwd(), p)
	if err != nil {
		return "", false, err.Error()
	}
	if err := env.Policy.CheckConfined(abs); err != nil {
		return "", false, err.Error()
	}
	fi, err := envStat(env, abs)
	if err != nil {
		return "", false, "nothing at " + abs
	}
	return abs, fi.IsDir(), ""
}

// globMatch says whether rel, a slash-separated path relative to the search
// root, matches pattern. A pattern with no slash matches the last segment at
// any depth; one with a slash is anchored at the root, which a leading slash
// also names. ** stands for any number of segments and {a,b} for either
// alternative.
func globMatch(pattern, rel string) bool {
	segs := strings.Split(rel, "/")
	for _, p := range expandBraces(pattern) {
		if !strings.Contains(p, "/") {
			p = "**/" + p
		}
		if matchSegments(strings.Split(strings.TrimPrefix(p, "/"), "/"), segs) {
			return true
		}
	}
	return false
}

func matchSegments(pat, segs []string) bool {
	for len(pat) > 0 {
		if pat[0] == "**" {
			for len(pat) > 0 && pat[0] == "**" {
				pat = pat[1:]
			}
			if len(pat) == 0 {
				return true
			}
			for i := range len(segs) + 1 {
				if matchSegments(pat, segs[i:]) {
					return true
				}
			}
			return false
		}
		if len(segs) == 0 {
			return false
		}
		if ok, err := path.Match(pat[0], segs[0]); err != nil || !ok {
			return false
		}
		pat, segs = pat[1:], segs[1:]
	}
	return len(segs) == 0
}

// expandBraces expands every {a,b,...} group of a glob into the patterns it
// stands for; a brace with no closing partner is literal.
func expandBraces(p string) []string {
	open := strings.IndexByte(p, '{')
	if open < 0 {
		return []string{p}
	}
	depth, end := 0, -1
	var cuts []int
	for i := open; i < len(p) && end < 0; i++ {
		switch p[i] {
		case '{':
			depth++
		case '}':
			if depth--; depth == 0 {
				end = i
			}
		case ',':
			if depth == 1 {
				cuts = append(cuts, i)
			}
		}
	}
	if end < 0 {
		return []string{p}
	}
	var out []string
	prev := open + 1
	for _, c := range append(cuts, end) {
		for _, alt := range expandBraces(p[prev:c]) {
			for _, tail := range expandBraces(p[end+1:]) {
				out = append(out, p[:open]+alt+tail)
			}
		}
		prev = c + 1
	}
	return out
}

// prefilterGlobs are --glob arguments that keep rg to the files a pattern
// can match: each alternative's last segment, matched by name at any depth.
// None when an alternative's last segment is ** (any file can match).
func prefilterGlobs(pattern string) []string {
	var out []string
	for _, p := range expandBraces(pattern) {
		last := p[strings.LastIndexByte(p, '/')+1:]
		if last == "" || strings.Contains(last, "**") {
			return nil
		}
		out = append(out, last)
	}
	return out
}

// runRg runs ripgrep and returns its standard output; exit status 1 (nothing
// found) is not an error.
func runRg(ctx context.Context, env *Env, args ...string) (string, error) {
	cmd, err := rgCommand(ctx, env, args...)
	if err != nil {
		return "", err
	}
	var out, errBuf strings.Builder
	cmd.Stdout = &out
	cmd.Stderr = &errBuf
	if err := cmd.Run(); err != nil {
		if ee, ok := err.(*exec.ExitError); !ok || ee.ExitCode() != 1 {
			reason := strings.TrimSpace(errBuf.String())
			if reason == "" {
				reason = err.Error()
			}
			return "", fmt.Errorf("rg failed: %s", reason)
		}
	}
	return out.String(), nil
}

type grepTool struct{}

func (grepTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "Grep",
		Description: "Search inside files with a regular expression, in ripgrep's syntax (so a literal brace is escaped: interface\\{\\}). " +
			"output_mode picks the answer: files_with_matches (the default) lists the files that match, the most recently modified first; " +
			"content shows the matching lines, with -A, -B and -C for lines of context and -n for line numbers (on unless set false); " +
			"count gives the number of matching lines per file. Narrow the files searched with glob or type. " +
			"A match stays within one line unless multiline is set. " +
			"head_limit keeps the first N lines or entries (250 unless set). Use this rather than grep or rg in a shell.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "pattern": {"type": "string", "description": "The regular expression to look for."},
    "path": {"type": "string", "description": "The file or directory to search. Defaults to the working directory."},
    "glob": {"type": "string", "description": "Only search files matching this glob, such as \"*.js\" or \"*.{ts,tsx}\"; a leading ! leaves them out instead."},
    "type": {"type": "string", "description": "Only search files of this ripgrep type, such as go, py, js or rust."},
    "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"], "description": "content: the matching lines; files_with_matches: the files (the default); count: matches per file."},
    "-i": {"type": "boolean", "description": "Ignore case."},
    "-n": {"type": "boolean", "description": "With content: number the lines. Defaults to true."},
    "-A": {"type": "integer", "description": "With content: lines to show after each match."},
    "-B": {"type": "integer", "description": "With content: lines to show before each match."},
    "-C": {"type": "integer", "description": "With content: lines to show before and after each match."},
    "multiline": {"type": "boolean", "description": "Let a match span lines, with . matching newlines too. Defaults to false."},
    "head_limit": {"type": "integer", "description": "Keep only the first N lines (content), files (files_with_matches) or entries (count). Defaults to 250."}
  },
  "required": ["pattern"]
}`),
	}}
}

type grepArgs struct {
	pattern, path, glob, typ, mode string
	ignoreCase, numbers, multiline bool
	after, before, around, limit   int
}

func parseGrepArgs(raw json.RawMessage) (grepArgs, string) {
	var in map[string]any
	if err := json.Unmarshal(raw, &in); err != nil {
		return grepArgs{}, "invalid arguments: " + err.Error()
	}
	a := grepArgs{pattern: argStr(in, "pattern"), path: argStr(in, "path"), glob: argStr(in, "glob"), typ: argStr(in, "type"),
		mode: argStr(in, "output_mode"), ignoreCase: argBool(in, "-i"), numbers: true, multiline: argBool(in, "multiline"),
		limit: grepDefaultLimit}
	if v, ok := in["-n"]; ok && v != nil {
		a.numbers = argBool(in, "-n")
	}
	for key, dst := range map[string]*int{"-A": &a.after, "-B": &a.before, "-C": &a.around} {
		if n, ok := argInt(in, key); ok && n > 0 {
			*dst = int(n)
		}
	}
	if n, ok := argInt(in, "head_limit"); ok && n > 0 {
		a.limit = int(n)
	}
	if a.mode == "" {
		a.mode = "files_with_matches"
	}
	switch {
	case a.pattern == "":
		return a, "pattern is required"
	case a.mode != "content" && a.mode != "files_with_matches" && a.mode != "count":
		return a, "output_mode has to be content, files_with_matches or count"
	}
	return a, ""
}

// argBool reads a flag leniently: true, or a string such as "true".
func argBool(in map[string]any, key string) bool {
	switch v := in[key].(type) {
	case bool:
		return v
	case string:
		b, _ := strconv.ParseBool(strings.TrimSpace(v))
		return b
	}
	return false
}

func (grepTool) Run(ctx context.Context, env *Env, raw json.RawMessage) (string, error) {
	a, msg := parseGrepArgs(raw)
	if msg != "" {
		return errorResult(msg), nil
	}
	root := a.path
	if root == "" {
		root = env.State.Cwd()
	}
	abs, isDir, msg := searchRoot(env, root)
	if msg != "" {
		return errorResult(msg), nil
	}
	var rgArgs []string
	if a.ignoreCase {
		rgArgs = append(rgArgs, "-i")
	}
	if a.multiline {
		rgArgs = append(rgArgs, "-U", "--multiline-dotall")
	}
	if a.typ != "" {
		rgArgs = append(rgArgs, "--type", a.typ)
	}
	// A glob with no slash means the same to rg wherever it runs; one with a
	// slash is matched here, against the path relative to the root.
	pathGlob := ""
	if strings.Contains(a.glob, "/") {
		pathGlob = a.glob
		if !strings.HasPrefix(pathGlob, "!") {
			for _, g := range prefilterGlobs(pathGlob) {
				rgArgs = append(rgArgs, "--glob", g)
			}
		}
	} else if a.glob != "" {
		rgArgs = append(rgArgs, "--glob", a.glob)
	}
	keep := func(p string) bool {
		if env.Policy.CheckRead(p) != nil {
			return false
		}
		if pathGlob == "" {
			return true
		}
		rel := filepath.Base(p)
		if isDir {
			if r, err := filepath.Rel(abs, p); err == nil {
				rel = filepath.ToSlash(r)
			}
		}
		if neg, ok := strings.CutPrefix(pathGlob, "!"); ok {
			return !globMatch(neg, rel)
		}
		return globMatch(pathGlob, rel)
	}
	switch a.mode {
	case "files_with_matches":
		out, err := runRg(ctx, env, append(rgArgs, "--files-with-matches", "--null", "--", a.pattern, abs)...)
		if err != nil {
			return errorResult(err.Error()), nil
		}
		var files []string
		for _, p := range strings.Split(out, "\x00") {
			if p = strings.TrimSpace(p); p != "" && keep(p) {
				files = append(files, p)
			}
		}
		files = newestFirst(env, files)
		shown := files[:min(len(files), a.limit)]
		if shown == nil {
			shown = []string{}
		}
		return jsonResult(map[string]any{"files": shown, "total_count": len(files), "truncated": len(files) > len(shown)})
	case "count":
		out, err := runRg(ctx, env, append(rgArgs, "--count", "--with-filename", "--null", "--sort", "path", "--", a.pattern, abs)...)
		if err != nil {
			return errorResult(err.Error()), nil
		}
		type fileCount struct {
			Path  string `json:"path"`
			Count int    `json:"count"`
		}
		counts := []fileCount{}
		total := 0
		for _, line := range strings.Split(out, "\n") {
			p, n, ok := strings.Cut(line, "\x00")
			c, err := strconv.Atoi(strings.TrimSpace(n))
			if !ok || err != nil || !keep(p) {
				continue
			}
			counts = append(counts, fileCount{p, c})
			total += c
		}
		shown := counts[:min(len(counts), a.limit)]
		return jsonResult(map[string]any{"counts": shown, "total_matches": total, "files": len(counts), "truncated": len(counts) > len(shown)})
	}
	if a.around > 0 {
		rgArgs = append(rgArgs, "-C", strconv.Itoa(a.around))
	}
	if a.after > 0 {
		rgArgs = append(rgArgs, "-A", strconv.Itoa(a.after))
	}
	if a.before > 0 {
		rgArgs = append(rgArgs, "-B", strconv.Itoa(a.before))
	}
	out, err := runRg(ctx, env, append(rgArgs, "--json", "--sort", "path", "--", a.pattern, abs)...)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	var kept []rgLine
	matches := 0
	for _, l := range parseRgLines(out) {
		if keep(l.path) {
			kept = append(kept, l)
			if l.match {
				matches++
			}
		}
	}
	text, shown := grepContent(kept, !isDir, a.numbers, a.after+a.before+a.around > 0, a.limit)
	return jsonOut(map[string]any{"content": text, "matches": matches, "truncated": shown < len(kept)}), nil
}

// newestFirst orders paths by modification time, the newest first.
func newestFirst(env *Env, paths []string) []string {
	mod := make(map[string]time.Time, len(paths))
	for _, p := range paths {
		if fi, err := envStat(env, p); err == nil {
			mod[p] = fi.ModTime()
		}
	}
	sort.SliceStable(paths, func(i, j int) bool { return mod[paths[i]].After(mod[paths[j]]) })
	return paths
}

// rgLine is one line of a content search: a matching line or a context line.
type rgLine struct {
	path  string
	line  int
	text  string
	match bool
}

// parseRgLines reads ripgrep's --json stream into its lines, in order. A
// multiline match arrives as one event holding all of its lines.
func parseRgLines(out string) []rgLine {
	var lines []rgLine
	sc := bufio.NewScanner(strings.NewReader(out))
	sc.Buffer(make([]byte, 1024*1024), 16*1024*1024)
	for sc.Scan() {
		var ev struct {
			Type string `json:"type"`
			Data struct {
				Path       struct{ Text string } `json:"path"`
				LineNumber int                   `json:"line_number"`
				Lines      struct{ Text string } `json:"lines"`
			} `json:"data"`
		}
		if json.Unmarshal(sc.Bytes(), &ev) != nil || (ev.Type != "match" && ev.Type != "context") {
			continue
		}
		text := strings.TrimSuffix(ev.Data.Lines.Text, "\n")
		for i, t := range strings.Split(text, "\n") {
			lines = append(lines, rgLine{ev.Data.Path.Text, ev.Data.LineNumber + i, strings.TrimSuffix(t, "\r"), ev.Type == "match"})
		}
	}
	return lines
}

// grepContent renders lines as ripgrep prints them: path:line:text for a
// match and path-line-text for context, with no path when one file was
// searched and no line number with numbers off, and "--" between groups when
// context was asked for. It keeps at most limit output lines and reports how
// many of lines it showed.
func grepContent(lines []rgLine, oneFile, numbers, context bool, limit int) (string, int) {
	var b bytes.Buffer
	emitted, shown := 0, 0
	for i, l := range lines {
		if context && i > 0 && (l.path != lines[i-1].path || l.line != lines[i-1].line+1) {
			if emitted >= limit {
				break
			}
			b.WriteString("--\n")
			emitted++
		}
		if emitted >= limit {
			break
		}
		sep := "-"
		if l.match {
			sep = ":"
		}
		if !oneFile {
			b.WriteString(l.path + sep)
		}
		if numbers {
			b.WriteString(strconv.Itoa(l.line) + sep)
		}
		text := l.text
		if len(text) > grepLineCap {
			text = text[:grepLineCap] + " ... [truncated]"
		}
		b.WriteString(text + "\n")
		emitted++
		shown++
	}
	return strings.TrimSuffix(b.String(), "\n"), shown
}
