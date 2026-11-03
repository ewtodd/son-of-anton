package cron

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"
)

// ScriptTimeout is how long a job's script may run.
var ScriptTimeout = 3600 * time.Second

// keepOutputs is how many saved runs a job keeps.
const keepOutputs = 50

// ScriptPath resolves a job's script under <home>/scripts. A path that
// leaves that directory, by ".." or a symlink, is refused.
func ScriptPath(home, script string) (string, error) {
	if strings.ContainsRune(script, 0) {
		return "", fmt.Errorf("script path %q contains a NUL byte", script)
	}
	dir := filepath.Join(home, "scripts")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	root, err := filepath.EvalSymlinks(dir)
	if err != nil {
		return "", err
	}
	path := script
	if !filepath.IsAbs(path) {
		path = filepath.Join(dir, path)
	}
	if resolved, err := filepath.EvalSymlinks(path); err == nil {
		path = resolved
	} else {
		path = filepath.Clean(path)
	}
	if rel, err := filepath.Rel(root, path); err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("script %q resolves to a place outside the scripts directory %s", script, root)
	}
	return path, nil
}

// RunScript runs a job's script in workdir (the home when "") and returns
// its stdout, trimmed. ok is false on any failure, and out then says what
// went wrong: that text is meant for the model or the chat.
func RunScript(ctx context.Context, home, script, workdir string) (out string, ok bool) {
	path, err := ScriptPath(home, script)
	if err != nil {
		return err.Error(), false
	}
	fi, err := os.Stat(path)
	if err != nil {
		return "No script at " + path, false
	}
	if !fi.Mode().IsRegular() {
		return "The script path is not a regular file: " + path, false
	}
	argv := []string{"python3", path}
	if py := os.Getenv("SON_OF_ANTON_PYTHON"); py != "" {
		argv[0] = py
	}
	if ext := strings.ToLower(filepath.Ext(path)); ext == ".sh" || ext == ".bash" {
		argv = []string{"bash", path}
	}
	if workdir == "" {
		workdir = home
	}
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Dir = workdir
	cmd.Env = scriptEnv(home)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Start(); err != nil {
		return "Could not start the script: " + err.Error(), false
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	kill := func() {
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		<-done
	}
	timer := time.NewTimer(ScriptTimeout)
	defer timer.Stop()
	select {
	case err = <-done:
	case <-timer.C:
		kill()
		return fmt.Sprintf("The script ran longer than %ds and was stopped: %s", int(ScriptTimeout.Seconds()), path), false
	case <-ctx.Done():
		kill()
		return "The script was cancelled: " + ctx.Err().Error(), false
	}
	text, errText := strings.TrimSpace(stdout.String()), strings.TrimSpace(stderr.String())
	if err != nil {
		var exit *exec.ExitError
		if !errors.As(err, &exit) {
			return "The script could not be run: " + err.Error(), false
		}
		parts := []string{fmt.Sprintf("The script finished with exit code %d", exit.ExitCode())}
		if errText != "" {
			parts = append(parts, "stderr:\n"+errText)
		}
		if text != "" {
			parts = append(parts, "stdout:\n"+text)
		}
		return strings.Join(parts, "\n"), false
	}
	return text, true
}

// scriptEnv is the environment a script gets: the daemon's, without
// credentials.
func scriptEnv(home string) []string {
	var env []string
	for _, kv := range os.Environ() {
		name, _, _ := strings.Cut(kv, "=")
		upper := strings.ToUpper(name)
		secret := false
		for _, suffix := range []string{"_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIAL", "_CREDENTIALS"} {
			secret = secret || strings.HasSuffix(upper, suffix)
		}
		if !secret {
			env = append(env, kv)
		}
	}
	return append(env, "SON_OF_ANTON_HOME="+home)
}

// WakeGate reads a script's last non-empty output line as the pre-run
// gate: JSON {"wakeAgent": false} means the run is silent.
func WakeGate(out string) (wake bool) {
	var last string
	for _, l := range strings.Split(out, "\n") {
		if strings.TrimSpace(l) != "" {
			last = strings.TrimSpace(l)
		}
	}
	var gate map[string]any
	if last == "" || json.Unmarshal([]byte(last), &gate) != nil {
		return true
	}
	v, ok := gate["wakeAgent"].(bool)
	return !ok || v
}

// SaveOutput writes a run's document to cron/output/<id>/<time>.md (0600)
// and keeps the newest 50 of the job.
func SaveOutput(home, id, doc string, at time.Time) error {
	if id == "" || strings.ContainsAny(id, "/\\") || id == "." || id == ".." {
		return fmt.Errorf("cron: unsafe job id %q", id)
	}
	if err := ensureDirs(home); err != nil {
		return err
	}
	dir := filepath.Join(Dir(home), "output", id)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".output_*.tmp")
	if err != nil {
		return err
	}
	fail := func(err error) error {
		_ = tmp.Close()
		_ = os.Remove(tmp.Name())
		return err
	}
	if _, err := tmp.WriteString(doc); err != nil {
		return fail(err)
	}
	if err := tmp.Sync(); err != nil {
		return fail(err)
	}
	if err := tmp.Close(); err != nil {
		return fail(err)
	}
	if err := os.Chmod(tmp.Name(), 0o600); err != nil {
		return fail(err)
	}
	if err := os.Rename(tmp.Name(), filepath.Join(dir, at.Format("2006-01-02_15-04-05")+".md")); err != nil {
		return fail(err)
	}
	files, err := filepath.Glob(filepath.Join(dir, "*.md"))
	if err != nil {
		return nil
	}
	sort.Sort(sort.Reverse(sort.StringSlice(files)))
	for _, old := range files[min(keepOutputs, len(files)):] {
		_ = os.Remove(old)
	}
	return nil
}
