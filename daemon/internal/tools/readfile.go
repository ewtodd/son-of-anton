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
	binarySniffBytes = 8192
)

type readFileTool struct{}

func (readFileTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "read_file",
		Description: "Return the contents of a text file, each line prefixed with its number as NUMBER|TEXT. " +
			"Long files come back in windows: continue with offset and limit. This replaces cat, head and tail.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "path": {"type": "string", "description": "The file to read: absolute, relative to the working directory, or starting with ~/."},
    "offset": {"type": "integer", "description": "First line to return, counting from 1.", "default": 1, "minimum": 1},
    "limit": {"type": "integer", "description": "How many lines to return, at most 2000.", "default": 2000, "maximum": 2000}
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
	fi, err := envStat(env, abs)
	if err != nil {
		if os.IsNotExist(err) {
			return jsonResult(map[string]any{
				"error":         "No such file: " + a.Path,
				"similar_files": similarFiles(env, abs),
			})
		}
		return errorResult(err.Error()), nil
	}
	if fi.IsDir() {
		return errorResult(abs + " is a directory; read a file inside it"), nil
	}
	data, err := envReadFile(env, abs)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	mime := SniffImage(data)
	if mime != "" {
		if mime != "image/svg+xml" {
			if out := readImage(ctx, env, a.Path, abs, data, mime); out != "" {
				return out, nil
			}
			// The sniff said image, and this session could attach images,
			// but the bytes still will not attach (a header the decoders
			// refuse): a failed image is usually text that never was one,
			// so read on.
		}
	}
	if isBinary(data) {
		return errorResult(abs + " holds binary data, not text"), nil
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
	if mime == "image/svg+xml" {
		// An SVG answer is source first — the model reads and edits it —
		// and, when it can be drawn, the render rides beside the source.
		if img := attachSVGRender(ctx, env, abs, data); img != nil {
			result["image"] = img
		}
	}
	return jsonResult(result)
}

// attachSVGRender renders an SVG beside its source. When the session cannot
// see images, or nothing renders (no rsvg-convert, markup that refuses), the
// source alone is the read: a text file was read, and nothing is missing.
func attachSVGRender(ctx context.Context, env *Env, abs string, data []byte) map[string]any {
	if !env.Vision || env.Attach == nil {
		return nil
	}
	out, outMime, _, err := PrepareImage(data, "image/svg+xml")
	if err != nil {
		return nil
	}
	id, err := env.Attach.Add(ctx, outMime, filepath.Base(abs), out)
	if err != nil {
		return nil
	}
	return map[string]any{"id": id, "mime": outMime, "name": filepath.Base(abs)}
}

// readImage returns an image as a multimodal result: the text names the file
// and the image itself rides the result as an image part, straight to the
// model's vision encoder. A model without vision gets an error instead, and
// bytes the decoders refuse come back as "" for the caller to read as text.
func readImage(ctx context.Context, env *Env, path, abs string, data []byte, mime string) string {
	if !env.Vision || env.Attach == nil {
		return errorResult(fmt.Sprintf(
			"'%s' is an image, and this session's model (%s) has no vision, so it cannot be attached. "+
				"Turn it into text yourself (an OCR or a converter) and read that, or come back on a vision-capable model.",
			path, env.Model))
	}
	out, outMime, note, err := PrepareImage(data, mime)
	if err != nil {
		return ""
	}
	id, err := env.Attach.Add(ctx, outMime, filepath.Base(abs), out)
	if err != nil {
		return errorResult("the image could not be attached: " + err.Error())
	}
	res := map[string]any{
		"content":   fmt.Sprintf("Read image '%s' (%s, %d bytes). The image is attached to this result.", path, outMime, len(data)),
		"file_size": len(data),
		"image":     map[string]any{"id": id, "mime": outMime, "name": filepath.Base(abs)},
	}
	if note != "" {
		res["note"] = note
	}
	b, err := jsonResult(res)
	if err != nil {
		return errorResult(err.Error())
	}
	return b
}

// isBinary reports whether data looks binary: a NUL byte in the first 8 KiB
// or invalid UTF-8.
// isBinary sniffs the first binarySniffBytes: a NUL byte, or bytes that are
// not UTF-8. A window that ends inside a character is cut back to where the
// character starts, so text is never called binary for where the window fell
// (a notes file full of dashes was, on 2026-11-07).
func isBinary(data []byte) bool {
	head := data
	if len(head) > binarySniffBytes {
		head = head[:binarySniffBytes]
		for i := 1; i < utf8.UTFMax && i <= len(head); i++ {
			if utf8.RuneStart(head[len(head)-i]) {
				if !utf8.FullRune(head[len(head)-i:]) {
					head = head[:len(head)-i]
				}
				break
			}
		}
	}
	if bytesIndexByte(head, 0) >= 0 {
		return true
	}
	return !utf8.Valid(head)
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
func similarFiles(env *Env, abs string) []string {
	dir, base := filepath.Dir(abs), filepath.Base(abs)
	entries, err := envReadDir(env, dir)
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
