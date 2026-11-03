package tools

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"

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
		return errorResult("path is required"), nil
	}
	if a.Content == "" && !jsonHasStringField(args, "content") {
		return errorResult("content is required"), nil
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
	written, dirsCreated, err := writeAtomic(abs, a.Content)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	verified := verifyWritten(abs, a.Content)
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
func verifyWritten(abs, content string) bool {
	data, err := os.ReadFile(abs)
	if err != nil {
		return false
	}
	want := sha256.Sum256([]byte(content))
	got := sha256.Sum256(data)
	return hex.EncodeToString(want[:]) == hex.EncodeToString(got[:])
}

// refreshStamp records the new on-disk state after a write.
func refreshStamp(env *Env, abs string) {
	fi, err := os.Stat(abs)
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
	fi, err := os.Stat(abs)
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
