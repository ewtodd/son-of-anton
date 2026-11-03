package tools

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"strings"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

const searchMatchContentCap = 500

type searchFilesTool struct{}

func (searchFilesTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "search_files",
		Description: "Look for text inside files with a regular expression, or look for files by name with a glob. " +
			"A content search answers with path, line number and the line itself. " +
			"A file search answers with paths, most recently modified first.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "pattern": {"type": "string", "description": "A regular expression when searching contents; a glob such as *.py when searching for files."},
    "target": {"type": "string", "enum": ["content", "files"], "description": "content looks inside files; files looks at file names.", "default": "content"},
    "path": {"type": "string", "description": "Where to look: a directory or a single file. Defaults to the working directory."},
    "file_glob": {"type": "string", "description": "In content searches, only look in files whose names match this glob, such as '*.py'."},
    "output_mode": {"type": "string", "enum": ["content", "files_only", "count"], "description": "For content searches: content prints the matching lines, files_only prints just the paths, count prints the number of matches per file.", "default": "content"},
    "limit": {"type": "integer", "description": "Most results to return. Defaults to 50.", "default": 50},
    "offset": {"type": "integer", "description": "Number of results to skip, for paging through a long list. Defaults to 0.", "default": 0},
    "context": {"type": "integer", "description": "Lines to show before and after each match in content searches. Defaults to 0.", "default": 0}
  },
  "required": ["pattern"]
}`),
	}}
}

type searchArgs struct {
	Pattern    string `json:"pattern"`
	Target     string `json:"target"`
	Path       string `json:"path"`
	FileGlob   string `json:"file_glob"`
	OutputMode string `json:"output_mode"`
	Limit      int    `json:"limit"`
	Offset     int    `json:"offset"`
	Context    int    `json:"context"`
}

type rgEntry struct {
	isMatch  bool
	path     string
	line     int
	text     string
	matchIdx int
}

func (searchFilesTool) Run(ctx context.Context, env *Env, args json.RawMessage) (string, error) {
	var a searchArgs
	if err := json.Unmarshal(args, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	if a.Pattern == "" {
		return errorResult("pattern is required"), nil
	}
	if a.Target == "" {
		a.Target = "content"
	}
	if a.OutputMode == "" {
		a.OutputMode = "content"
	}
	if a.Limit <= 0 {
		a.Limit = 50
	}
	if a.Offset < 0 {
		a.Offset = 0
	}
	searchPath := a.Path
	if searchPath == "" {
		searchPath = env.State.Cwd()
	}
	abs, err := env.Policy.Resolve(env.State.Cwd(), searchPath)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	if err := env.Policy.CheckConfined(abs); err != nil {
		return errorResult(err.Error()), nil
	}
	if fi, err := envStat(env, abs); err != nil || !fi.IsDir() {
		return errorResult("nothing at " + a.Path), nil
	}

	switch a.Target {
	case "files":
		return runFileSearch(ctx, env, a, abs)
	case "content":
		return runContentSearch(ctx, env, a, abs)
	default:
		return errorResult("target has to be content or files"), nil
	}
}

func runContentSearch(ctx context.Context, env *Env, a searchArgs, abs string) (string, error) {
	cmd, err := rgCommand(ctx, env, "--json", "--sort", "path")
	if err != nil {
		return errorResult(err.Error()), nil
	}
	if a.Context > 0 {
		cmd.Args = append(cmd.Args, "-C", fmt.Sprint(a.Context))
	}
	if a.FileGlob != "" {
		cmd.Args = append(cmd.Args, "--glob", a.FileGlob)
	}
	cmd.Args = append(cmd.Args, "--", a.Pattern, abs)
	var out, errBuf strings.Builder
	cmd.Stdout = &out
	cmd.Stderr = &errBuf
	runErr := cmd.Run()
	if runErr != nil {
		if ee, ok := runErr.(*exec.ExitError); ok {
			if ee.ExitCode() == 1 {
				// No matches: not an error.
			} else {
				return errorResult("rg failed: " + strings.TrimSpace(errBuf.String())), nil
			}
		} else {
			return errorResult("rg failed: " + runErr.Error()), nil
		}
	}

	entries, matchFiles := parseRgJSON(out.String())
	// Drop read-blocked files.
	allowed := map[string]bool{}
	for _, f := range matchFiles {
		if env.Policy.CheckRead(f) == nil {
			allowed[f] = true
		}
	}
	var kept []rgEntry
	keptMatches := 0
	for _, e := range entries {
		if !allowed[e.path] {
			continue
		}
		if e.isMatch {
			keptMatches++
		}
		kept = append(kept, e)
	}

	total := keptMatches
	truncated := total > a.Offset+a.Limit

	switch a.OutputMode {
	case "files_only":
		var files []string
		seen := map[string]bool{}
		for _, e := range kept {
			if e.isMatch && !seen[e.path] {
				seen[e.path] = true
				files = append(files, e.path)
			}
		}
		return jsonResult(map[string]any{
			"files": files, "total_count": total, "truncated": truncated,
		})
	case "count":
		counts := map[string]int{}
		order := []string{}
		for _, e := range kept {
			if e.isMatch {
				if counts[e.path] == 0 {
					order = append(order, e.path)
				}
				counts[e.path]++
			}
		}
		var outCounts []map[string]any
		for _, p := range order {
			outCounts = append(outCounts, map[string]any{"path": p, "count": counts[p]})
		}
		return jsonResult(map[string]any{
			"counts": outCounts, "total_count": total, "truncated": truncated,
		})
	default: // content
		// Page over matches; context entries ride along with their match.
		matchIdxOnPage := map[int]bool{}
		n := 0
		for _, e := range kept {
			if e.isMatch {
				if n >= a.Offset && n < a.Offset+a.Limit {
					matchIdxOnPage[e.matchIdx] = true
				}
				n++
			}
		}
		var matches []map[string]any
		for _, e := range kept {
			if !matchIdxOnPage[e.matchIdx] {
				continue
			}
			m := map[string]any{
				"path":    e.path,
				"line":    e.line,
				"content": capMatchContent(e.text),
			}
			if !e.isMatch {
				m["context"] = true
			}
			matches = append(matches, m)
		}
		return jsonResult(map[string]any{
			"matches": matches, "total_count": total, "truncated": truncated,
		})
	}
}

func capMatchContent(s string) string {
	if len(s) > searchMatchContentCap {
		return s[:searchMatchContentCap]
	}
	return s
}

// parseRgJSON parses ripgrep's --json stream into ordered entries. Context
// lines are attributed to their adjacent match.
func parseRgJSON(out string) ([]rgEntry, []string) {
	var entries []rgEntry
	var matchFiles []string
	var pending []rgEntry
	lastMatch := -1
	sc := bufio.NewScanner(strings.NewReader(out))
	sc.Buffer(make([]byte, 1024*1024), 16*1024*1024)
	for sc.Scan() {
		line := sc.Text()
		if line == "" {
			continue
		}
		var ev struct {
			Type string          `json:"type"`
			Data json.RawMessage `json:"data"`
		}
		if err := json.Unmarshal([]byte(line), &ev); err != nil {
			continue
		}
		switch ev.Type {
		case "begin":
			lastMatch = -1
			pending = nil
		case "match":
			var d struct {
				Path       struct{ Text string } `json:"path"`
				LineNumber int                   `json:"line_number"`
				Lines      struct{ Text string } `json:"lines"`
			}
			if err := json.Unmarshal(ev.Data, &d); err != nil {
				continue
			}
			idx := len(matchFiles)
			matchFiles = append(matchFiles, d.Path.Text)
			for i := range pending {
				pending[i].matchIdx = idx
				entries = append(entries, pending[i])
			}
			pending = nil
			entries = append(entries, rgEntry{true, d.Path.Text, d.LineNumber, d.Lines.Text, idx})
			lastMatch = idx
		case "context":
			var d struct {
				Path       struct{ Text string } `json:"path"`
				LineNumber int                   `json:"line_number"`
				Lines      struct{ Text string } `json:"lines"`
			}
			if err := json.Unmarshal(ev.Data, &d); err != nil {
				continue
			}
			if lastMatch >= 0 {
				entries = append(entries, rgEntry{false, d.Path.Text, d.LineNumber, d.Lines.Text, lastMatch})
			} else {
				pending = append(pending, rgEntry{false, d.Path.Text, d.LineNumber, d.Lines.Text, -1})
			}
		}
	}
	return entries, matchFiles
}

func runFileSearch(ctx context.Context, env *Env, a searchArgs, abs string) (string, error) {
	glob := a.Pattern
	if !strings.ContainsAny(glob, "*?[") {
		glob = "*" + glob + "*"
	}
	cmdArgs := []string{"--files", "--sortr", "modified", "--glob", glob, abs}
	cmd, err := rgCommand(ctx, env, cmdArgs...)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	var out, errBuf strings.Builder
	cmd.Stdout = &out
	cmd.Stderr = &errBuf
	if err := cmd.Run(); err != nil {
		return errorResult("rg failed: " + strings.TrimSpace(errBuf.String())), nil
	}
	var all []string
	for _, line := range strings.Split(strings.TrimSpace(out.String()), "\n") {
		if line == "" {
			continue
		}
		if env.Policy.CheckRead(line) == nil {
			all = append(all, line)
		}
	}
	total := len(all)
	truncated := total > a.Offset+a.Limit
	end := a.Offset + a.Limit
	if end > total {
		end = total
	}
	page := all[a.Offset:end]
	return jsonResult(map[string]any{
		"files": page, "total_count": total, "truncated": truncated,
	})
}
