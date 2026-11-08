// Command son-of-anton is the user's command: it runs soa-d for the terminal
// UI, -q and -z, shows the daemon's status and sessions (daemon/phases/10a-son-of-anton-command.md).
// `problem` runs and follows physics runs (daemon/phases/11c-physics-runs.md).
package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/buildinfo"
	"github.com/ewtodd/son-of-anton/daemon/internal/client"
	"github.com/ewtodd/son-of-anton/daemon/internal/config"
)

const usage = `usage:
  son-of-anton [flags]                          the terminal UI
  son-of-anton [flags] PROMPT...                one query
  son-of-anton [flags] -q PROMPT | --query PROMPT | --query-file FILE|-
  son-of-anton [flags] -z PROMPT | --oneshot PROMPT
  son-of-anton problem run SPEC [--model M] [--manager-model M] [--max-sessions N]
                       [--session-tokens N] [--total-tokens N] [--hours H] [--wait]
  son-of-anton problem list | watch ID | stop ID | resume ID
  son-of-anton status
  son-of-anton sessions [list] [--kind KIND]... [--all] [--limit N]
  son-of-anton sessions rename ID TITLE...
  son-of-anton sessions delete ID [--yes]
  son-of-anton sessions import ARGS...          (soa-d import's flags)
  son-of-anton view INSTANCE|--socket PATH [list] [--all] [--limit N]
  son-of-anton view INSTANCE|--socket PATH ID   a session, read-only, in the terminal UI
  son-of-anton view INSTANCE|--socket PATH show ID
  son-of-anton --version

flags: -m/--model M  --reasoning L  --yolo  --home DIR
       UI only: --in DIR  -c/--continue  -r/--resume ID|latest  --view (with -r: read-only)
       -z only: --usage-file F
`

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

// usageErr prints msg and the usage; the exit code is 2.
func usageErr(stderr io.Writer, format string, a ...any) int {
	fmt.Fprintf(stderr, "son-of-anton: "+format+"\n\n%s", append(a, usage)...)
	return 2
}

func fail(stderr io.Writer, format string, a ...any) int {
	fmt.Fprintf(stderr, "son-of-anton: "+format+"\n", a...)
	return 1
}

// flags is everything the top-level command line can say.
type flags struct {
	home, model, reasoning, in, resume   string
	query, oneshot, queryFile, usageFile string
	hasQuery, hasOneshot, hasQueryFile   bool
	yolo, cont, version, help, view      bool
	words                                []string
	command                              string // status, sessions, problem or view
	rest                                 []string
}

// valueFlags take a value; the long spelling is what they are stored under.
var valueFlags = map[string]string{
	"-m": "--model", "--model": "--model", "--reasoning": "--reasoning", "--home": "--home",
	"--in": "--in", "-r": "--resume", "--resume": "--resume",
	"-q": "--query", "--query": "--query", "--query-file": "--query-file",
	"-z": "--oneshot", "--oneshot": "--oneshot", "--usage-file": "--usage-file",
}

// parse reads the command line; ok is false after a usage error was printed.
func parse(args []string, stderr io.Writer) (f flags, code int, ok bool) {
	bad := func(format string, a ...any) (flags, int, bool) {
		return f, usageErr(stderr, format, a...), false
	}
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if arg == "--" {
			f.words = append(f.words, args[i+1:]...)
			break
		}
		if !strings.HasPrefix(arg, "-") || arg == "-" {
			if len(f.words) == 0 {
				switch arg {
				case "status", "sessions", "problem", "view":
					f.command, f.rest = arg, args[i+1:]
					return f, 0, true
				case "chat":
					continue // the old spelling of the same thing
				}
			}
			f.words = append(f.words, arg)
			continue
		}
		name, value, joined := arg, "", false
		if strings.HasPrefix(arg, "--") {
			if k, v, ok := strings.Cut(arg, "="); ok {
				name, value, joined = k, v, true
			}
		}
		if long, takes := valueFlags[name]; takes {
			if !joined {
				if i+1 >= len(args) {
					return bad("%s needs a value", name)
				}
				i++
				value = args[i]
			}
			switch long {
			case "--model":
				f.model = value
			case "--reasoning":
				f.reasoning = value
			case "--home":
				f.home = value
			case "--in":
				f.in = value
			case "--resume":
				f.resume = value
			case "--query":
				f.query, f.hasQuery = value, true
			case "--query-file":
				f.queryFile, f.hasQueryFile = value, true
			case "--oneshot":
				f.oneshot, f.hasOneshot = value, true
			case "--usage-file":
				f.usageFile = value
			}
			continue
		}
		if joined {
			return bad("unknown flag %s", name)
		}
		switch name {
		case "--yolo":
			f.yolo = true
		case "--view":
			f.view = true
		case "-c", "--continue":
			f.cont = true
		case "--version", "-V":
			f.version = true
		case "-h", "--help":
			f.help = true
		default:
			return bad("unknown flag %s", arg)
		}
	}
	return f, 0, true
}

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	f, code, ok := parse(args, stderr)
	if !ok {
		return code
	}
	if f.version {
		fmt.Fprintln(stdout, "son-of-anton "+buildinfo.Version)
		return 0
	}
	if f.help {
		fmt.Fprint(stdout, usage)
		return 0
	}
	switch f.command {
	case "problem":
		return runProblem(f, stdout, stderr)
	case "status":
		if len(f.rest) > 0 {
			return usageErr(stderr, "status takes no arguments")
		}
		return runStatus(f, stdout, stderr)
	case "sessions":
		return runSessions(f, stdin, stdout, stderr)
	case "view":
		return runView(f.rest, stdout, stderr)
	}
	return runSoaD(f, stdin, stderr)
}

// homeOf is the home to use: --home, else the environment's, absolute.
func homeOf(f flags) (string, error) {
	h := f.home
	if h == "" {
		var err error
		if h, err = config.Home(); err != nil {
			return "", err
		}
	}
	return filepath.Abs(h)
}

// soaD finds the daemon's binary: $SOA_D_BIN, soa-d beside this executable,
// soa-d on PATH.
func soaD() (string, error) {
	if b := os.Getenv("SOA_D_BIN"); b != "" {
		return b, nil
	}
	if exe, err := os.Executable(); err == nil {
		beside := filepath.Join(filepath.Dir(exe), "soa-d")
		if fi, err := os.Stat(beside); err == nil && !fi.IsDir() {
			return beside, nil
		}
	}
	if p, err := exec.LookPath("soa-d"); err == nil {
		return p, nil
	}
	return "", errors.New("soa-d is not installed")
}

// execSoaD replaces this process with soa-d running args.
func execSoaD(args []string, stderr io.Writer) int {
	bin, err := soaD()
	if err != nil {
		return fail(stderr, "%v", err)
	}
	err = syscall.Exec(bin, append([]string{bin}, args...), os.Environ())
	return fail(stderr, "running %s: %v", bin, err)
}

// runSoaD is the UI, a query or a one-shot.
func runSoaD(f flags, stdin io.Reader, stderr io.Writer) int {
	oneQuery := f.hasQuery || f.hasQueryFile || len(f.words) > 0
	isQuery := oneQuery && !f.hasOneshot
	switch {
	case f.hasQuery && f.hasOneshot:
		return usageErr(stderr, "-q and -z cannot be used together")
	case f.hasQueryFile && f.hasOneshot:
		return usageErr(stderr, "--query-file and -z cannot be used together")
	case f.hasQuery && f.hasQueryFile:
		return usageErr(stderr, "-q and --query-file cannot be used together")
	case (f.hasQuery || f.hasQueryFile || f.hasOneshot) && len(f.words) > 0:
		return usageErr(stderr, "give the prompt once: either as the value of -q/-z or as words")
	case f.usageFile != "" && !f.hasOneshot:
		return usageErr(stderr, "--usage-file only works with -z")
	case f.cont && f.resume != "":
		return usageErr(stderr, "-c and --resume cannot be used together")
	case f.cont && len(f.words) > 0 && !f.hasQuery && !f.hasQueryFile && !f.hasOneshot:
		return usageErr(stderr, "-c takes no name (resume by name is gone): use -c for this workspace's newest session, or --resume ID")
	case oneQuery || f.hasOneshot:
		if f.in != "" {
			return usageErr(stderr, "--in has no meaning with a query or -z")
		}
		if f.cont || f.resume != "" {
			return usageErr(stderr, "-c and --resume have no meaning with a query or -z")
		}
	}
	if f.view {
		switch {
		case oneQuery || f.hasOneshot:
			return usageErr(stderr, "--view opens a session in the terminal UI; it has no meaning with a query or -z")
		case f.resume == "":
			return usageErr(stderr, "--view needs the session to watch: -r ID|latest")
		case f.model != "" || f.reasoning != "" || f.yolo:
			return usageErr(stderr, "--view only watches: -m, --reasoning and --yolo would change the session")
		}
	}
	home, err := homeOf(f)
	if err != nil {
		return fail(stderr, "%v", err)
	}
	common := []string{"--home", home}
	if f.model != "" {
		common = append(common, "--model", f.model)
	}
	if f.reasoning != "" {
		common = append(common, "--reasoning", f.reasoning)
	}
	if f.yolo {
		common = append(common, "--yolo")
	}

	if f.hasOneshot {
		argv := append([]string{"oneshot"}, common...)
		if f.usageFile != "" {
			argv = append(argv, "--usage-file", f.usageFile)
		}
		return execSoaD(append(argv, f.oneshot), stderr)
	}
	if isQuery {
		prompt := f.query
		switch {
		case f.hasQueryFile:
			var b []byte
			var err error
			if f.queryFile == "-" {
				b, err = io.ReadAll(stdin)
			} else {
				b, err = os.ReadFile(f.queryFile)
			}
			if err != nil {
				fmt.Fprintf(stderr, "son-of-anton: cannot read --query-file %s: %v\n", f.queryFile, err)
				return 2
			}
			if strings.TrimSpace(string(b)) == "" {
				fmt.Fprintf(stderr, "son-of-anton: --query-file %s is empty\n", f.queryFile)
				return 2
			}
			prompt = string(b)
		case !f.hasQuery:
			prompt = strings.Join(f.words, " ")
		}
		return execSoaD(append(append([]string{"query"}, common...), prompt), stderr)
	}

	cwd := f.in
	if cwd == "" {
		if cwd, err = os.Getwd(); err != nil {
			return fail(stderr, "%v", err)
		}
	}
	if cwd, err = filepath.Abs(cwd); err != nil {
		return fail(stderr, "%v", err)
	}
	if fi, err := os.Stat(cwd); err != nil || !fi.IsDir() {
		return fail(stderr, "--in directory not found: %s", f.in)
	}
	argv := append(append([]string{"tui"}, common...), "--in", cwd)
	switch {
	case f.resume != "":
		argv = append(argv, "--resume", strings.TrimSpace(f.resume))
	case f.cont:
		argv = append(argv, "-c")
	}
	if f.view {
		argv = append(argv, "--view")
	}
	return execSoaD(argv, stderr)
}

// ---- status ----

type listed struct {
	Sessions []struct {
		ID      string `json:"id"`
		Kind    string `json:"kind"`
		Title   string `json:"title"`
		Cwd     string `json:"cwd"`
		Updated string `json:"updated"`
	} `json:"sessions"`
}

// kinds the terminal can see, in the order status counts them.
var kinds = []string{"interactive", "oneshot", "cron"}

func onOff(b bool) string {
	if b {
		return "on"
	}
	return "off"
}

func runStatus(f flags, stdout, stderr io.Writer) int {
	home, err := homeOf(f)
	if err != nil {
		return fail(stderr, "%v", err)
	}
	cfg, err := config.Load(home)
	if err != nil {
		return fail(stderr, "%v", err)
	}
	fmt.Fprintf(stdout, "home: %s\n", home)

	c, derr := client.Dial(home, "cli")
	if derr == nil {
		defer c.Close()
		h := c.Hello()
		// A daemon started before the one-version release says "soa-d 0.0.1".
		version := strings.TrimPrefix(fmt.Sprint(h["version"]), "soa-d ")
		fmt.Fprintf(stdout, "daemon: running (pid %v, soa-d %s)\n", num(h["pid"]), version)
	} else {
		fmt.Fprintln(stdout, "daemon: not running")
	}
	if cfg.Daemon.Autostart != nil && !*cfg.Daemon.Autostart {
		unit := "its son-of-anton unit"
		if cfg.Daemon.Instance != "" {
			unit = "the son-of-anton-" + cfg.Daemon.Instance + " unit"
		}
		fmt.Fprintf(stdout, "served by: %s\n", unit)
	} else {
		fmt.Fprintln(stdout, "served by: started on demand by the terminal UI")
	}
	fmt.Fprintf(stdout, "signal: %s\n", onOff(cfg.Daemon.Signal))
	fmt.Fprintf(stdout, "cron: %s\n", onOff(cfg.Daemon.Cron))
	chats := cfg.ChatModel()
	if chats == "" {
		chats = "the default"
	}
	fmt.Fprintf(stdout, "models: %s, chats %s\n", cfg.Model.Default, chats)

	if derr != nil {
		return 0
	}
	var res listed
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := c.Call(ctx, "session.list", map[string]any{"kinds": kinds, "limit": 1 << 30}, &res); err != nil {
		return fail(stderr, "%v", err)
	}
	count := map[string]int{}
	for _, s := range res.Sessions {
		count[s.Kind]++
	}
	var parts []string
	for _, k := range kinds {
		if count[k] > 0 {
			parts = append(parts, fmt.Sprintf("%d %s", count[k], k))
		}
	}
	if len(parts) == 0 {
		parts = []string{"none"}
	}
	fmt.Fprintf(stdout, "sessions: %s\n", strings.Join(parts, ", "))
	return 0
}

func num(v any) string {
	if n, ok := v.(float64); ok {
		return strconv.FormatInt(int64(n), 10)
	}
	return fmt.Sprint(v)
}

// ---- sessions ----

func runSessions(f flags, stdin io.Reader, stdout, stderr io.Writer) int {
	args := f.rest
	action := "list"
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		action, args = args[0], args[1:]
	}
	home, err := homeOf(f)
	if err != nil {
		return fail(stderr, "%v", err)
	}
	if action == "import" {
		// soa-d import's own flags; --home may follow them.
		var rest []string
		for i := 0; i < len(args); i++ {
			switch {
			case args[i] == "--home" && i+1 < len(args):
				home, i = args[i+1], i+1
			case strings.HasPrefix(args[i], "--home="):
				home = strings.TrimPrefix(args[i], "--home=")
			default:
				rest = append(rest, args[i])
			}
		}
		return execSoaD(append(append([]string{"import"}, rest...), "--home", home), stderr)
	}

	switch action {
	case "list", "rename", "delete":
	default:
		return usageErr(stderr, "unknown sessions action %q", action)
	}
	// Check the arguments before connecting (which may start a daemon).
	var (
		kindsWanted []string
		all, yes    bool
		limit       = 50
		pos         []string
	)
	for i := 0; i < len(args); i++ {
		a := args[i]
		switch {
		case action == "list" && a == "--kind", action == "list" && a == "--limit":
			if i+1 >= len(args) {
				return usageErr(stderr, "%s needs a value", a)
			}
			i++
			if a == "--kind" {
				kindsWanted = append(kindsWanted, args[i])
			} else if n, err := strconv.Atoi(args[i]); err != nil || n < 1 {
				return usageErr(stderr, "--limit needs a positive number")
			} else {
				limit = n
			}
		case action == "list" && strings.HasPrefix(a, "--kind="):
			kindsWanted = append(kindsWanted, strings.TrimPrefix(a, "--kind="))
		case action == "list" && strings.HasPrefix(a, "--limit="):
			n, err := strconv.Atoi(strings.TrimPrefix(a, "--limit="))
			if err != nil || n < 1 {
				return usageErr(stderr, "--limit needs a positive number")
			}
			limit = n
		case action == "list" && a == "--all":
			all = true
		case action == "delete" && (a == "--yes" || a == "-y"):
			yes = true
		case strings.HasPrefix(a, "-") && a != "-":
			return usageErr(stderr, "unknown flag %s", a)
		default:
			pos = append(pos, a)
		}
	}
	switch action {
	case "list":
		if len(pos) > 0 {
			return usageErr(stderr, "sessions list takes no arguments")
		}
	case "rename":
		if len(pos) < 2 {
			return usageErr(stderr, "usage: sessions rename ID TITLE...")
		}
	case "delete":
		if len(pos) != 1 {
			return usageErr(stderr, "usage: sessions delete ID [--yes]")
		}
	}

	c, err := client.Connect(home, "cli")
	if err != nil {
		return fail(stderr, "%v", err)
	}
	defer c.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	rpc := func(method string, params, out any) bool {
		if err := c.Call(ctx, method, params, out); err != nil {
			fail(stderr, "%v", err)
			return false
		}
		return true
	}

	switch action {
	case "list":
		want := kindsWanted
		switch {
		case all:
			want = []string{"interactive", "oneshot", "cron", "chat", "child"}
		case len(want) == 0:
			want = []string{"interactive"}
		}
		var res listed
		if !rpc("session.list", map[string]any{"kinds": want, "limit": limit}, &res) {
			return 1
		}
		if len(res.Sessions) == 0 {
			fmt.Fprintln(stdout, "no sessions")
			return 0
		}
		homeDir, _ := os.UserHomeDir()
		for _, s := range res.Sessions {
			title := s.Title
			if title == "" {
				title = "(untitled)"
			}
			updated := s.Updated
			if t, err := time.Parse(time.RFC3339, s.Updated); err == nil {
				updated = t.Local().Format("2006-01-02 15:04")
			}
			fmt.Fprintf(stdout, "%s  %s  %s  %s  %s\n", s.ID, s.Kind, updated, title, tilde(s.Cwd, homeDir))
		}
	case "rename":
		title := strings.Join(pos[1:], " ")
		if !rpc("session.rename", map[string]any{"session": pos[0], "title": title}, nil) {
			return 1
		}
		fmt.Fprintf(stdout, "renamed %s: %s\n", pos[0], strings.TrimSpace(title))
	case "delete":
		id := pos[0]
		if !yes {
			title, ok := titleOf(ctx, c, id)
			if !ok {
				return fail(stderr, "no such session %q", id)
			}
			fmt.Fprintf(stdout, "delete %s (%s)? [y/N] ", id, title)
			line, _ := bufio.NewReader(stdin).ReadString('\n')
			if a := strings.ToLower(strings.TrimSpace(line)); a != "y" && a != "yes" {
				fmt.Fprintln(stdout, "not deleted")
				return 1
			}
		}
		if !rpc("session.delete", map[string]any{"session": id}, nil) {
			return 1
		}
		fmt.Fprintf(stdout, "deleted %s\n", id)
	}
	return 0
}

// titleOf is a session's title ("(untitled)" without one); ok is false when
// the daemon does not know it.
func titleOf(ctx context.Context, c *client.Client, id string) (string, bool) {
	var res listed
	all := []string{"interactive", "oneshot", "cron", "chat", "child"}
	if err := c.Call(ctx, "session.list", map[string]any{"kinds": all, "limit": 1 << 30}, &res); err != nil {
		return "", false
	}
	for _, s := range res.Sessions {
		if s.ID == id {
			if s.Title == "" {
				return "(untitled)", true
			}
			return s.Title, true
		}
	}
	return "", false
}

// tilde shortens a path under the home directory to ~/...
func tilde(p, home string) string {
	if home != "" && (p == home || strings.HasPrefix(p, home+"/")) {
		return "~" + strings.TrimPrefix(p, home)
	}
	return p
}
