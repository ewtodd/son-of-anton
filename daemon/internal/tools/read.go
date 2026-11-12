package tools

import (
	"context"
	"crypto/sha256"
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
	readMaxLineChars = 2000
	readBudgetChars  = 100_000
	similarFileLimit = 5
	binarySniffBytes = 8192
)

type readTool struct{}

func (readTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "Read",
		Description: "Show a file's contents. Text comes back numbered the way cat -n numbers it: the line number, a tab, then the line, " +
			"counting from 1. One call returns up to 2000 lines, and any line over 2000 characters is cut short; " +
			"page through a longer file with offset and limit. An image file is attached for you to look at, when this session's model takes images. " +
			"A file must have been read in this session before Write may replace it or Edit may change it. " +
			"Prefer this to cat, head or tail.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "file_path": {"type": "string", "description": "The file to read: an absolute path, one relative to the working directory, or one starting with ~/."},
    "offset": {"type": "integer", "description": "The line to start at, counting from 1. Give it for a file too long to read at once.", "minimum": 1},
    "limit": {"type": "integer", "description": "How many lines to show (2000 unless you set it). Give it for a file too long to read at once.", "minimum": 1}
  },
  "required": ["file_path"]
}`),
	}}
}

type readArgs struct {
	FilePath string `json:"file_path"`
	Offset   int    `json:"offset"`
	Limit    int    `json:"limit"`
}

// pathArg is a file tool's file_path, or why there is none: a call that sent
// the old path parameter is told the parameter's new name.
func pathArg(raw json.RawMessage, filePath string) (string, string) {
	if filePath != "" {
		return filePath, ""
	}
	if jsonHasStringField(raw, "path") {
		return "", "file_path is required; this tool takes the file as file_path, not path"
	}
	return "", "file_path is required"
}

func (readTool) Run(ctx context.Context, env *Env, args json.RawMessage) (string, error) {
	var a readArgs
	if err := json.Unmarshal(args, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	path, msg := pathArg(args, a.FilePath)
	if msg != "" {
		return errorResult(msg), nil
	}
	abs, err := env.Policy.Resolve(env.State.Cwd(), path)
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
				"error":         "No such file: " + path,
				"similar_files": similarFiles(env, abs),
			})
		}
		return errorResult(err.Error()), nil
	}
	if fi.IsDir() {
		return errorResult(abs + " is a directory; read a file inside it, or list it with Glob or ls"), nil
	}
	data, err := envReadFile(env, abs)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	stamp := readStamp{size: int64(len(data)), sum: sha256.Sum256(data)}
	mime := SniffImage(data)
	if mime != "" {
		if mime != "image/svg+xml" {
			if out, ok := readImage(ctx, env, path, abs, data, mime); out != "" {
				if ok {
					stamp.whole = true
					env.State.recordRead(abs, stamp, 0, 0)
				}
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
	stamp.lines = total
	offset := max(a.Offset, 1)
	limit := a.Limit
	if limit <= 0 {
		limit = readDefaultLimit
	}

	start := min(offset-1, total)
	end := min(start+min(limit, total), total)
	var b strings.Builder
	emitted := 0
	for i := start; i < end; i++ {
		line := lines[i]
		if len(line) > readMaxLineChars {
			line = line[:readMaxLineChars] + " ... [truncated]"
		}
		formatted := catLine(i+1, line)
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
	switch {
	case total == 0:
		result["note"] = "The file exists but is empty."
	case offset > total:
		result["note"] = fmt.Sprintf("The file has only %d lines, so offset %d is past its end; nothing was shown.", total, offset)
	}
	if total == 0 {
		stamp.whole = true
	}
	env.State.recordRead(abs, stamp, start, start+emitted)
	if mime == "image/svg+xml" {
		// An SVG answer is source first — the model reads and edits it —
		// and, when it can be drawn, the render rides beside the source.
		if img := attachSVGRender(ctx, env, abs, data); img != nil {
			result["image"] = img
		}
	}
	return jsonResult(result)
}

// catLine is line n as Read shows it, numbered the way cat -n numbers lines.
func catLine(n int, line string) string {
	return fmt.Sprintf("%6d\t%s", n, line)
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
// model's vision encoder; attached says it did. A model without vision gets an
// error instead, and bytes the decoders refuse come back as "" for the caller
// to read as text.
func readImage(ctx context.Context, env *Env, path, abs string, data []byte, mime string) (out string, attached bool) {
	if !env.Vision || env.Attach == nil {
		return errorResult(fmt.Sprintf(
			"'%s' is an image, and this session's model (%s) has no vision, so it cannot be attached. "+
				"Turn it into text yourself (an OCR or a converter) and read that, or come back on a vision-capable model.",
			path, env.Model)), false
	}
	img, outMime, note, err := PrepareImage(data, mime)
	if err != nil {
		return "", false
	}
	id, err := env.Attach.Add(ctx, outMime, filepath.Base(abs), img)
	if err != nil {
		return errorResult("the image could not be attached: " + err.Error()), false
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
		return errorResult(err.Error()), false
	}
	return b, true
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
