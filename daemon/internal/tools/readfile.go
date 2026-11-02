package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

const (
	readDefaultLimit = 2000
	readMaxLimit     = 2000
	readMaxLineChars = 2000
	readBudgetChars  = 100_000
	similarFileLimit = 5
)

type readFileTool struct{}

func (readFileTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "read_file",
		Description: "Read a text file with line numbers (\"LINE|CONTENT\"). " +
			"Use offset and limit for large files. Prefer this to cat/head/tail.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "path": {"type": "string", "description": "Path to the file to read (absolute, relative to the working directory, or ~/path)."},
    "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed, default 1).", "default": 1, "minimum": 1},
    "limit": {"type": "integer", "description": "Maximum number of lines to read (default 2000, max 2000).", "default": 2000, "maximum": 2000}
  },
  "required": ["path"]
}`),
	}}
}

type readFileArgs struct {
	Path   string `json:"path"`
	Offset int    `json:"offset"`
	Limit  int    `json:"limit"`
}

func (readFileTool) Run(ctx context.Context, env *Env, args json.RawMessage) (string, error) {
	var a readFileArgs
	if err := json.Unmarshal(args, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	if a.Path == "" {
		return errorResult("path is required"), nil
	}
	abs, err := env.Policy.Resolve(env.State.Cwd(), a.Path)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	if err := env.Policy.CheckRead(abs); err != nil {
		return errorResult(err.Error()), nil
	}
	fi, err := os.Stat(abs)
	if err != nil {
		if os.IsNotExist(err) {
			return jsonResult(map[string]any{
				"error":         "File not found: " + a.Path,
				"similar_files": similarFiles(abs),
			})
		}
		return errorResult(err.Error()), nil
	}
	if fi.IsDir() {
		return errorResult(abs + " is a directory, not a file"), nil
	}
	data, err := os.ReadFile(abs)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	if isBinary(data) {
		return errorResult("Cannot read binary file: " + abs), nil
	}

	lines := splitLines(string(data))
	total := len(lines)
	offset := a.Offset
	if offset < 1 {
		offset = 1
	}
	limit := a.Limit
	if limit <= 0 {
		limit = readDefaultLimit
	}
	if limit > readMaxLimit {
		limit = readMaxLimit
	}

	start := offset - 1
	end := start + limit
	if end > total {
		end = total
	}
	var b strings.Builder
	emitted := 0
	for i := start; i < end; i++ {
		line := lines[i]
		if len(line) > readMaxLineChars {
			line = line[:readMaxLineChars] + " ... [truncated]"
		}
		formatted := fmt.Sprintf("%d|%s", i+1, line)
		if b.Len()+len(formatted)+1 > readBudgetChars && emitted > 0 {
			break
		}
		if emitted > 0 {
			b.WriteByte('\n')
		}
		b.WriteString(formatted)
		emitted++
	}
	truncated := end < total || emitted < end-start
	result := map[string]any{
		"content":     b.String(),
		"total_lines": total,
		"file_size":   fi.Size(),
		"truncated":   truncated,
	}
	nextOffset := start + emitted
	if truncated && nextOffset < total {
		result["next_offset"] = nextOffset + 1
	}
	env.State.SetReadStamp(abs, readStamp{
		mtime:   fi.ModTime().UnixNano(),
		size:    fi.Size(),
		partial: offset > 1 || truncated,
	})
	return jsonResult(result)
}

// isBinary reports whether data looks binary: a NUL byte in the first 8 KiB
// or invalid UTF-8.
func isBinary(data []byte) bool {
	n := len(data)
	if n > 8192 {
		n = 8192
	}
	if bytesIndexByte(data[:n], 0) >= 0 {
		return true
	}
	return !utf8.Valid(data[:n])
}

func bytesIndexByte(b []byte, c byte) int {
	for i, x := range b {
		if x == c {
			return i
		}
	}
	return -1
}

// splitLines splits on \n; a trailing newline does not create an empty line.
func splitLines(s string) []string {
	if s == "" {
		return nil
	}
	lines := strings.Split(s, "\n")
	if lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	return lines
}

// similarFiles lists up to 5 names in the same directory that are within
// edit distance 2 of the wanted name, or share a 4+ char case-insensitive
// prefix.
func similarFiles(abs string) []string {
	dir, base := filepath.Dir(abs), filepath.Base(abs)
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	want := strings.ToLower(base)
	var out []string
	type cand struct {
		name string
		dist int
	}
	var cands []cand
	for _, e := range entries {
		name := e.Name()
		if name == base {
			continue
		}
		lname := strings.ToLower(name)
		d := levenshtein(lname, want, 2)
		if d <= 2 || (len(want) >= 4 && len(lname) >= 4 && strings.HasPrefix(lname, want[:4])) {
			cands = append(cands, cand{name, d})
		}
	}
	for i := range cands {
		for j := i + 1; j < len(cands); j++ {
			if cands[j].dist < cands[i].dist || (cands[j].dist == cands[i].dist && cands[j].name < cands[i].name) {
				cands[i], cands[j] = cands[j], cands[i]
			}
		}
	}
	for _, c := range cands {
		out = append(out, c.name)
		if len(out) >= similarFileLimit {
			break
		}
	}
	return out
}

// levenshtein computes the edit distance capped at cap+1 (returns cap+1 when
// the distance exceeds cap, so callers only need the <= cap test).
func levenshtein(a, b string, cap int) int {
	la, lb := len(a), len(b)
	if abs(la-lb) > cap {
		return cap + 1
	}
	prev := make([]int, lb+1)
	cur := make([]int, lb+1)
	for j := 0; j <= lb; j++ {
		prev[j] = j
	}
	for i := 1; i <= la; i++ {
		cur[0] = i
		rowMin := cur[0]
		for j := 1; j <= lb; j++ {
			cost := 1
			if a[i-1] == b[j-1] {
				cost = 0
			}
			v := prev[j] + 1
			if x := cur[j-1] + 1; x < v {
				v = x
			}
			if x := prev[j-1] + cost; x < v {
				v = x
			}
			cur[j] = v
			if v < rowMin {
				rowMin = v
			}
		}
		if rowMin > cap {
			return cap + 1
		}
		prev, cur = cur, prev
	}
	return prev[lb]
}

func abs(x int) int {
	if x < 0 {
		return -x
	}
	return x
}
