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
	"strings"
	"sync"
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
		Description: "Execute a shell command, starting in the session's working directory. " +
			"A cd, or an export, carries over to the next call. " +
			"To leave something running, set background; a trailing &, nohup or setsid is not allowed. " +
			"Reading, writing and searching files have their own tools, so keep this one for builds, installs, git, " +
			"running programs and scripts.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "command": {"type": "string", "description": "The command line, run by bash."},
    "timeout": {"type": "integer", "description": "How many seconds to wait before giving up: 180 unless set, 600 at most. Anything slower belongs in background mode."},
    "workdir": {"type": "string", "description": "Directory to run in, for this call only. The session's working directory stays as it was."},
    "background": {"type": "boolean", "description": "Start the command and return at once with a session id; follow it up with the process tool."},
    "notify_on_complete": {"type": "boolean", "description": "With background: send a message when the process exits."}
  },
  "required": ["command"]
}`),
	}}
}

var (
	// A command that ends in a lone & (not &&) backgrounds itself.
	trailingAmpRe = regexp.MustCompile(`(^|[^&])&\s*$`)
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
	Notify     bool   `json:"notify_on_complete"`
}

func (terminalTool) Run(ctx context.Context, env *Env, args json.RawMessage) (string, error) {
	var a terminalArgs
	if err := json.Unmarshal(args, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	if a.Command == "" {
		return errorResult("no command given"), nil
	}
	timeout := terminalDefaultTimeout
	if a.Timeout != nil {
		if *a.Timeout <= 0 {
			return errorResult("timeout has to be a number of seconds above zero"), nil
		}
		timeout = *a.Timeout
	}
	if timeout > terminalMaxTimeout {
		return errorResult(fmt.Sprintf("a foreground command may wait at most %d seconds; use background for longer work", terminalMaxTimeout)), nil
	}
	if a.Workdir != "" && !workdirRe.MatchString(a.Workdir) {
		return errorResult("workdir must be a plain path with no shell special characters"), nil
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
	if a.Background {
		return runBackground(env, cwd, a)
	}
	out, newCwd, code, done := runShell(ctx, env.State.SnapshotPath(), cwd, a.Command, timeout, env.Model, env.CommandHome)
	out = stripANSI(out)
	out = strings.TrimSpace(out)

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
		return fmt.Errorf("a trailing & is not allowed: run the command normally, or set background")
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

// withHome sets HOME in environ when home is not "" (the last value of a
// variable is the one a process gets).
func withHome(environ []string, home string) []string {
	if home == "" {
		return environ
	}
	return append(environ, "HOME="+home)
}

// runShell runs one command in a throwaway bash that sources the session's
// environment snapshot, and captures the post-command cwd and environment.
// It returns the output (stdout+stderr merged), the new cwd (empty when the
// command exited before the marker), the exit code, and whether the marker
// was seen.
func runShell(ctx context.Context, snapshot, cwd, command string, timeoutSeconds int, model, home string) (string, string, int, bool) {
	nonce := newNonce()
	wrapper := buildWrapper(snapshot, cwd, nonce)

	cmd := exec.Command("bash", "-c", wrapper)
	cmd.Dir = cwd
	cmd.Env = withHome(append(append([]string{}, os.Environ()...), "SOA_D_CMD="+command, "SOA_MODEL="+model), home)
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
	deadline := time.AfterFunc(time.Duration(timeoutSeconds)*time.Second, func() {
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
			return before + "\n[command timed out after " + fmt.Sprint(timeoutSeconds) + "s]", "", 124, false
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
