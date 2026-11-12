package tools

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

type writeFileTool struct{}

func (writeFileTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "write_file",
		Description: "Save text as a file, overwriting whatever the file held. Missing parent directories are created. " +
			"To change part of an existing file, use patch instead.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "path": {"type": "string", "description": "Where to write: absolute, relative to the working directory, or starting with ~/."},
    "content": {"type": "string", "description": "The full new contents of the file."}
  },
  "required": ["path", "content"]
}`),
	}}
}

type writeFileArgs struct {
	Path    string `json:"path"`
	Content string `json:"content"`
}

func (writeFileTool) Run(ctx context.Context, env *Env, args json.RawMessage) (string, error) {
	var a writeFileArgs
	if err := json.Unmarshal(args, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	if a.Path == "" {
		if jsonHasStringField(json.RawMessage(a.Content), "path") {
			return errorResult("path is required. The content you gave is itself a JSON object holding path and content: " +
				"pass path and content as write_file's own arguments, with content as the file's text alone."), nil
		}
		return errorResult("path is required"), nil
	}
	if a.Content == "" && !jsonHasStringField(args, "content") {
		return errorResult("content is required"), nil
	}
	if readFileEcho(a.Content) {
		return errorResult("Nothing was written: this content is read_file's output, not the file's text. Its lines start " +
			"with the line numbers and '|' that read_file adds when it shows a file. Write the text without them, or use " +
			"patch to add or change only the lines that change."), nil
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
	edit := env.LSP.Begin(abs)
	warning := stalenessWarning(env, abs)
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
	if warning != "" {
		result["_warning"] = warning
	}
	if report := edit.Finish(ctx, a.Content); report != "" {
		result["lsp_diagnostics"] = report
	}
	refreshStamp(env, abs)
	return jsonResult(result)
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

// readFileEcho reports whether content is read_file's display of a file rather
// than a file's text: two or more lines numbered consecutively as read_file
// prints them ("12|..."), either as content itself or as the string content
// of a JSON object (read_file's whole result). A model that copies a read back
// into write_file saves the numbers into every line, once per round trip.
func readFileEcho(content string) bool {
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
		num, _, ok := strings.Cut(line, "|")
		n, err := strconv.Atoi(num)
		if !ok || err != nil || num != strconv.Itoa(n) || n < 1 {
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

// refreshStamp records the new on-disk state after a write.
func refreshStamp(env *Env, abs string) {
	fi, err := envStat(env, abs)
	if err != nil {
		return
	}
	env.State.SetReadStamp(abs, readStamp{mtime: fi.ModTime().UnixNano(), size: fi.Size()})
}

// stalenessWarning checks the read stamp against the current file state.
func stalenessWarning(env *Env, abs string) string {
	st, ok := env.State.ReadStamp(abs)
	if !ok {
		return ""
	}
	fi, err := envStat(env, abs)
	if err != nil {
		return ""
	}
	if st.partial {
		return "you had read only part of this file; read all of it before assuming you know what it holds now"
	}
	if fi.ModTime().UnixNano() != st.mtime || fi.Size() != st.size {
		return "the file was modified after you last read it; this write went through anyway, so read it again to confirm the outcome"
	}
	return ""
}
