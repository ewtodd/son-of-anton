package tools

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

const (
	bashDefaultTimeout = 180 * time.Second
	bashMaxTimeoutMS   = 600_000
	terminalMaxOutput  = 50_000
)

type bashTool struct{}

func (bashTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "Bash",
		Description: "Run a command with bash, in the session's working directory. The shell's state carries from call to call: " +
			"a cd moves the working directory, and an export stays set. " +
			"A command gets 3 minutes unless timeout says otherwise (in milliseconds, 10 minutes at most). " +
			"For anything slower, or meant to keep running, set run_in_background: the call returns at once with a bash_id, " +
			"BashOutput shows what the command has printed since you last looked, KillShell stops it, and a message arrives when it ends. " +
			"Do not background a command yourself with a trailing &, nohup or setsid. " +
			"Files have their own tools (Read, Write, Edit, Glob, Grep), so keep this one for builds, tests, git, " +
			"installs and running programs.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "command": {"type": "string", "description": "The command line to run."},
    "timeout": {"type": "integer", "description": "How long to wait for the command, in milliseconds: 180000 unless set, 600000 at most.", "maximum": 600000},
    "description": {"type": "string", "description": "A few words on what the command does, for whoever watches the session."},
    "run_in_background": {"type": "boolean", "description": "Start the command and return at once with a bash_id to read its output with BashOutput."}
  },
  "required": ["command"]
}`),
	}}
}

var (
	// A command that ends in a lone & (not &&) backgrounds itself.
	trailingAmpRe = regexp.MustCompile(`(^|[^&])&\s*$`)
	// ANSI escape sequences (CSI, OSC, other single-character escapes).
	ansiRe = regexp.MustCompile("\x1b\\[[0-?]*[ -/]*[@-~]|\x1b\\][^\x07]*\x07|\x1b[@-Z\\-_]")
)

type bashArgs struct {
	Command     string `json:"command"`
	Timeout     *int   `json:"timeout"`
	Description string `json:"description"`
	Background  bool   `json:"run_in_background"`
}

func (bashTool) Run(ctx context.Context, env *Env, args json.RawMessage) (string, error) {
	var a bashArgs
	if err := json.Unmarshal(args, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	if a.Command == "" {
		return errorResult("no command given"), nil
	}
	timeout := bashDefaultTimeout
	if a.Timeout != nil {
		if *a.Timeout <= 0 {
			return errorResult("timeout has to be a number of milliseconds above zero"), nil
		}
		if *a.Timeout > bashMaxTimeoutMS {
			return errorResult(fmt.Sprintf("timeout is %d ms, and a command may wait at most %d ms; set run_in_background for longer work", *a.Timeout, bashMaxTimeoutMS)), nil
		}
		timeout = time.Duration(*a.Timeout) * time.Millisecond
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
	if a.Background {
		return runBackground(env, cwd, a.Command)
	}
	nonce := newNonce()
	cmd, err := shellCmd(env, buildWrapper(env.State.SnapshotPath(), cwd, nonce), cwd, a.Command)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	out, newCwd, code, done := runShell(ctx, cmd, nonce, timeout)
	out = stripANSI(out)
	out = strings.TrimSpace(out)

	result := map[string]any{"output": out, "exit_code": code}
	if done && newCwd != "" && newCwd != env.State.Cwd() {
		env.State.SetCwd(newCwd)
		result["cwd"] = newCwd
	}
	return jsonResult(result)
}

// refuseBackgrounding rejects commands that try to outlive the call.
func refuseBackgrounding(command string) error {
	if trailingAmpRe.MatchString(command) {
		return fmt.Errorf("a trailing & is not allowed: run the command normally, or set run_in_background")
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
			return fmt.Errorf("%s is refused: set run_in_background to leave a command running, not %s", base, base)
		}
	}
	return nil
}

// withHome sets HOME in environ when home is not "" (the last value of a
// variable is the one a process gets).
func withHome(environ []string, home string) []string {
	if home == "" {
		return environ
	}
	return append(environ, "HOME="+home)
}

// shellCmd builds the process that runs script under bash with command in
// SOA_D_CMD. In a sandboxed session that is bwrap running the runtime's bash
// (the session's temp dir bound, so the environment snapshot works); when the
// sandbox is unavailable there is no process, never an unsandboxed one.
func shellCmd(env *Env, script, cwd, command string) (*exec.Cmd, error) {
	if env.Sandbox != nil {
		pol := *env.Sandbox
		pol.Binds = append(append([]string{}, pol.Binds...), env.State.TempDir())
		return pol.Command([]string{"SOA_D_CMD=" + command, "SOA_MODEL=" + env.Model}, pol.Bash(), "-c", script)
	}
	cmd := exec.Command("bash", "-c", script)
	cmd.Dir = cwd
	cmd.Env = withHome(append(append([]string{}, os.Environ()...), "SOA_D_CMD="+command, "SOA_MODEL="+env.Model), env.CommandHome)
	return cmd, nil
}

// runShell runs cmd, a shellCmd built around buildWrapper(.., nonce), and
// captures the post-command cwd. It returns the output (stdout+stderr merged),
// the new cwd (empty when the command exited before the marker), the exit
// code, and whether the marker was seen.
func runShell(ctx context.Context, cmd *exec.Cmd, nonce string, timeout time.Duration) (string, string, int, bool) {
	cmd.Stdin = openNull()
	buf := &boundedBuffer{}
	cmd.Stdout = buf
	cmd.Stderr = buf
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	// A background child that inherited stdout keeps the pipe open after the
	// shell exits; give it a moment, then stop waiting for the copy.
	cmd.WaitDelay = 2 * time.Second

	if err := cmd.Start(); err != nil {
		return "could not start bash: " + err.Error(), "", 126, false
	}

	waitCh := make(chan error, 1)
	go func() { waitCh <- cmd.Wait() }()

	var timedOut int32
	deadline := time.AfterFunc(timeout, func() {
		atomic.StoreInt32(&timedOut, 1)
		killGroup(cmd)
	})
	select {
	case <-ctx.Done():
		deadline.Stop()
		killGroup(cmd)
		<-waitCh
		before, rest := buf.result(nonce)
		return before + rest + "\n[command interrupted]", "", 130, false
	case <-waitCh:
		deadline.Stop()
		// A foreground call leaves nothing running: whatever the command
		// left behind in its process group goes with it.
		_ = killGroup(cmd)
	}

	before, rest := buf.result(nonce)
	if rest == "" {
		if atomic.LoadInt32(&timedOut) == 1 {
			return before + "\n[command timed out after " + fmtWait(timeout) + "]", "", 124, false
		}
		return before, "", cmdExitCodeFromRun(cmd), false
	}
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
		"unset SOA_D_CMD SOA_MODEL", // never into the snapshot: the next call sets its own
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

// The output cap is split 40% head, 60% tail around a notice.
const (
	terminalHeadKeep = terminalMaxOutput * 2 / 5
	terminalTailKeep = terminalMaxOutput - terminalHeadKeep
	// The tail window also holds the marker block the wrapper prints at the
	// end, which is cut off before the tail is kept.
	terminalTailWindow = terminalTailKeep + 4096
)

// boundedBuffer collects a command's output without holding all of it: the
// first terminalHeadKeep bytes, a rolling window of the latest bytes, and the
// total count. Memory stays within a small multiple of the cap whatever the
// command prints.
type boundedBuffer struct {
	mu    sync.Mutex
	head  []byte
	tail  []byte
	total int
}

func (b *boundedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	n := len(p)
	b.total += n
	if room := terminalHeadKeep - len(b.head); room > 0 {
		take := min(room, len(p))
		b.head = append(b.head, p[:take]...)
		p = p[take:]
	}
	b.tail = append(b.tail, p...)
	if len(b.tail) > 2*terminalTailWindow {
		b.tail = append(b.tail[:0], b.tail[len(b.tail)-terminalTailWindow:]...)
	}
	return n, nil
}

// result splits the output at the wrapper's marker line: before is what the
// command printed (head, a notice, and the latest tail when it exceeded the
// cap), rest is the marker block and what follows, empty when the marker
// never came (timeout, kill).
func (b *boundedBuffer) result(nonce string) (before, rest string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	headStr, tailStr := string(b.head), string(b.tail)
	if b.total == len(b.head)+len(b.tail) {
		// Nothing was dropped: the output is whole, and the marker may lie
		// in either part.
		all := headStr + tailStr
		rest = ""
		if idx := findMarkerLine(all, nonce); idx >= 0 {
			all, rest = all[:idx], all[idx:]
		}
		if len(all) <= terminalMaxOutput {
			return all, rest
		}
		return b.truncated(all[:terminalHeadKeep], all[terminalHeadKeep:], len(all)), rest
	}
	if len(tailStr) > terminalTailWindow {
		tailStr = tailStr[len(tailStr)-terminalTailWindow:]
	}
	total := b.total
	if idx := findMarkerLine(tailStr, nonce); idx >= 0 {
		rest = tailStr[idx:]
		tailStr = tailStr[:idx]
		total -= len(rest)
	}
	return b.truncated(headStr, tailStr, total), rest
}

// truncated joins the head and the last part of the tail around the notice.
func (b *boundedBuffer) truncated(head, tail string, total int) string {
	if len(tail) > terminalTailKeep {
		tail = tail[len(tail)-terminalTailKeep:]
	}
	notice := fmt.Sprintf("\n\n... [output cut: %d of %d chars omitted] ...\n\n", total-len(head)-len(tail), total)
	return head + notice + tail
}

// fmtWait is a timeout as a person says it: "180s", "1.5s", "250ms".
func fmtWait(d time.Duration) string {
	if d < time.Second {
		return strconv.FormatInt(d.Milliseconds(), 10) + "ms"
	}
	return strconv.FormatFloat(d.Seconds(), 'f', -1, 64) + "s"
}
