package approval

import (
	"path"
	"strings"
)

// The rules. Each asks a question about the token stream: "is there a
// delete whose operand is the filesystem root?", "does anything fetched get
// piped into a shell?". A rule that looks for a verb finds it wherever it
// stands, quoted or not, because the model may hide a command inside
// `echo`, `eval` or `bash -c`; only the quoted operands of a search command
// are exempt (scan.go). The hardline rules are stricter about position:
// they look at what a segment actually runs.

// --- small helpers -------------------------------------------------------

func isFlag(s string) bool { return len(s) >= 2 && s[0] == '-' }

// hasBounded reports whether w occurs in s with no word character on
// either side.
func hasBounded(s, w string) bool {
	for from := 0; ; {
		i := strings.Index(s[from:], w)
		if i < 0 {
			return false
		}
		i += from
		end := i + len(w)
		if (i == 0 || !isWordChar(s[i-1])) && (end == len(s) || !isWordChar(s[end])) {
			return true
		}
		from = i + 1
	}
}

// startsWord reports whether s is w, or w followed by a non-word character.
func startsWord(s, w string) bool {
	return strings.HasPrefix(s, w) && (len(s) == len(w) || !isWordChar(s[len(w)]))
}

func anyArg(args []tok, f func(string) bool) bool {
	for _, a := range args {
		if f(a.l) {
			return true
		}
	}
	return false
}

// shortCluster reports whether s is a single-dash option bundle that
// includes one of the letters.
func shortCluster(s, letters string) bool {
	return isFlag(s) && s[1] != '-' && strings.ContainsAny(s[1:], letters)
}

// firstOperand is the first argument that is not an option.
func firstOperand(args []tok) (tok, bool) {
	for _, a := range args {
		if !isFlag(a.t) {
			return a, true
		}
	}
	return tok{}, false
}

// --- paths ---------------------------------------------------------------

// canonHome writes a leading $HOME or ${HOME} as ~.
func canonHome(p string) string {
	for _, v := range []string{"${home}", "$home"} {
		if strings.HasPrefix(p, v) {
			return "~" + p[len(v):]
		}
	}
	return p
}

var systemTrees = map[string]bool{
	"/home": true, "/root": true, "/etc": true, "/usr": true, "/var": true,
	"/bin": true, "/sbin": true, "/boot": true, "/lib": true,
}

// protectedPath is the filesystem root, a top-level system directory, or the
// home directory, each with or without a trailing glob.
func protectedPath(p string) bool {
	p = canonHome(p)
	if p == "" || (p[0] != '/' && p[0] != '~') {
		return false
	}
	c := path.Clean(p)
	if p[0] == '/' && strings.Trim(c, "/*") == "" {
		return true
	}
	c = strings.TrimSuffix(c, "/*")
	return systemTrees[c] || c == "~"
}

var diskPrefixes = []string{"/dev/sd", "/dev/nvme", "/dev/hd", "/dev/mmcblk", "/dev/vd", "/dev/xvd"}

func isDiskDevice(p string) bool {
	for _, d := range diskPrefixes {
		if strings.HasPrefix(p, d) {
			return true
		}
	}
	return false
}

type target uint8

const (
	tNone target = iota
	tSystem
	tCredential
	tSOA
	tProject
)

var targetRule = map[target]string{
	tSystem:     "write-system-path",
	tCredential: "write-credential-path",
	tSOA:        "write-soa-config",
	tProject:    "write-project-config",
}

var (
	rcFiles   = []string{".bashrc", ".zshrc", ".profile", ".bash_profile", ".zprofile"}
	credFiles = []string{".netrc", ".pgpass", ".npmrc", ".pypirc"}
	soaRoots  = []string{"~/.son-of-anton/", "$son_of_anton_home/", "${son_of_anton_home}/"}
)

func startsPath(p, prefix string) bool { return startsWord(p, prefix) }

// classifyTarget says which kind of protected file a path names, if any.
func classifyTarget(raw string) target {
	p := canonHome(raw)
	switch {
	case strings.HasPrefix(p, "/etc/"), strings.HasPrefix(p, "/dev/sd"):
		return tSystem
	case p == "~/.ssh", strings.HasPrefix(p, "~/.ssh/"):
		return tCredential
	}
	for _, f := range append(append([]string{}, rcFiles...), credFiles...) {
		if startsPath(p, "~/"+f) {
			return tCredential
		}
	}
	for _, r := range soaRoots {
		if rest, ok := strings.CutPrefix(p, r); ok && (startsPath(rest, ".env") || startsPath(rest, "config.toml")) {
			return tSOA
		}
	}
	last := p[strings.LastIndexByte(p, '/')+1:]
	if last == ".env" || strings.HasPrefix(last, ".env.") || last == "config.toml" {
		return tProject
	}
	return tNone
}

// --- hardline ------------------------------------------------------------

func checkHard(sc *scan, h *hits) {
	for _, ch := range sc.commands() {
		for _, oi := range ch.sudo {
			if sudoStdinOrShell(sc.toks[oi].t) {
				h.add("sudo-stdin-shell")
			}
		}
		if ch.cmd < 0 {
			continue
		}
		name := base(sc.toks[ch.cmd].l)
		args := sc.wordArgs(ch.cmd, true)
		switch name {
		case "rm":
			endOpts := false
			for _, a := range args {
				if a.t == "--" {
					endOpts = true
				} else if (endOpts || !isFlag(a.t)) && protectedPath(a.l) {
					h.add("wipe-tree")
				}
			}
		case "init", "telinit":
			if len(args) > 0 && (startsWord(args[0].l, "0") || startsWord(args[0].l, "6")) {
				h.add("halt-machine")
			}
		case "systemctl":
			user := anyArg(args, func(a string) bool { return a == "--user" })
			if v, ok := firstOperand(args); ok && !user {
				for _, w := range []string{"poweroff", "reboot", "halt", "kexec"} {
					if startsWord(v.l, w) {
						h.add("halt-machine")
					}
				}
			}
		default:
			for _, w := range []string{"shutdown", "reboot", "halt", "poweroff"} {
				if startsWord(name, w) {
					h.add("halt-machine")
				}
			}
		}
	}

	// These look at the whole text, quoted or not.
	var joined strings.Builder
	for i, t := range sc.toks {
		joined.WriteString(t.t)
		switch {
		case t.kind == kWord && hasBounded(t.l, "mkfs"):
			h.add("format-disk")
		case t.kind == kWord && base(t.l) == "dd":
			for _, u := range sc.rest(i + 1) {
				if u.line != t.line {
					break
				}
				if v, ok := strings.CutPrefix(u.l, "of="); ok && u.kind == kWord && isDiskDevice(v) {
					h.add("raw-disk-write")
				}
			}
		case t.kind == kWord && base(t.l) == "kill" && killsEverything(sc.wordArgs(i, false)):
			h.add("kill-everything")
		case t.kind == kRedir && strings.Contains(t.l, ">") && i+1 < len(sc.toks) &&
			sc.toks[i+1].kind == kWord && isDiskDevice(canonHome(sc.toks[i+1].l)):
			h.add("raw-disk-write")
		}
	}
	if strings.Contains(joined.String(), ":(){:|:&};:") {
		h.add("fork-bomb")
	}
}

// sudoStdinOrShell reports whether a sudo option reads the password from
// stdin (-S, --stdin) or starts a shell (-s, --shell).
func sudoStdinOrShell(opt string) bool {
	if strings.HasPrefix(opt, "--") {
		return len(opt) >= 4 && (strings.HasPrefix("--stdin", opt) || (len(opt) >= 4 && strings.HasPrefix("--shell", opt)))
	}
	return strings.ContainsAny(opt[1:], "sS")
}

// killsEverything: kill's options end with -1 (every process).
func killsEverything(args []tok) bool {
	for k := 0; k < len(args); k++ {
		switch a := args[k].l; {
		case a == "-1":
			return true
		case a == "-s" || a == "-n":
			k++
		case !isFlag(a):
			return false
		}
	}
	return false
}

// --- everything else -----------------------------------------------------

func checkSoft(sc *scan, h *hits) {
	checkDeletes(sc, h)
	checkPermissions(sc, h)
	checkSQL(sc, h)
	checkProcesses(sc, h)
	checkContainers(sc, h)
	checkSudo(sc, h)
	checkRunning(sc, h)
	checkWrites(sc, h)
	checkGit(sc, h)
	checkGateway(sc, h)
}

func checkDeletes(sc *scan, h *hits) {
	for _, i := range sc.verbs("rm") {
		if sc.over {
			return
		}
		args := sc.wordArgs(i, false)
		if len(args) > 0 && args[0].t != "--" && (strings.HasPrefix(args[0].l, "--") || shortCluster(args[0].l, "r")) {
			h.add("rm-sweep")
		}
		for _, a := range args {
			if a.t == "--" || a.q {
				break
			}
			if recursiveFlag(a.l) {
				h.add("rm-sweep")
			}
		}
		for _, a := range args {
			if strings.HasPrefix(a.t, "/") {
				h.add("rm-sweep")
			}
		}
	}
	for _, i := range sc.verbs("xargs") {
		if sc.over {
			return
		}
		for _, t := range sc.rest(i + 1) {
			if t.kind == kWord && hasBounded(t.l, "rm") {
				h.add("bulk-delete")
			}
		}
	}
	for _, i := range sc.verbs("find") {
		if sc.over {
			return
		}
		for j := i + 1; j < len(sc.toks); j++ {
			if sc.spend(1) {
				break
			}
			t := sc.toks[j]
			if t.kind != kWord {
				continue
			}
			if startsWord(t.l, "-delete") {
				h.add("bulk-delete")
			}
			if (t.l == "-exec" || t.l == "-execdir") && j+1 < len(sc.toks) && sc.toks[j+1].kind == kWord &&
				hasBounded(base(sc.toks[j+1].l), "rm") && base(sc.toks[j+1].l) == "rm" {
				h.add("bulk-delete")
			}
		}
	}
	for _, i := range sc.verbs("dd") {
		if sc.over {
			return
		}
		for _, t := range sc.rest(i + 1) {
			if t.kind == kWord && strings.HasPrefix(t.l, "if=") {
				h.add("dd-read")
			}
		}
	}
}

// recursiveFlag: -r, -R, -fr, ... or --recursive.
func recursiveFlag(l string) bool {
	if l == "--recursive" {
		return true
	}
	if !isFlag(l) || l[1] == '-' {
		return false
	}
	letters := 1
	for letters < len(l) && l[letters] >= 'a' && l[letters] <= 'z' {
		letters++
	}
	return strings.Contains(l[1:letters], "r") && (letters == len(l) || !isWordChar(l[letters]))
}

func checkPermissions(sc *scan, h *hits) {
	for _, i := range sc.verbs("chmod") {
		if sc.over {
			return
		}
		args := sc.wordArgs(i, false)
		if m, ok := firstOperand(args); ok && opensToAll(m.l) {
			h.add("chmod-open")
		}
		for _, a := range args {
			if a.l == "+x" {
				for j := i + 1; j+1 < len(sc.toks); j++ {
					if sc.spend(1) {
						break
					}
					s := sc.toks[j]
					if s.kind == kSep && (s.sep == sepSemi || s.sep == sepBg || s.sep == sepPipe || s.sep == sepAnd || s.sep == sepOr) &&
						strings.HasPrefix(sc.toks[j+1].t, "./") {
						h.add("exec-after-chmod")
					}
				}
			}
		}
	}
	for _, i := range sc.verbs("chown") {
		if sc.over {
			return
		}
		args := sc.wordArgs(i, false)
		recursive := false
		var owner string
		for _, a := range args {
			switch {
			case a.l == "r" || shortCluster(a.l, "r") || strings.HasPrefix(a.l, "--recur"):
				recursive = true
			case !isFlag(a.l) && owner == "":
				owner = a.l
			}
		}
		if recursive && (strings.HasPrefix(owner, "root") || strings.Contains(owner, ":root")) {
			h.add("chown-root-tree")
		}
	}
}

// opensToAll: a mode that gives "other" write permission. Four-digit modes
// (sticky bit and friends) are left to the person.
func opensToAll(mode string) bool {
	if len(mode) == 3 && allDigits(mode) {
		return mode[2] >= '0' && mode[2] <= '7' && (mode[2]-'0')&2 != 0
	}
	for _, clause := range strings.Split(mode, ",") {
		i := strings.IndexAny(clause, "+=")
		if i < 0 || i == 0 {
			continue
		}
		who, perms := clause[:i], clause[i+1:]
		if strings.Trim(who, "ugoa") == "" && strings.ContainsAny(who, "oa") && strings.Contains(perms, "w") &&
			strings.Trim(perms, "rwxXst") == "" {
			return true
		}
	}
	return false
}

func checkSQL(sc *scan, h *hits) {
	word := func(i int) string {
		if i >= len(sc.toks) || sc.toks[i].kind != kWord || sc.toks[i].data {
			return ""
		}
		return strings.Trim(sc.toks[i].l, `()[]"';,`)
	}
	for i := range sc.toks {
		switch word(i) {
		case "drop":
			if w := word(i + 1); w == "table" || w == "database" {
				h.add("sql-destroy")
			}
		case "delete":
			if word(i+1) != "from" {
				break
			}
			hasWhere := false
			for _, t := range sc.rest(i + 2) {
				if t.line != sc.toks[i].line {
					break
				}
				if t.kind == kWord && hasBounded(t.l, "where") {
					hasWhere = true
				}
			}
			if !hasWhere {
				h.add("sql-destroy")
			}
		case "truncate":
			if i+1 < len(sc.toks) && sc.toks[i+1].kind == kWord && isWordChar(sc.toks[i+1].t[0]) {
				h.add("sql-destroy")
			}
		}
	}
}

// subcommands walks the options in front of a tool's subcommand (an option
// may or may not carry a value) and returns the indexes where the
// subcommand word could be.
func subcommands(args []tok) []int {
	var out []int
	for k := 0; k < len(args); k++ {
		if isFlag(args[k].t) {
			continue
		}
		out = append(out, k)
		if k == 0 || !isFlag(args[k-1].t) {
			break
		}
	}
	return out
}

func checkProcesses(sc *scan, h *hits) {
	for _, i := range sc.verbs("systemctl") {
		if sc.over {
			return
		}
		if v, ok := firstOperand(sc.wordArgs(i, false)); ok {
			for _, w := range []string{"stop", "restart", "disable", "mask"} {
				if startsWord(v.l, w) {
					h.add("systemctl-stop")
				}
			}
		}
	}
	for _, i := range sc.verbs("pkill") {
		if sc.over {
			return
		}
		args := sc.wordArgs(i, false)
		if forcesKill(args) {
			h.add("kill-force")
		}
		mentionsSelf(sc, i, h)
	}
	for _, i := range sc.verbs("killall") {
		if sc.over {
			return
		}
		args := sc.wordArgs(i, false)
		if forcesKill(args) || anyArg(args, func(a string) bool { return a == "-r" || strings.HasPrefix(a, "--regexp") }) {
			h.add("kill-force")
		}
		mentionsSelf(sc, i, h)
	}
	for _, i := range sc.verbs("kill") {
		if sc.over {
			return
		}
		for j := i + 1; j+1 < len(sc.toks); j++ {
			if sc.spend(1) {
				break
			}
			s := sc.toks[j]
			if s.kind == kSep && s.sep == sepSubst && sc.toks[j+1].kind == kWord &&
				(startsWord(sc.toks[j+1].l, "pgrep") || startsWord(sc.toks[j+1].l, "pidof")) {
				h.add("kill-self")
			}
		}
	}
}

// forcesKill: -9, -KILL or -s KILL among the arguments.
func forcesKill(args []tok) bool {
	for k, a := range args {
		switch a.l {
		case "-9", "-kill", "-sigkill":
			return true
		case "-s":
			if k+1 < len(args) {
				switch args[k+1].l {
				case "9", "kill", "sigkill":
					return true
				}
			}
		}
	}
	return false
}

// mentionsSelf: a kill command that names the harness or its gateway.
func mentionsSelf(sc *scan, i int, h *hits) {
	for _, t := range sc.rest(i + 1) {
		if t.kind != kWord {
			continue
		}
		for _, w := range []string{"son-of-anton", "gateway", "cli.py"} {
			if hasBounded(t.l, w) {
				h.add("kill-self")
			}
		}
	}
}

func checkContainers(sc *scan, h *hits) {
	for _, t := range sc.toks {
		if t.kind != kWord || t.data {
			continue
		}
		for _, v := range []string{"docker_host=", "docker_context=", "container_host=", "container_connection="} {
			if strings.HasPrefix(t.l, v) && len(t.l) > len(v) {
				h.add("container-remote")
			}
		}
	}
	for _, i := range sc.verbs("docker") {
		if sc.over {
			return
		}
		args := sc.wordArgs(i, false)
		dockerOptions(args, h)
		dockerLifecycle(args, h)
	}
	for _, i := range sc.verbs("docker-compose") {
		if sc.over {
			return
		}
		composeLifecycle(sc.wordArgs(i, false), h)
	}
	for _, i := range sc.verbs("podman") {
		if sc.over {
			return
		}
		args := sc.wordArgs(i, false)
		region := subcommands(args)
		end := len(args)
		if len(region) > 0 {
			end = region[len(region)-1]
		}
		for k := 0; k < end; k++ {
			a := args[k].l
			switch {
			case a == "-r" || a == "-c", strings.HasPrefix(a, "--remote"), strings.HasPrefix(a, "--url"),
				strings.HasPrefix(a, "--connection"), strings.HasPrefix(a, "--identity"):
				h.add("container-remote")
			}
		}
	}
}

// dockerOptions flags a host or context option placed before the
// subcommand, where it redirects the whole command to another daemon.
func dockerOptions(args []tok, h *hits) {
	region := subcommands(args)
	end := len(args)
	if len(region) > 0 {
		end = region[len(region)-1]
	}
	for k := 0; k < end; k++ {
		a := args[k].l
		hasValue := k+1 < len(args) && !isFlag(args[k+1].t)
		switch {
		case a == "-h" || a == "--host" || a == "-c" || a == "--context":
			if hasValue {
				h.add("container-remote")
			}
		case strings.HasPrefix(a, "--host=") || strings.HasPrefix(a, "--context="):
			h.add("container-remote")
		case !strings.HasPrefix(a, "--") && len(a) > 2 && (a[:2] == "-h" || a[:2] == "-c"):
			h.add("container-remote")
		}
	}
	for _, k := range region {
		if args[k].l == "context" && k+1 < len(args) && startsWord(args[k+1].l, "use") {
			h.add("container-remote")
		}
	}
}

func dockerLifecycle(args []tok, h *hits) {
	for _, k := range subcommands(args) {
		switch w := args[k].l; {
		case startsWord(w, "restart"), startsWord(w, "stop"), startsWord(w, "kill"):
			h.add("container-lifecycle")
		case w == "compose" || w == "container":
			rest := args[k+1:]
			if w == "compose" {
				composeLifecycle(rest, h)
			} else {
				for _, j := range subcommands(rest) {
					for _, v := range []string{"restart", "stop", "kill"} {
						if startsWord(rest[j].l, v) {
							h.add("container-lifecycle")
						}
					}
				}
			}
		}
	}
}

func composeLifecycle(args []tok, h *hits) {
	for _, k := range subcommands(args) {
		for _, v := range []string{"restart", "stop", "kill", "down"} {
			if startsWord(args[k].l, v) {
				h.add("container-lifecycle")
			}
		}
	}
}

func checkSudo(sc *scan, h *hits) {
	for _, i := range sc.verbs("sudo") {
		if sc.over {
			return
		}
		for _, t := range sc.wordArgs(i, true) {
			if !isFlag(t.l) {
				continue
			}
			if strings.HasPrefix(t.l, "--") {
				if strings.HasPrefix(t.l, "--st") || strings.HasPrefix(t.l, "--a") {
					h.add("sudo-escalation")
				}
			} else if strings.ContainsAny(t.l[1:], "sa") && strings.Trim(t.l[1:], "abcdefghijklmnopqrstuvwxyz") == "" {
				h.add("sudo-escalation")
			}
		}
	}
	// sudo-rs and the like are sudo too.
	for i, t := range sc.toks {
		if t.kind == kWord && !t.data && strings.HasPrefix(t.l, "sudo-") {
			for _, a := range sc.wordArgs(i, true) {
				if shortCluster(a.l, "sa") {
					h.add("sudo-escalation")
				}
			}
		}
	}
}

var fetchers = map[string]bool{"curl": true, "wget": true}

var decoders = map[string]bool{"base64": true, "base32": true, "base16": true, "xxd": true, "tr": true, "openssl": true}

func checkRunning(sc *scan, h *hits) {
	for i, t := range sc.toks {
		if t.kind != kWord || t.data {
			continue
		}
		if sc.over {
			return
		}
		name := base(t.l)
		switch {
		case fetchers[name] && sc.pipedIntoShell(i):
			h.add("fetch-and-run")
		case decoders[name] && sc.pipedIntoShell(i) && isDecoder(name, sc.wordArgs(i, false)):
			h.add("decode-and-run")
		}
		if shells[name] && i+1 < len(sc.toks) && sc.toks[i+1].kind == kRedir && strings.HasPrefix(sc.toks[i+1].l, "<<") {
			h.add("shell-from-stdin")
		}
		if shells[name] || ((name == "eval" || name == "source" || name == ".") && isSegmentCommand(sc, i)) {
			j := i + 1
			for j < len(sc.toks) && (sc.toks[j].kind == kWord && isFlag(sc.toks[j].t) || sc.toks[j].kind == kRedir && sc.toks[j].l == "<") {
				if sc.spend(1) {
					return
				}
				j++
			}
			if j+1 < len(sc.toks) && sc.toks[j].kind == kSep && (sc.toks[j].sep == sepSubst || sc.toks[j].sep == sepProc) &&
				sc.toks[j+1].kind == kWord && fetchers[base(sc.toks[j+1].l)] {
				h.add("fetch-and-run")
			}
		}
	}
}

func isSegmentCommand(sc *scan, i int) bool {
	for s := i; s >= 0 && sc.toks[s].seg == sc.toks[i].seg; s-- {
		if sc.spend(1) {
			return false
		}
		if sc.isSegStart(s) {
			return sc.chainAt(s).cmd == i
		}
	}
	return false
}

// isDecoder: a command that turns encoded text back into something a shell
// could run.
func isDecoder(name string, args []tok) bool {
	has := func(opts ...string) bool {
		return anyArg(args, func(a string) bool {
			for _, o := range opts {
				if a == o {
					return true
				}
			}
			return false
		})
	}
	switch name {
	case "base64", "base32", "base16":
		return has("-d", "--decode")
	case "xxd":
		return has("-r")
	case "tr":
		return true
	case "openssl":
		return has("-d") && has("base64", "enc")
	}
	return false
}

func checkWrites(sc *scan, h *hits) {
	note := func(raw string) {
		if k := classifyTarget(raw); k != tNone {
			h.add(targetRule[k])
		}
	}
	for i, t := range sc.toks {
		if t.kind == kRedir && !t.data && strings.Contains(t.l, ">") && !strings.HasSuffix(t.l, "&") &&
			i+1 < len(sc.toks) && sc.toks[i+1].kind == kWord {
			note(sc.toks[i+1].l)
		}
	}
	for _, i := range sc.verbs("tee") {
		if sc.over {
			return
		}
		for _, t := range sc.rest(i + 1) {
			if t.kind == kWord && !t.data {
				note(t.l)
			}
		}
	}
	for _, verb := range []string{"cp", "mv", "install"} {
		for _, i := range sc.verbs(verb) {
			if sc.over {
				return
			}
			var ops []tok
			for _, a := range sc.wordArgs(i, false) {
				if !isFlag(a.t) {
					ops = append(ops, a)
				}
			}
			for _, o := range ops {
				if strings.HasPrefix(o.l, "/etc/") {
					h.add("write-system-path")
				}
			}
			if len(ops) > 0 {
				note(ops[len(ops)-1].l)
			}
		}
	}
	for _, editor := range []string{"sed", "perl", "ruby"} {
		for _, i := range sc.verbs(editor) {
			if sc.over {
				return
			}
			args := sc.wordArgs(i, false)
			inPlace := false
			for _, a := range args {
				if editor == "sed" && isFlag(a.t) && strings.Contains(a.l, "i") || editor != "sed" && shortCluster(a.l, "i") {
					inPlace = true
				}
			}
			if !inPlace {
				continue
			}
			for _, a := range args {
				note(a.l)
			}
		}
	}
}

func checkGit(sc *scan, h *hits) {
	for _, i := range sc.verbs("git") {
		if sc.over {
			return
		}
		args := sc.wordArgs(i, false)
		k := 0
		for k < len(args) && isFlag(args[k].t) {
			if args[k].t == "-C" || args[k].t == "-c" {
				k++
			}
			k++
		}
		if k >= len(args) {
			continue
		}
		rest := args[k+1:]
		switch args[k].l {
		case "reset":
			if anyArg(rest, func(a string) bool { return len(a) >= 3 && strings.HasPrefix("--hard", a) }) {
				h.add("git-reset-hard")
			}
		case "push":
			if anyArg(rest, func(a string) bool {
				return strings.HasPrefix(a, "--forc") || shortCluster(a, "f") || strings.HasPrefix(a, "+") || endsWithDashF(a)
			}) {
				h.add("git-force-push")
			}
		case "clean":
			if anyArg(rest, func(a string) bool { return a == "--force" || shortCluster(a, "f") }) {
				h.add("git-clean")
			}
		case "branch":
			if anyArg(rest, func(a string) bool {
				return shortCluster(a, "d") || strings.HasPrefix(a, "--del") || shortCluster(a, "D")
			}) {
				h.add("git-branch-delete")
			}
		}
	}
}

// endsWithDashF: some token holds "-f" followed by a non-word character or
// its end ("feature-f" counts: a branch spelled that way is treated like the
// option, which errs towards asking).
func endsWithDashF(s string) bool {
	for from := 0; ; {
		i := strings.Index(s[from:], "-f")
		if i < 0 {
			return false
		}
		end := from + i + 2
		if end == len(s) || !isWordChar(s[end]) {
			return true
		}
		from += i + 1
	}
}

func checkGateway(sc *scan, h *hits) {
	for _, i := range sc.verbs("son-of-anton") {
		if sc.over {
			return
		}
		args := sc.wordArgs(i, false)
		for _, k := range subcommands(args) {
			if args[k].l == "gateway" && k+1 < len(args) {
				if startsWord(args[k+1].l, "stop") || startsWord(args[k+1].l, "restart") {
					h.add("gateway-control")
				}
			}
		}
	}
	for i := 0; i+1 < len(sc.toks); i++ {
		if sc.toks[i].kind != kWord || sc.toks[i].l != "gateway" || sc.toks[i+1].kind != kWord || !startsWord(sc.toks[i+1].l, "run") {
			continue
		}
		for _, t := range sc.rest(i + 2) {
			if t.kind == kWord && (startsWord(t.l, "disown") || startsWord(t.l, "setsid")) {
				h.add("gateway-control")
			}
		}
		for j := i + 2; j < len(sc.toks); j++ {
			if sc.spend(1) {
				break
			}
			if sc.toks[j].kind != kSep || sc.toks[j].sep != sepBg {
				continue
			}
			n := j + 1
			for n < len(sc.toks) && sc.toks[n].kind == kSep && sc.toks[n].sep == sepNL {
				n++
			}
			if n == len(sc.toks) || (sc.toks[n].kind == kSep && sc.toks[n].sep == sepSemi) {
				h.add("gateway-control")
			}
		}
		for _, v := range sc.verbs("nohup") {
			if v < i {
				h.add("gateway-control")
			}
		}
	}
}
