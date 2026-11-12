package tools

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

type writeTool struct{}

func (writeTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "Write",
		Description: "Save text as a file, replacing whatever the file held; missing parent directories are created. " +
			"A new file can be written straight away. A file that already exists must have been read in full with Read in this session, " +
			"and the write is refused if the file changed on disk after that read. " +
			"To change part of a file, use Edit: it sends only the change.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "file_path": {"type": "string", "description": "Where to write: an absolute path, one relative to the working directory, or one starting with ~/."},
    "content": {"type": "string", "description": "The whole new text of the file."}
  },
  "required": ["file_path", "content"]
}`),
	}}
}

type writeArgs struct {
	FilePath string `json:"file_path"`
	Content  string `json:"content"`
}

func (writeTool) Run(ctx context.Context, env *Env, args json.RawMessage) (string, error) {
	var a writeArgs
	if err := json.Unmarshal(args, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	if a.FilePath == "" && (jsonHasStringField(json.RawMessage(a.Content), "file_path") || jsonHasStringField(json.RawMessage(a.Content), "path")) {
		return errorResult("file_path is required. The content you gave is itself a JSON object holding the file's path and content: " +
			"pass file_path and content as Write's own arguments, with content as the file's text alone."), nil
	}
	path, msg := pathArg(args, a.FilePath)
	if msg != "" {
		return errorResult(msg), nil
	}
	if a.Content == "" && !jsonHasStringField(args, "content") {
		return errorResult("content is required"), nil
	}
	if readEcho(a.Content) {
		return errorResult("Nothing was written: this content is Read's display of a file, not the file's text. Each of its lines starts " +
			"with the line number and the tab (or '|') that Read puts in front of a line. Write the text without them, or use " +
			"Edit to change only the lines that change."), nil
	}
	abs, err := env.Policy.Resolve(env.State.Cwd(), path)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	check := env.Policy.CheckWrite(abs)
	if check.Denied {
		return errorResult(check.Reason), nil
	}
	if fi, err := envStat(env, abs); err == nil {
		if fi.IsDir() {
			return errorResult(abs + " is a directory"), nil
		}
		if why := changeRefusal(env, abs, path, true); why != "" {
			return errorResult(why), nil
		}
	}
	if v := env.Gate.Write(ctx, abs, check.Protected); !v.Allowed {
		return errorResult(v.Message), nil
	}
	edit := env.LSP.Begin(abs)
	written, dirsCreated, err := envWriteAtomic(env, abs, a.Content)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	verified := verifyWritten(env, abs, a.Content)
	result := map[string]any{
		"bytes_written": written,
		"dirs_created":  dirsCreated,
		"verified":      verified,
		"resolved_path": abs,
	}
	if report := edit.Finish(ctx, a.Content); report != "" {
		result["lsp_diagnostics"] = report
	}
	stampWritten(env, abs, a.Content)
	return jsonResult(result)
}

// changeRefusal is why the existing file abs (shown as the caller named it)
// may not be changed now, or "": this session must have read it with Read,
// all of it when whole (Write replaces every line), and the file must still
// hold the bytes that read saw.
func changeRefusal(env *Env, abs, shown string, whole bool) string {
	verb := "edit it"
	if whole {
		verb = "write over it"
	}
	st, ok := env.State.readStamp(abs)
	if !ok {
		return shown + " has not been read in this session. Read it first, then " + verb + "."
	}
	data, err := envReadFile(env, abs)
	if err != nil {
		return "" // gone or unreadable: the write or edit itself reports it
	}
	if int64(len(data)) != st.size || sha256.Sum256(data) != st.sum {
		return shown + " has changed on disk since this session read it. Read it again, then " + verb + "."
	}
	if whole && !st.covers() {
		return fmt.Sprintf("%s has been read only in part (%s of its %d lines). Read the rest of it before replacing the whole file, "+
			"or change just the part you need with Edit.", shown, linesSeen(st.seen), st.lines)
	}
	return ""
}

// linesSeen names line ranges as a reader counts them: "lines 1-2000 and
// 4001-4200".
func linesSeen(rs []lineRange) string {
	if len(rs) == 0 {
		return "no lines"
	}
	parts := make([]string, len(rs))
	for i, r := range rs {
		parts[i] = strconv.Itoa(r.start+1) + "-" + strconv.Itoa(r.end)
	}
	if len(parts) == 1 {
		return "lines " + parts[0]
	}
	return "lines " + strings.Join(parts[:len(parts)-1], ", ") + " and " + parts[len(parts)-1]
}

// stampWritten records that the session knows all of abs: it just wrote
// content there.
func stampWritten(env *Env, abs, content string) {
	env.State.setStamp(abs, readStamp{size: int64(len(content)), sum: sha256.Sum256([]byte(content)),
		lines: len(splitLines(content)), whole: true})
}

// jsonHasStringField reports whether the raw arguments carry a string field.
func jsonHasStringField(raw json.RawMessage, field string) bool {
	var m map[string]json.RawMessage
	if err := json.Unmarshal(raw, &m); err != nil {
		return false
	}
	var s string
	v, ok := m[field]
	return ok && json.Unmarshal(v, &s) == nil
}

// readEcho reports whether content is Read's display of a file rather than a
// file's text: two or more lines numbered consecutively as Read shows them,
// either as content itself or as the string content of a JSON object (Read's
// whole result). A model that copies a read back into Write saves the numbers
// into every line, once per round trip.
func readEcho(content string) bool {
	var wrapped struct {
		Content *string `json:"content"`
	}
	if json.Unmarshal([]byte(content), &wrapped) == nil && wrapped.Content != nil && numberedLines(*wrapped.Content) {
		return true
	}
	return numberedLines(content)
}

func numberedLines(s string) bool {
	lines := strings.Split(strings.TrimRight(s, "\n"), "\n")
	if len(lines) < 2 {
		return false
	}
	first := 0
	for i, line := range lines {
		n, ok := shownNumber(line)
		if !ok {
			return false
		}
		if i == 0 {
			first = n
		} else if n != first+i {
			return false
		}
	}
	return true
}

// shownNumber is the line number a Read display puts in front of line: cat
// -n's right-aligned "%6d\t", or "N|" as the read tool showed lines before it
// took Read's shape. A tab-separated file whose first column counts from 1
// is not mistaken for one, since cat -n pads the number to six columns.
func shownNumber(line string) (int, bool) {
	if num, _, ok := strings.Cut(line, "\t"); ok {
		if n, err := strconv.Atoi(strings.TrimLeft(num, " ")); err == nil && n >= 1 && num == fmt.Sprintf("%6d", n) {
			return n, true
		}
	}
	if num, _, ok := strings.Cut(line, "|"); ok {
		if n, err := strconv.Atoi(num); err == nil && n >= 1 && num == strconv.Itoa(n) {
			return n, true
		}
	}
	return 0, false
}

// writeAtomic writes content to abs via a temp file + rename, creating parent
// directories (0755) as needed and preserving an existing file's mode.
// It returns the bytes written and whether the target is new.
func writeAtomic(abs, content string) (int, bool, error) {
	parent := filepath.Dir(abs)
	dirsCreated := false
	if _, err := os.Stat(parent); os.IsNotExist(err) {
		dirsCreated = true
	}
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return 0, false, err
	}
	mode := os.FileMode(0o644)
	if fi, err := os.Stat(abs); err == nil {
		mode = fi.Mode().Perm()
	} else if !os.IsNotExist(err) {
		return 0, false, err
	}
	tmp, err := os.CreateTemp(parent, ".soa-d-write-*")
	if err != nil {
		return 0, false, err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.WriteString(content); err != nil {
		tmp.Close()
		return 0, false, err
	}
	if err := tmp.Chmod(mode); err != nil {
		tmp.Close()
		return 0, false, err
	}
	if err := tmp.Close(); err != nil {
		return 0, false, err
	}
	if err := os.Rename(tmpName, abs); err != nil {
		return 0, false, err
	}
	return len(content), dirsCreated, nil
}

// verifyWritten re-reads the file and compares its hash to the written
// content.
func verifyWritten(env *Env, abs, content string) bool {
	data, err := envReadFile(env, abs)
	if err != nil {
		return false
	}
	want := sha256.Sum256([]byte(content))
	got := sha256.Sum256(data)
	return hex.EncodeToString(want[:]) == hex.EncodeToString(got[:])
}
