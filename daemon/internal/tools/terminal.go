package tools

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

const (
	terminalDefaultTimeout = 180
	terminalMaxTimeout     = 600
	terminalMaxOutput      = 50_000
)

type terminalTool struct{}

func (terminalTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "terminal",
		Description: "Run a shell command in the session's working directory. " +
			"The working directory and exported environment variables persist across calls. " +
			"Use background processes only through the background parameter; do not background " +
			"with &, nohup or setsid. Prefer the file tools for reading, writing and searching; " +
			"use the terminal for builds, installs, git, processes and scripts.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "command": {"type": "string", "description": "The shell command to run."},
    "timeout": {"type": "integer", "description": "Seconds to wait (default 180, max 600). Long-running work must use background=true."},
    "workdir": {"type": "string", "description": "Run in this directory for this call only; the session cwd is unchanged."},
    "background": {"type": "boolean", "description": "Run in the background and return a session id (not available in this build)."}
  },
  "required": ["command"]
}`),
	}}
}

var (
	trailingAmpRe = regexp.MustCompile(`(^|\s)&(\s|$)`)
	// workdir is a plain path: no shell metacharacters allowed.
	workdirRe = regexp.MustCompile(`^[A-Za-z0-9/._~@=,:-]+$`)
	// ANSI escape sequences (CSI, OSC, other single-character escapes).
	ansiRe = regexp.MustCompile("\x1b\\[[0-?]*[ -/]*[@-~]|\x1b\\][^\x07]*\x07|\x1b[@-Z\\-_]")
)

type terminalArgs struct {
	Command    string `json:"command"`
	Timeout    *int   `json:"timeout"`
	Workdir    string `json:"workdir"`
	Background bool   `json:"background"`
}

func (terminalTool) Run(ctx context.Context, env *Env, args json.RawMessage) (string, error) {
	var a terminalArgs
	if err := json.Unmarshal(args, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	if a.Command == "" {
		return errorResult("command is required"), nil
	}
	timeout := terminalDefaultTimeout
	if a.Timeout != nil {
		if *a.Timeout <= 0 {
			return errorResult("timeout must be a positive number of seconds"), nil
		}
		timeout = *a.Timeout
	}
	if timeout > terminalMaxTimeout {
		return errorResult(fmt.Sprintf("foreground timeout is capped at %d seconds; long-running work must use background=true", terminalMaxTimeout)), nil
	}
	if a.Background {
		return errorResult("background mode is not available in this build; run the command in the foreground"), nil
	}
	if a.Workdir != "" && !workdirRe.MatchString(a.Workdir) {
		return errorResult("workdir must be a plain path (no shell metacharacters)"), nil
	}
	if err := refuseBackgrounding(a.Command); err != nil {
		return errorResult(err.Error()), nil
	}
	if v := env.Gate.Command(ctx, a.Command); !v.Allowed {
		return jsonResult(map[string]any{
			"error": v.Message, "status": "blocked", "exit_code": -1, "output": "",
		})
	}

	cwd := env.State.Cwd()
	if a.Workdir != "" {
		cwd = a.Workdir
	}
	out, newCwd, code, done := runShell(ctx, env.State.SnapshotPath(), cwd, a.Command, timeout)
	out = stripANSI(out)
	out = strings.TrimSpace(out)
	out = truncateOutput(out)

	result := map[string]any{"output": out, "exit_code": code}
	if done && newCwd != "" && a.Workdir == "" && newCwd != env.State.Cwd() {
		env.State.SetCwd(newCwd)
		result["cwd"] = newCwd
	}
	return jsonResult(result)
}

// refuseBackgrounding rejects commands that try to outlive the call.
func refuseBackgrounding(command string) error {
	if trailingAmpRe.MatchString(command) {
		return fmt.Errorf("backgrounding with & is refused: run the command in the foreground, or use background mode when it is available")
	}
	for _, seg := range strings.FieldsFunc(command, func(r rune) bool {
		return r == ';' || r == '&' || r == '|' || r == '\n'
	}) {
		seg = strings.TrimSpace(seg)
		if seg == "" {
			continue
		}
		first := seg
		if i := strings.IndexAny(first, " \t"); i >= 0 {
			first = first[:i]
		}
		base := first
		if i := strings.LastIndex(base, "/"); i >= 0 {
			base = base[i+1:]
		}
		switch base {
		case "nohup", "setsid", "disown":
			return fmt.Errorf("%s is refused: backgrounding must use the background mode, not %s", base, base)
		}
	}
	return nil
}

// runShell runs one command in a throwaway bash that sources the session's
// environment snapshot, and captures the post-command cwd and environment.
// It returns the output (stdout+stderr merged), the new cwd (empty when the
// command exited before the marker), the exit code, and whether the marker
// was seen.
func runShell(ctx context.Context, snapshot, cwd, command string, timeoutSeconds int) (string, string, int, bool) {
	nonce := newNonce()
	wrapper := buildWrapper(snapshot, cwd, nonce)

	cmd := exec.Command("bash", "-c", wrapper)
	cmd.Dir = cwd
	cmd.Env = append(append([]string{}, os.Environ()...), "SOA_D_CMD="+command)
	cmd.Stdin = openNull()
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	if err := cmd.Start(); err != nil {
		return "failed to start bash: " + err.Error(), "", 126, false
	}

	waitCh := make(chan error, 1)
	go func() { waitCh <- cmd.Wait() }()

	var timedOut int32
	deadline := time.AfterFunc(time.Duration(timeoutSeconds)*time.Second, func() {
		atomic.StoreInt32(&timedOut, 1)
		killGroup(cmd)
	})
	select {
	case <-ctx.Done():
		deadline.Stop()
		killGroup(cmd)
		<-waitCh
		out := buf.String()
		return out + "\n[Command interrupted]", "", 130, false
	case <-waitCh:
		deadline.Stop()
	}

	out := buf.String()
	markerIdx := findMarkerLine(out, nonce)
	if markerIdx < 0 {
		if atomic.LoadInt32(&timedOut) == 1 {
			return out + "\n[Command timed out after " + fmt.Sprint(timeoutSeconds) + "s]", "", 124, false
		}
		return out, "", cmdExitCodeFromRun(cmd), false
	}
	before, rest := out[:markerIdx], out[markerIdx:]
	newCwd := ""
	// rest starts at the marker line; skip it, then the pwd line follows.
	if nl := strings.IndexByte(rest, '\n'); nl >= 0 {
		rest = rest[nl+1:]
	}
	if nl := strings.IndexByte(rest, '\n'); nl >= 0 {
		newCwd = strings.TrimSpace(rest[:nl])
		rest = rest[nl+1:]
	}
	return before, newCwd, cmdExitCodeFromRun(cmd), true
}

// cmdExitCodeFromRun recovers the exit status; the wait already completed.
func cmdExitCodeFromRun(cmd *exec.Cmd) int {
	if cmd.ProcessState == nil {
		return 1
	}
	return cmd.ProcessState.ExitCode()
}

func buildWrapper(snapshot, cwd, nonce string) string {
	return strings.Join([]string{
		"set +e",
		fmt.Sprintf("[ -f %s ] && . %s", shQuote(snapshot), shQuote(snapshot)),
		fmt.Sprintf("builtin cd -- %s || exit 126", shQuote(cwd)),
		`eval "$SOA_D_CMD"`,
		"__soa_rc=$?",
		"unset SOA_D_CMD",
		fmt.Sprintf("export -p > %s && mv -f %s %s", shQuote(snapshot+".tmp"), shQuote(snapshot+".tmp"), shQuote(snapshot)),
		// Leading newline keeps the nonce on its own line even when the
		// command's output has no trailing newline.
		fmt.Sprintf("printf '\\n%%s\\n' %s", shQuote(nonce)),
		"pwd -P",
		"exit $__soa_rc",
	}, "\n")
}

func findMarkerLine(out, nonce string) int {
	rest := out
	off := 0
	for {
		lineEnd := strings.IndexByte(rest, '\n')
		var line string
		if lineEnd < 0 {
			line = rest
		} else {
			line = rest[:lineEnd]
		}
		if strings.TrimSpace(line) == nonce {
			return off
		}
		if lineEnd < 0 {
			break
		}
		rest = rest[lineEnd+1:]
		off += lineEnd + 1
	}
	return -1
}

func newNonce() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return "soa" + hex.EncodeToString(b)
}

func killGroup(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
}

func shQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'"
}

func stripANSI(s string) string {
	return ansiRe.ReplaceAllString(s, "")
}

// truncateOutput caps output at 50,000 chars, keeping a 40% head and a 60%
// tail around a notice.
func truncateOutput(out string) string {
	if len(out) <= terminalMaxOutput {
		return out
	}
	head := terminalMaxOutput * 2 / 5
	tail := terminalMaxOutput - head
	notice := fmt.Sprintf("\n\n... [OUTPUT TRUNCATED - %d chars omitted out of %d total] ...\n\n", len(out)-head-tail, len(out))
	return out[:head] + notice + out[len(out)-tail:]
}
