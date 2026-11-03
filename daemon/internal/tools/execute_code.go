package tools

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

const (
	codeMaxToolCalls = 50
	codeStdoutMax    = 50_000
	codeStdoutHead   = codeStdoutMax * 2 / 5
	codeStdoutTail   = codeStdoutMax - codeStdoutHead
	codeStderrMax    = 10_000
	codeKillGrace    = 5 * time.Second
	codeJoinWait     = 3 * time.Second
)

// codeTimeout is the wall clock a script gets; a variable so a test can
// shorten it.
var codeTimeout = 300 * time.Second

// executeCodeTool runs a Python script that calls the session's tools over a
// socket. have is the sandbox tools its registry holds (nil: all of them);
// the registry sets it, so the schema documents only tools the model has.
type executeCodeTool struct{ have []string }

// adapt returns the tool for a registry holding the named tools.
func (executeCodeTool) adapt(names []string) Tool {
	var have []string
	for _, n := range sandboxTools {
		if slices.Contains(names, n) {
			have = append(have, n)
		}
	}
	return executeCodeTool{have: have}
}

// sandbox is the tools a script may call: the registry's, or all of the
// sandbox tools when it holds none of them.
func (t executeCodeTool) sandbox() []string {
	if len(t.have) == 0 {
		return sandboxTools
	}
	return t.have
}

func (t executeCodeTool) Schema() provider.Tool {
	have := t.sandbox()
	var lines []string
	for _, n := range sandboxTools {
		if slices.Contains(have, n) {
			lines = append(lines, toolDocLines[n])
		}
	}
	var examples []string
	for _, n := range []string{"web_search", "terminal"} {
		if slices.Contains(have, n) {
			examples = append(examples, n)
		}
	}
	if len(examples) == 0 {
		examples = have[:min(2, len(have))]
	}
	imports := "..."
	if len(examples) > 0 {
		imports = strings.Join(examples, ", ") + ", ..."
	}
	desc := "Run a Python script that calls Son of Anton tools, for 3+ calls with logic between " +
		"them: filtering large outputs before they reach your context, branching, or loops " +
		"over pages and files. Use plain tool calls for single calls, results you need to read " +
		"in full, or anything that needs the user.\n\n" +
		"Available via `from son_of_anton_tools import ...`:\n" + strings.Join(lines, "\n") + "\n\n" +
		"Limits: 5 minutes, 50 KB of stdout, 50 tool calls; terminal() runs in the foreground " +
		"only. Scripts run in the session's working directory with Python 3, and the " +
		"stdlib is available. Print the result to stdout. Helpers (no import): json_parse(text), " +
		"shell_quote(s), retry(fn, max_attempts=3, delay=2)."
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"code": map[string]any{
				"type": "string",
				"description": "Python code to execute. Import tools with " +
					"`from son_of_anton_tools import " + imports + "` " +
					"and print your final result to stdout.",
			},
		},
		"required": []string{"code"},
	})
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "execute_code", Description: desc, Parameters: params,
	}}
}

// codeError is a result for a script that did not run.
func codeError(msg string) string {
	return jsonOut(map[string]any{"status": "error", "error": msg, "tool_calls_made": 0, "duration_seconds": 0})
}

func (t executeCodeTool) Run(ctx context.Context, env *Env, args json.RawMessage) (string, error) {
	var in map[string]json.RawMessage
	if err := json.Unmarshal(args, &in); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	if _, hasCode := in["code"]; !hasCode {
		if _, hasCommand := in["command"]; hasCommand {
			return errorResult("execute_code received a 'command' parameter, but it requires " +
				"Python source in 'code'. Use terminal(command=...) for shell " +
				"commands; for Python, retry as execute_code(code=...)."), nil
		}
	}
	var code string
	if raw := in["code"]; len(raw) > 0 && string(raw) != "null" {
		if err := json.Unmarshal(raw, &code); err != nil {
			return errorResult(fmt.Sprintf("execute_code received a %s in 'code', but it "+
				"requires Python source as a string. Retry as "+
				"execute_code(code=\"...\").", jsonTypeName(raw))), nil
		}
	}
	if strings.TrimSpace(code) == "" {
		return errorResult("No code provided. execute_code requires a non-empty 'code' " +
			"parameter containing Python source. To run shell commands, use " +
			"terminal(command=...) instead."), nil
	}
	if env.NoScripts != "" {
		return codeError(env.NoScripts), nil
	}
	py := findPython()
	if py == "" {
		return jsonOut(map[string]any{"status": "error", "error": "execute_code needs a Python 3 interpreter and found none"}), nil
	}
	if v := env.Gate.Script(ctx, code); !v.Allowed {
		return codeError(v.Message), nil
	}
	if env.Call == nil {
		return codeError("execute_code cannot call tools in this run"), nil
	}
	return t.run(ctx, env, py, code), nil
}

// jsonTypeName names a JSON value's type the way a Python script would.
func jsonTypeName(raw json.RawMessage) string {
	switch raw[0] {
	case '{':
		return "dict"
	case '[':
		return "list"
	case 't', 'f':
		return "bool"
	}
	if bytes.ContainsAny(raw, ".eE") {
		return "float"
	}
	return "int"
}

// findPython picks the interpreter: the active virtualenv's, the conda
// env's, $SON_OF_ANTON_PYTHON (the Nix wrapper sets it), then python3 on
// PATH. "" when none exists.
func findPython() string {
	usable := func(p string) bool {
		fi, err := os.Stat(p)
		return err == nil && fi.Mode().IsRegular() && fi.Mode().Perm()&0o111 != 0
	}
	for _, v := range []string{"VIRTUAL_ENV", "CONDA_PREFIX"} {
		root := strings.TrimSpace(os.Getenv(v))
		if root == "" {
			continue
		}
		for _, exe := range []string{"python", "python3"} {
			if p := filepath.Join(root, "bin", exe); usable(p) {
				return p
			}
		}
	}
	if p := strings.TrimSpace(os.Getenv("SON_OF_ANTON_PYTHON")); p != "" && usable(p) {
		return p
	}
	if p, err := exec.LookPath("python3"); err == nil {
		return p
	}
	return ""
}

// secretSubstrings name the variables a script never sees; safePrefixes the
// ones it may.
var (
	secretSubstrings = []string{"KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PASSWD", "AUTH", "DSN", "WEBHOOK", "CREDS", "BEARER", "APIKEY"}
	safePrefixes     = []string{"PATH", "HOME", "USER", "LANG", "LC_", "TERM", "TMPDIR", "TMP", "TEMP", "SHELL", "LOGNAME", "XDG_", "PYTHONPATH", "VIRTUAL_ENV", "CONDA"}
)

// scrubEnv is the environment of a script: the inherited variables that name
// no secret and start with a safe prefix, plus the RPC endpoint and the
// Python settings. PYTHONPATH is the staging directory, then any inherited.
func scrubEnv(environ []string, dir, sock, token string) []string {
	var out []string
	inherited := ""
	for _, kv := range environ {
		name, value, ok := strings.Cut(kv, "=")
		if !ok {
			continue
		}
		upper := strings.ToUpper(name)
		secret := false
		for _, s := range secretSubstrings {
			if strings.Contains(upper, s) {
				secret = true
				break
			}
		}
		if secret {
			continue
		}
		keep := name == "SON_OF_ANTON_HOME"
		for _, p := range safePrefixes {
			if strings.HasPrefix(name, p) {
				keep = true
			}
		}
		if !keep {
			continue
		}
		if name == "PYTHONPATH" {
			inherited = value
			continue
		}
		out = append(out, kv)
	}
	path := dir
	if inherited != "" {
		path += string(os.PathListSeparator) + inherited
	}
	return append(out,
		"SON_OF_ANTON_RPC_SOCKET="+sock,
		"SON_OF_ANTON_RPC_TOKEN="+token,
		"PYTHONDONTWRITEBYTECODE=1",
		"PYTHONIOENCODING=utf-8",
		"PYTHONUTF8=1",
		"PYTHONPATH="+path,
	)
}

// headTail keeps the first head bytes of a stream and the last tail bytes
// of the rest, and counts all of it.
type headTail struct {
	mu         sync.Mutex
	maxH, maxT int
	head, tail []byte
	total      int
}

func (h *headTail) Write(p []byte) (int, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	n := len(p)
	h.total += n
	if room := h.maxH - len(h.head); room > 0 {
		take := min(room, len(p))
		h.head = append(h.head, p[:take]...)
		p = p[take:]
	}
	if h.maxT > 0 {
		h.tail = append(h.tail, p...)
		if len(h.tail) > 2*h.maxT {
			h.tail = append(h.tail[:0], h.tail[len(h.tail)-h.maxT:]...)
		}
	}
	return n, nil
}

// text is the kept output and its metadata; the part between head and tail
// is replaced by a notice.
func (h *headTail) text() (string, map[string]any) {
	h.mu.Lock()
	defer h.mu.Unlock()
	tail := h.tail
	if len(tail) > h.maxT {
		tail = tail[len(tail)-h.maxT:]
	}
	captured := len(h.head) + len(tail)
	omitted := h.total - captured
	meta := map[string]any{
		"stdout_truncated":      omitted > 0,
		"stdout_bytes_captured": captured,
		"stdout_bytes_total":    h.total,
		"stdout_bytes_omitted":  omitted,
	}
	out := string(h.head) + string(tail)
	if omitted > 0 {
		out = string(h.head) + fmt.Sprintf("\n\n... [OUTPUT TRUNCATED - %s bytes omitted out of %s total] ...\n\n", commas(omitted), commas(h.total)) + string(tail)
		meta["warning"] = "execute_code stdout was truncated; the script did run, but only " +
			"the captured head/tail output is included. Re-run only with " +
			"narrower output if the omitted data is required."
	}
	return strings.ToValidUTF8(out, "�"), meta
}

// commas writes n with thousands separated.
func commas(n int) string {
	s := fmt.Sprint(n)
	for i := len(s) - 3; i > 0; i -= 3 {
		s = s[:i] + "," + s[i:]
	}
	return s
}

// run stages the stub module and the script, serves the script's tool calls
// and returns the result.
func (t executeCodeTool) run(ctx context.Context, env *Env, py, code string) string {
	began := time.Now()
	failed := func(err error) string {
		return jsonOut(map[string]any{"status": "error", "error": err.Error(), "tool_calls_made": 0,
			"duration_seconds": math.Round(time.Since(began).Seconds()*100) / 100})
	}
	dir, err := os.MkdirTemp("", "soa-code-")
	if err != nil {
		return failed(err)
	}
	defer func() { _ = os.RemoveAll(dir) }()
	have := t.sandbox()
	if err := os.WriteFile(filepath.Join(dir, "son_of_anton_tools.py"), []byte(stubModule(have)), 0o600); err != nil {
		return failed(err)
	}
	script := filepath.Join(dir, "script.py")
	if err := os.WriteFile(script, []byte(code), 0o600); err != nil {
		return failed(err)
	}

	sock := filepath.Join(dir, "rpc.sock")
	ln, err := net.Listen("unix", sock)
	if err != nil {
		return failed(err)
	}
	if err := os.Chmod(sock, 0o600); err != nil {
		_ = ln.Close()
		return failed(err)
	}
	tok := make([]byte, 32)
	_, _ = rand.Read(tok)
	token := base64.RawURLEncoding.EncodeToString(tok)
	rpcCtx, rpcCancel := context.WithCancel(ctx)
	rpc := &rpcServer{ln: ln, token: token, allowed: have, call: env.Call}
	served := make(chan struct{})
	go func() { defer close(served); rpc.serve(rpcCtx) }()
	stopRPC := func() {
		rpcCancel()
		rpc.close()
		select {
		case <-served:
		case <-time.After(codeJoinWait):
		}
	}
	defer stopRPC()

	cmd := exec.Command(py, script)
	cmd.Dir = env.State.Cwd()
	cmd.Env = withHome(scrubEnv(os.Environ(), dir, sock, token), env.CommandHome)
	out := &headTail{maxH: codeStdoutHead, maxT: codeStdoutTail}
	errOut := &headTail{maxH: codeStderrMax}
	cmd.Stdout, cmd.Stderr = out, errOut
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	// A grandchild that kept the pipes open must not hold the result back.
	cmd.WaitDelay = codeJoinWait
	if err := cmd.Start(); err != nil {
		return failed(err)
	}
	waited := make(chan struct{})
	go func() { _ = cmd.Wait(); close(waited) }()

	status := "success"
	timer := time.NewTimer(codeTimeout)
	defer timer.Stop()
	select {
	case <-waited:
	case <-timer.C:
		status = "timeout"
	case <-ctx.Done():
		status = "interrupted"
	}
	if status != "success" {
		stopGroup(cmd.Process.Pid, waited)
	}
	stopRPC()

	stdout, meta := out.text()
	stderr, _ := errOut.text()
	stdout, stderr = stripANSI(stdout), stripANSI(stderr)
	exit := cmd.ProcessState.ExitCode()
	res := map[string]any{
		"status":           status,
		"output":           stdout,
		"exit_code":        exit,
		"tool_calls_made":  rpc.made(),
		"duration_seconds": math.Round(time.Since(began).Seconds()*100) / 100,
	}
	for k, v := range meta {
		res[k] = v
	}
	switch {
	case status == "timeout":
		msg := fmt.Sprintf("Script timed out after %ds and was killed.", int(codeTimeout/time.Second))
		res["error"] = msg
		if stdout != "" {
			res["output"] = stdout + "\n\n⏰ " + msg
		} else {
			res["output"] = "⏰ " + msg
		}
	case status == "interrupted":
		res["output"] = stdout + "\n[execution interrupted — user sent a new message]"
	case exit != 0:
		res["status"] = "error"
		if stderr != "" {
			res["error"] = stderr
			// The stdout's last newline (a print's) is the separator's.
			res["output"] = strings.TrimSuffix(stdout, "\n") + "\n--- stderr ---\n" + stderr
		} else {
			res["error"] = fmt.Sprintf("Script exited with code %d", exit)
		}
	}
	return jsonOut(res)
}

// stopGroup ends the script's process group: SIGTERM, then SIGKILL if it is
// still there after the grace period.
func stopGroup(pid int, waited <-chan struct{}) {
	_ = syscall.Kill(-pid, syscall.SIGTERM)
	select {
	case <-waited:
	case <-time.After(codeKillGrace):
		_ = syscall.Kill(-pid, syscall.SIGKILL)
		<-waited
	}
}

// rpcServer answers one script's tool calls on a unix socket, one connection,
// one request at a time.
type rpcServer struct {
	ln      net.Listener
	token   string
	allowed []string
	call    func(ctx context.Context, name string, args json.RawMessage) string

	mu    sync.Mutex
	conn  net.Conn
	count int
}

func (s *rpcServer) made() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.count
}

// close stops accepting and drops the connection.
func (s *rpcServer) close() {
	_ = s.ln.Close()
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.conn != nil {
		_ = s.conn.Close()
	}
}

func (s *rpcServer) serve(ctx context.Context) {
	conn, err := s.ln.Accept()
	if err != nil {
		return
	}
	_ = s.ln.Close()
	s.mu.Lock()
	s.conn = conn
	s.mu.Unlock()
	defer func() { _ = conn.Close() }()
	r := bufio.NewReader(conn)
	for {
		line, err := r.ReadBytes('\n')
		if line = bytes.TrimSpace(line); len(line) > 0 {
			if _, werr := conn.Write([]byte(s.handle(ctx, line) + "\n")); werr != nil {
				return
			}
		}
		if err != nil {
			return
		}
	}
}

// handle answers one request line with one line.
func (s *rpcServer) handle(ctx context.Context, line []byte) string {
	var req struct {
		Tool  string          `json:"tool"`
		Args  json.RawMessage `json:"args"`
		Token string          `json:"token"`
	}
	if err := json.Unmarshal(line, &req); err != nil {
		return errorResult("Invalid RPC request: " + err.Error())
	}
	if s.token == "" || subtle.ConstantTimeCompare([]byte(req.Token), []byte(s.token)) != 1 {
		return errorResult("Unauthorized RPC request")
	}
	if !slices.Contains(s.allowed, req.Tool) {
		names := slices.Clone(s.allowed)
		sort.Strings(names)
		return errorResult(fmt.Sprintf("Tool '%s' is not available in execute_code. Available: %s", req.Tool, strings.Join(names, ", ")))
	}
	s.mu.Lock()
	over := s.count >= codeMaxToolCalls
	s.mu.Unlock()
	if over {
		return errorResult(fmt.Sprintf("Tool call limit reached (%d). No more tool calls allowed in this execution.", codeMaxToolCalls))
	}
	args := req.Args
	if len(args) == 0 {
		args = json.RawMessage("{}")
	}
	if req.Tool == "terminal" {
		var m map[string]json.RawMessage
		if json.Unmarshal(args, &m) == nil {
			delete(m, "background")
			delete(m, "notify_on_complete")
			args, _ = json.Marshal(m)
		}
	}
	result := s.call(ctx, req.Tool, args)
	s.mu.Lock()
	s.count++
	s.mu.Unlock()
	return oneLine(result)
}

// oneLine is a tool result as one line: JSON compacted, anything else as a
// JSON string.
func oneLine(result string) string {
	var b bytes.Buffer
	if json.Compact(&b, []byte(result)) == nil {
		return b.String()
	}
	q, _ := json.Marshal(result)
	return string(q)
}
