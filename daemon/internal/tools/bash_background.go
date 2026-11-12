package tools

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os/exec"
	"regexp"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

const (
	procBufferMax     = 200_000
	procKillGrace     = 2 * time.Second
	procExitedChars   = 2000
	procCommandPeek   = 200
	procMinPrefixSize = 4
)

// bgProc is one background process owned by a State. Fields after done are
// guarded by the owning State's mutex.
type bgProc struct {
	id, command string
	pid         int
	started     time.Time
	cmd         *exec.Cmd
	buf         *rollingBuffer
	done        chan struct{}
	notify      bool

	exited   bool
	exitCode int
	ended    time.Time
	consumed bool
	killed   bool
}

// note is a queued completion notice for a process.
type note struct{ id, text string }

func (p *bgProc) signal(sig syscall.Signal) {
	_ = syscall.Kill(-p.pid, sig)
}

// rollingBuffer keeps the most recent procBufferMax bytes written to it, the
// count of all the bytes ever written, and how far BashOutput has read.
type rollingBuffer struct {
	mu    sync.Mutex
	b     []byte
	total int64
	read  int64
}

func (r *rollingBuffer) Write(p []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.b = append(r.b, p...)
	r.total += int64(len(p))
	if len(r.b) > 2*procBufferMax {
		r.b = append(r.b[:0], r.b[len(r.b)-procBufferMax:]...)
	}
	return len(p), nil
}

// String is the retained output, never starting inside a UTF-8 sequence.
func (r *rollingBuffer) String() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	b := r.b
	if len(b) > procBufferMax {
		b = b[len(b)-procBufferMax:]
	}
	for len(b) > 0 && !utf8.RuneStart(b[0]) {
		b = b[1:]
	}
	return string(b)
}

// unread returns what was written since the last call and moves the read
// mark past it, with how many bytes of that the buffer had already dropped
// (it keeps only the latest). Unless final, a character still being written
// waits for the next call.
func (r *rollingBuffer) unread(final bool) (string, int64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	base := r.total - int64(len(r.b)) // where r.b starts in the whole output
	from, dropped := r.read, int64(0)
	if from < base {
		dropped, from = base-from, base
	}
	start, end := int(from-base), len(r.b)
	if keep := end - procBufferMax; start < keep {
		dropped += int64(keep - start)
		start = keep
	}
	if dropped > 0 {
		for start < end && !utf8.RuneStart(r.b[start]) {
			start++
			dropped++
		}
	}
	for i := 1; !final && i < utf8.UTFMax && i <= end-start; i++ {
		if utf8.RuneStart(r.b[end-i]) {
			if !utf8.FullRune(r.b[end-i : end]) {
				end -= i
			}
			break
		}
	}
	r.read = base + int64(end)
	return string(r.b[start:end]), dropped
}

func lastChars(s string, n int) string {
	if r := []rune(s); len(r) > n {
		return string(r[len(r)-n:])
	}
	return s
}

func cleanOutput(s string, n int) string {
	return strings.TrimSpace(lastChars(stripANSI(s), n))
}

// startBackground launches command in its own process group and registers it
// with the session state. Its end queues a receipt when this run can deliver
// one.
func startBackground(env *Env, cwd, command string) (string, error) {
	st := env.State
	idb := make([]byte, 6)
	_, _ = rand.Read(idb)
	p := &bgProc{
		id:      "bash_" + hex.EncodeToString(idb),
		command: command,
		started: time.Now(),
		buf:     &rollingBuffer{},
		done:    make(chan struct{}),
		notify:  env.AsyncDelivery, // recorded before the process starts
	}
	script := strings.Join([]string{
		"set +e",
		fmt.Sprintf("[ -f %s ] && . %s", shQuote(st.SnapshotPath()), shQuote(st.SnapshotPath())),
		fmt.Sprintf("builtin cd -- %s || exit 126", shQuote(cwd)),
		`eval "$SOA_D_CMD"`,
	}, "\n")
	cmd, err := shellCmd(env, script, cwd, command)
	if err != nil {
		return "", err
	}
	cmd.Stdin = openNull()
	cmd.Stdout, cmd.Stderr = p.buf, p.buf
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	// A child that inherited the output pipe may outlive the shell.
	cmd.WaitDelay = 2 * time.Second
	p.cmd = cmd

	st.mu.Lock()
	if st.closed {
		st.mu.Unlock()
		return "", fmt.Errorf("the session is closed")
	}
	if err := cmd.Start(); err != nil {
		st.mu.Unlock()
		return "", fmt.Errorf("could not start bash: %w", err)
	}
	p.pid = cmd.Process.Pid
	st.procs[p.id] = p
	st.mu.Unlock()

	go func() {
		_ = cmd.Wait()
		code := cmd.ProcessState.ExitCode()
		if ws, ok := cmd.ProcessState.Sys().(syscall.WaitStatus); ok && ws.Signaled() {
			code = -int(ws.Signal())
		}
		st.mu.Lock()
		p.exited, p.exitCode, p.ended = true, code, time.Now()
		close(p.done)
		queued := p.notify && !p.consumed
		if queued {
			st.notes = append(st.notes, note{p.id, fmt.Sprintf(
				"[background receipt] process %s exited with code %d\ncommand: %s\noutput (last %d characters):\n%s",
				p.id, code, p.command, procExitedChars, cleanOutput(p.buf.String(), procExitedChars))})
		}
		cb, exit := st.onNote, st.onExit
		st.mu.Unlock()
		if queued && cb != nil {
			cb()
		}
		if exit != nil {
			exit()
		}
	}()
	return p.id, nil
}

// consume marks a finished process's result as read and withdraws its queued
// notice.
func (s *State) consume(p *bgProc) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p.consumed = true
	kept := s.notes[:0]
	for _, n := range s.notes {
		if n.id != p.id {
			kept = append(kept, n)
		}
	}
	s.notes = kept
}

// TakeNotifications drains the queued completion notices.
func (s *State) TakeNotifications() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []string
	for _, n := range s.notes {
		out = append(out, n.text)
	}
	s.notes = nil
	return out
}

// lookupProc resolves an id or a unique prefix of 4+ characters. The error for
// an id that fits none lists the session's background shells, so a model that
// lost track of them finds them again.
func (s *State) lookupProc(ref string) (*bgProc, error) {
	ref = strings.TrimSpace(ref)
	short := strings.TrimPrefix(ref, "bash_")
	s.mu.Lock()
	defer s.mu.Unlock()
	if p, ok := s.procs["bash_"+short]; ok && short != "" {
		return p, nil
	}
	if len(short) >= procMinPrefixSize {
		var found []*bgProc
		for id, p := range s.procs {
			if strings.HasPrefix(id, "bash_"+short) {
				found = append(found, p)
			}
		}
		if len(found) == 1 {
			return found[0], nil
		}
		if len(found) > 1 {
			return nil, fmt.Errorf("%q fits more than one background shell; use more of the id", ref)
		}
	}
	msg := "there is no background shell with id " + ref
	if ref == "" {
		msg = "give the bash_id that Bash returned"
	}
	ps := make([]*bgProc, 0, len(s.procs))
	for _, p := range s.procs {
		ps = append(ps, p)
	}
	sort.Slice(ps, func(i, j int) bool { return ps[i].started.Before(ps[j].started) })
	if len(ps) == 0 {
		return nil, fmt.Errorf("%s; this session has started none", msg)
	}
	lines := make([]string, len(ps))
	for i, p := range ps {
		lines[i] = fmt.Sprintf("%s (%s): %s", p.id, procStatus(p.exited, p.killed, p.exitCode), lastCharsHead(p.command, procCommandPeek))
	}
	return nil, fmt.Errorf("%s; this session's background shells are:\n%s", msg, strings.Join(lines, "\n"))
}

func (s *State) snapshotProc(p *bgProc) (exited, killed bool, code int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return p.exited, p.killed, p.exitCode
}

// procStatus names a background shell's state: running, completed (exit 0),
// failed, or killed (by KillShell or the session's end).
func procStatus(exited, killed bool, code int) string {
	switch {
	case !exited:
		return "running"
	case killed:
		return "killed"
	case code == 0:
		return "completed"
	}
	return "failed"
}

func lastCharsHead(s string, n int) string {
	if r := []rune(s); len(r) > n {
		return string(r[:n])
	}
	return s
}

// runBackground is Bash with run_in_background, after the gate has allowed
// the command. Its end is announced by a receipt where one can be delivered.
func runBackground(env *Env, cwd, command string) (string, error) {
	id, err := startBackground(env, cwd, command)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	p, _ := env.State.lookupProc(id)
	out := map[string]any{"bash_id": id, "status": "running", "pid": p.pid,
		"note": "Started in the background. BashOutput with this bash_id shows its output; a message arrives here when it ends."}
	if !env.AsyncDelivery {
		out["note"] = "Started in the background. Nothing can tell you when it ends in this run, so check on it with BashOutput."
	}
	return jsonResult(out)
}

type bashOutputTool struct{}

func (bashOutputTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "BashOutput",
		Description: "Look at a command started with Bash's run_in_background: what it has printed since you last looked (standard output and error together), " +
			"whether it is still running, and its exit code once it has ended. Each call shows only output that is new. " +
			"With filter, only the new lines matching that regular expression are shown; the others are passed over for good. " +
			"After a command has ended, reading it here also takes the place of the message that says it ended.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "bash_id": {"type": "string", "description": "The bash_id Bash returned for the command."},
    "filter": {"type": "string", "description": "A regular expression: show only the new lines that match it."}
  },
  "required": ["bash_id"]
}`),
	}}
}

func (bashOutputTool) Run(_ context.Context, env *Env, raw json.RawMessage) (string, error) {
	var a struct {
		BashID string `json:"bash_id"`
		Filter string `json:"filter"`
	}
	if err := json.Unmarshal(raw, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	var filter *regexp.Regexp
	if a.Filter != "" {
		re, err := regexp.Compile(a.Filter)
		if err != nil {
			return errorResult("filter is not a regular expression: " + err.Error()), nil
		}
		filter = re
	}
	st := env.State
	p, err := st.lookupProc(a.BashID)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	exited, killed, code := st.snapshotProc(p)
	text, dropped := p.buf.unread(exited)
	text = strings.TrimRight(stripANSI(text), "\n")
	if filter != nil && text != "" {
		var kept []string
		for _, line := range strings.Split(text, "\n") {
			if filter.MatchString(line) {
				kept = append(kept, line)
			}
		}
		text = strings.Join(kept, "\n")
	}
	if len(text) > terminalMaxOutput {
		text = cutMiddle(text)
	}
	out := map[string]any{"bash_id": p.id, "status": procStatus(exited, killed, code), "output": text}
	if exited {
		out["exit_code"] = code
		st.consume(p)
	}
	if dropped > 0 {
		out["note"] = fmt.Sprintf("%d bytes it printed before this output were dropped unread: only the latest %d are kept.", dropped, procBufferMax)
	}
	return jsonResult(out)
}

// cutMiddle keeps the head and tail of text around a notice of what was left
// out.
func cutMiddle(text string) string {
	head, tail := text[:terminalHeadKeep], text[len(text)-terminalTailKeep:]
	return head + fmt.Sprintf("\n\n... [output cut: %d of %d chars omitted] ...\n\n", len(text)-len(head)-len(tail), len(text)) + tail
}

type killShellTool struct{}

func (killShellTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "KillShell",
		Description: "Stop a command started with Bash's run_in_background, along with everything it started. " +
			"What it printed stays readable with BashOutput.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "shell_id": {"type": "string", "description": "The bash_id of the background command to stop."}
  },
  "required": ["shell_id"]
}`),
	}}
}

func (killShellTool) Run(_ context.Context, env *Env, raw json.RawMessage) (string, error) {
	var a struct {
		ShellID string `json:"shell_id"`
	}
	if err := json.Unmarshal(raw, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	p, err := env.State.lookupProc(a.ShellID)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	return killProc(env.State, p)
}

func killProc(st *State, p *bgProc) (string, error) {
	st.mu.Lock()
	if p.exited {
		code := p.exitCode
		st.mu.Unlock()
		st.consume(p)
		return jsonResult(map[string]any{"shell_id": p.id, "status": "already_exited", "exit_code": code})
	}
	p.killed = true
	st.mu.Unlock()
	p.signal(syscall.SIGTERM)
	select {
	case <-p.done:
	case <-time.After(procKillGrace):
	}
	// Whatever the shell left in the group goes too.
	p.signal(syscall.SIGKILL)
	select {
	case <-p.done:
	case <-time.After(procKillGrace):
	}
	st.consume(p)
	_, _, code := st.snapshotProc(p)
	return jsonResult(map[string]any{"shell_id": p.id, "status": "killed", "exit_code": code, "command": lastCharsHead(p.command, procCommandPeek)})
}
