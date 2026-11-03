package tools

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os/exec"
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
	procWaitDefault   = 180
	procWaitMax       = 600
	procLogDefault    = 200
	procKillGrace     = 2 * time.Second
	procPreviewChars  = 1000
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
}

// note is a queued completion notice for a process.
type note struct{ id, text string }

func (p *bgProc) signal(sig syscall.Signal) {
	_ = syscall.Kill(-p.pid, sig)
}

// rollingBuffer keeps the most recent procBufferMax bytes written to it.
type rollingBuffer struct {
	mu sync.Mutex
	b  []byte
}

func (r *rollingBuffer) Write(p []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.b = append(r.b, p...)
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
// with the session state.
func startBackground(env *Env, cwd, command string, notify bool) (string, error) {
	st := env.State
	idb := make([]byte, 6)
	_, _ = rand.Read(idb)
	p := &bgProc{
		id:      "proc_" + hex.EncodeToString(idb),
		command: command,
		started: time.Now(),
		buf:     &rollingBuffer{},
		done:    make(chan struct{}),
		notify:  notify && env.AsyncDelivery, // recorded before the process starts
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
				"[Process %s has finished, exit code %d. It ran: %s. Last %d characters of its output:\n%s]",
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

// lookupProc resolves an id or a unique prefix of 4+ characters.
func (s *State) lookupProc(ref string) (*bgProc, error) {
	ref = strings.TrimSpace(ref)
	if ref == "" {
		return nil, fmt.Errorf("give a session_id")
	}
	short := strings.TrimPrefix(ref, "proc_")
	s.mu.Lock()
	defer s.mu.Unlock()
	if p, ok := s.procs["proc_"+short]; ok {
		return p, nil
	}
	if len(short) >= procMinPrefixSize {
		var found []*bgProc
		for id, p := range s.procs {
			if strings.HasPrefix(id, "proc_"+short) {
				found = append(found, p)
			}
		}
		if len(found) == 1 {
			return found[0], nil
		}
		if len(found) > 1 {
			return nil, fmt.Errorf("%q fits more than one process; use more of the id", ref)
		}
	}
	return nil, fmt.Errorf("there is no process with id %s", ref)
}

func (s *State) snapshotProc(p *bgProc) (exited bool, code int, ended time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return p.exited, p.exitCode, p.ended
}

type processTool struct{}

func (processTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "process",
		Description: "Inspect and control the processes that terminal started with background set. " +
			"Choose an action: list shows them all; poll gives status plus the latest output; log pages through output lines " +
			"(offset, limit); wait blocks until the process ends or timeout seconds pass (180 by default, 600 at most); kill stops it. " +
			"Identify a process by the session_id terminal gave back, or by the first 4 or more characters of it if those are unique. " +
			"A background process cannot be given input.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "action": {"type": "string", "enum": ["list", "poll", "log", "wait", "kill"], "description": "What to do with the background processes."},
    "session_id": {"type": "string", "description": "Which process, for every action except list."},
    "timeout": {"type": "integer", "description": "For wait: how many seconds."},
    "offset": {"type": "integer", "description": "For log: the line to start at, from 0. Without it you get the most recent lines."},
    "limit": {"type": "integer", "description": "For log: how many lines, 200 by default."}
  },
  "required": ["action"]
}`),
	}}
}

type processArgs struct {
	Action    string `json:"action"`
	SessionID string `json:"session_id"`
	Timeout   *int   `json:"timeout"`
	Offset    *int   `json:"offset"`
	Limit     *int   `json:"limit"`
}

func (processTool) Run(ctx context.Context, env *Env, raw json.RawMessage) (string, error) {
	var a processArgs
	if err := json.Unmarshal(raw, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	st := env.State
	if a.Action == "list" {
		return listProcs(st)
	}
	switch a.Action {
	case "poll", "log", "wait", "kill":
	default:
		return errorResult(fmt.Sprintf("%q is not an action; the actions are list, poll, log, wait and kill", a.Action)), nil
	}
	p, err := st.lookupProc(a.SessionID)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	exited, code, ended := st.snapshotProc(p)
	status := "running"
	if exited {
		status = "exited"
	}
	switch a.Action {
	case "poll":
		out := map[string]any{
			"session_id": p.id, "command": p.command, "status": status, "pid": p.pid,
			"uptime_seconds": int(uptime(p, exited, ended).Seconds()),
			"output_preview": cleanOutput(p.buf.String(), procPreviewChars),
		}
		if exited {
			out["exit_code"] = code
		}
		return jsonResult(out)
	case "log":
		if exited {
			st.consume(p)
		}
		return jsonResult(logResult(p, status, a))
	case "wait":
		return waitProc(ctx, st, p, a.Timeout)
	default:
		return killProc(st, p)
	}
}

func uptime(p *bgProc, exited bool, ended time.Time) time.Duration {
	if exited {
		return ended.Sub(p.started)
	}
	return time.Since(p.started)
}

func listProcs(st *State) (string, error) {
	st.mu.Lock()
	ps := make([]*bgProc, 0, len(st.procs))
	for _, p := range st.procs {
		ps = append(ps, p)
	}
	st.mu.Unlock()
	sort.Slice(ps, func(i, j int) bool { return ps[i].started.Before(ps[j].started) })
	list := make([]map[string]any, 0, len(ps))
	for _, p := range ps {
		exited, code, ended := st.snapshotProc(p)
		e := map[string]any{
			"session_id": p.id, "command": lastCharsHead(p.command, procCommandPeek), "pid": p.pid,
			"status": "running", "started_at": p.started.Format(time.RFC3339),
			"uptime_seconds": int(uptime(p, exited, ended).Seconds()),
		}
		if exited {
			e["status"], e["exit_code"] = "exited", code
		}
		list = append(list, e)
	}
	return jsonResult(map[string]any{"processes": list})
}

func lastCharsHead(s string, n int) string {
	if r := []rune(s); len(r) > n {
		return string(r[:n])
	}
	return s
}

func logResult(p *bgProc, status string, a processArgs) map[string]any {
	text := strings.TrimRight(stripANSI(p.buf.String()), "\n")
	var lines []string
	if text != "" {
		lines = strings.Split(text, "\n")
	}
	limit := procLogDefault
	if a.Limit != nil && *a.Limit > 0 {
		limit = *a.Limit
	}
	start := max(len(lines)-limit, 0)
	if a.Offset != nil {
		start = min(max(*a.Offset, 0), len(lines))
	}
	sel := lines[start:min(start+limit, len(lines))]
	return map[string]any{
		"session_id": p.id, "status": status, "output": strings.Join(sel, "\n"),
		"total_lines": len(lines), "showing": fmt.Sprintf("%d lines", len(sel)),
	}
}

func waitProc(ctx context.Context, st *State, p *bgProc, timeout *int) (string, error) {
	secs := procWaitDefault
	if timeout != nil {
		secs = *timeout
	}
	secs = min(max(secs, 1), procWaitMax)
	timer := time.NewTimer(time.Duration(secs) * time.Second)
	defer timer.Stop()
	select {
	case <-p.done:
		st.consume(p)
		_, code, _ := st.snapshotProc(p)
		return jsonResult(map[string]any{
			"session_id": p.id, "status": "exited", "exit_code": code,
			"output": cleanOutput(p.buf.String(), procExitedChars),
		})
	case <-timer.C:
		return jsonResult(map[string]any{
			"session_id": p.id, "status": "timeout", "process_running": true,
			"output":       cleanOutput(p.buf.String(), procPreviewChars),
			"timeout_note": fmt.Sprintf("The process is still running after %d seconds and has not been touched. You can wait more, poll, or kill it.", secs),
		})
	case <-ctx.Done():
		return jsonResult(map[string]any{
			"session_id": p.id, "status": "interrupted",
			"note": "The wait was cut short; the process is still running.",
		})
	}
}

func killProc(st *State, p *bgProc) (string, error) {
	if exited, code, _ := st.snapshotProc(p); exited {
		st.consume(p)
		return jsonResult(map[string]any{"session_id": p.id, "status": "already_exited", "exit_code": code})
	}
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
	_, code, _ := st.snapshotProc(p)
	return jsonResult(map[string]any{"session_id": p.id, "status": "killed", "exit_code": code})
}

// runBackground is terminal(background=true), after the gate has allowed
// the command.
func runBackground(env *Env, cwd string, a terminalArgs) (string, error) {
	id, err := startBackground(env, cwd, a.Command, a.Notify)
	if err != nil {
		return errorResult(err.Error()), nil
	}
	p, _ := env.State.lookupProc(id)
	out := map[string]any{"output": "Started in the background", "session_id": id, "pid": p.pid, "exit_code": 0}
	if a.Notify && !env.AsyncDelivery {
		out["notify_unsupported"] = true
		out["notify_note"] = "This run cannot receive a notification. To find out when the process ends, wait on it or poll it with the process tool."
	}
	return jsonResult(out)
}
