package approval

import (
	"os"
	"regexp"
	"strings"
	"sync"

	"golang.org/x/text/unicode/norm"
)

// The detector: deobfuscate the text, lex it into tokens (scan.go), ask the
// rules in rules.go what they see, and classify the payload of any
// `bash -c` the same way. Every finding has a key (what "always allow"
// remembers) and a description (what the person is shown).

const (
	maxCommandChars     = 128_000
	maxSeparatorFreeLen = 4_096
	maxSegments         = 25_000
)

// ruleInfo describes one thing the detector can report.
type ruleInfo struct {
	key, desc string
	hard      bool // blocked in every mode, yolo included
}

var ruleList = []ruleInfo{
	// The floor.
	{"wipe-tree", "delete of the filesystem root, a system directory or the home directory", true},
	{"halt-machine", "shutdown, reboot or runlevel change", true},
	{"raw-disk-write", "write to a raw block device", true},
	{"format-disk", "make a filesystem (mkfs)", true},
	{"fork-bomb", "fork bomb", true},
	{"kill-everything", "signal every process (kill -1)", true},
	{"sudo-stdin-shell", "sudo taking its password from stdin or starting a shell", true},

	// Deleting and permissions.
	{"rm-sweep", "delete that is recursive, uses a long option, or names an absolute path", false},
	{"chmod-open", "permissions that let everyone write", false},
	{"chown-root-tree", "recursive chown to root", false},
	{"bulk-delete", "delete in bulk (xargs rm, find -exec rm, find -delete)", false},
	{"dd-read", "dd with an input file", false},
	{"sql-destroy", "SQL that drops, truncates or deletes without a WHERE", false},

	// Processes and services.
	{"systemctl-stop", "stop, restart, disable or mask a service", false},
	{"kill-force", "force-kill processes (pkill -9, killall -9, killall -r)", false},
	{"kill-self", "kill son-of-anton or gateway processes", false},
	{"gateway-control", "stop, restart or detach the son-of-anton gateway", false},
	{"container-remote", "container tool pointed at another daemon or host", false},
	{"container-lifecycle", "stop, restart or kill containers", false},
	{"sudo-escalation", "sudo with an option that reads a password, asks a helper or opens a shell", false},

	// Running what was fetched or decoded.
	{"fetch-and-run", "run remote content in a shell", false},
	{"decode-and-run", "run decoded content in a shell (possible obfuscation)", false},
	{"shell-from-stdin", "run a shell on a here-document or here-string", false},
	{"exec-after-chmod", "chmod +x followed by running the file", false},

	// Writing to places that matter.
	{"write-system-path", "write to /etc or a disk device", false},
	{"write-credential-path", "write to SSH, shell start-up or credential files", false},
	{"write-soa-config", "write to son-of-anton's config.toml or .env", false},
	{"write-project-config", "write to a .env or config.toml file", false},

	// git.
	{"git-reset-hard", "git reset --hard", false},
	{"git-force-push", "git force push", false},
	{"git-clean", "git clean with force", false},
	{"git-branch-delete", "git branch delete", false},
}

var ruleByKey = func() map[string]ruleInfo {
	m := make(map[string]ruleInfo, len(ruleList))
	for _, r := range ruleList {
		m[r.key] = r
	}
	return m
}()

// PatternKeys lists the key of every rule Classify can report, without the
// parser limit.
func PatternKeys() []string {
	keys := make([]string, 0, len(ruleList))
	for _, r := range ruleList {
		keys = append(keys, r.key)
	}
	return keys
}

// hits collects the rules a command trips, once each.
type hits struct {
	seen  map[string]bool
	order []string
}

func (h *hits) add(key string) {
	if h.seen == nil {
		h.seen = map[string]bool{}
	}
	if !h.seen[key] {
		h.seen[key] = true
		h.order = append(h.order, key)
	}
}

// Classify returns every blocklist hit for a command, hardline first.
func Classify(command string) []Finding {
	return classify(command, 0)
}

func classify(command string, depth int) []Finding {
	if overParserLimit(command) {
		return []Finding{{Key: "parser-limit", Description: "command parser limit exceeded", Hardline: true}}
	}
	sc := lex(deobfuscate(maskQuotedNewlines(command)))
	var h hits
	checkHard(sc, &h)
	checkSoft(sc, &h)
	if sc.over {
		return []Finding{{Key: "parser-limit", Description: "command parser limit exceeded", Hardline: true}}
	}
	findings := make([]Finding, 0, len(h.order))
	for _, hard := range []bool{true, false} {
		for _, k := range h.order {
			if r := ruleByKey[k]; r.hard == hard {
				findings = append(findings, Finding{Key: k, Description: r.desc, Hardline: hard})
			}
		}
	}
	if depth < 2 {
		for _, p := range sc.payloads() {
			findings = append(findings, classify(p, depth+1)...)
		}
	}
	return dedupe(findings)
}

func dedupe(fs []Finding) []Finding {
	seen := map[string]bool{}
	out := fs[:0:0]
	for _, f := range fs {
		id := f.Key
		if f.Hardline {
			id += "!"
		}
		if !seen[id] {
			seen[id] = true
			out = append(out, f)
		}
	}
	return out
}

// overParserLimit refuses commands too large or too knotted to scan with
// confidence; they are treated as hardline rather than waved through.
func overParserLimit(command string) bool {
	if len(command) > maxCommandChars {
		return true
	}
	seps := strings.Count(command, ";") + strings.Count(command, "&") + strings.Count(command, "|") + strings.Count(command, "\n")
	if len(command) > maxSeparatorFreeLen && seps == 0 {
		return true
	}
	return seps >= maxSegments
}

// --- deobfuscation -------------------------------------------------------

var (
	terminalCodes = regexp.MustCompile(`\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]`)
	lineJoin      = regexp.MustCompile(`\\\r?\n`)
	homeVariable  = regexp.MustCompile(`\$\{HOME\}|\$HOME\b`)
	backslashed   = regexp.MustCompile(`\\([^\n])`)
	emptyQuotes   = regexp.MustCompile(`''|""`)
	ifsVariable   = regexp.MustCompile(`\$\{IFS\b[^}]*\}|\$IFS\b`)
)

// deobfuscate undoes the ways a command can be spelled so that its words are
// not the words it runs: terminal escapes and NULs removed, compatibility
// forms folded (full-width letters), line continuations joined, the
// instance and user homes written as ~/.son-of-anton and ~, backslashes and
// empty quotes dropped, $IFS read as a blank.
func deobfuscate(command string) string {
	stages := []func(string) string{
		func(s string) string { return terminalCodes.ReplaceAllString(s, "") },
		func(s string) string { return strings.ReplaceAll(s, "\x00", "") },
		norm.NFKC.String,
		func(s string) string { return lineJoin.ReplaceAllString(s, "") },
		func(s string) string { return foldHome(s, instanceHome(), "~/.son-of-anton") },
		func(s string) string { return foldHome(s, userHome(), "~") },
		func(s string) string { return homeVariable.ReplaceAllString(s, "~") },
		func(s string) string { return backslashed.ReplaceAllString(s, "$1") },
		func(s string) string { return emptyQuotes.ReplaceAllString(s, "") },
		func(s string) string { return ifsVariable.ReplaceAllString(s, " ") },
	}
	for _, f := range stages {
		command = f(command)
	}
	return command
}

func userHome() string {
	if h := os.Getenv("HOME"); h != "" {
		return h
	}
	if h, err := os.UserHomeDir(); err == nil {
		return h
	}
	return ""
}

// instanceHome is SON_OF_ANTON_HOME, else ~/.son-of-anton.
func instanceHome() string {
	if h := os.Getenv("SON_OF_ANTON_HOME"); h != "" {
		return h
	}
	if h := userHome(); h != "" {
		return h + "/.son-of-anton"
	}
	return ""
}

var homeMatchers sync.Map // home path -> *regexp.Regexp

// foldHome rewrites an absolute home path used as a directory prefix to
// short. A home with fewer than two path components (unset, "/", "/x") is
// never folded, so a stray value cannot rewrite unrelated paths, and a bare
// home with nothing under it is left alone.
func foldHome(s, home, short string) string {
	var parts []string
	for _, c := range strings.Split(home, "/") {
		if c != "" {
			parts = append(parts, regexp.QuoteMeta(c))
		}
	}
	if len(parts) < 2 {
		return s
	}
	re, ok := homeMatchers.Load(home)
	if !ok {
		re = regexp.MustCompile(`/*` + strings.Join(parts, `/+`) + "(?P<below>(?:/[^/\\s'\"`;|&<>()]*)+)")
		homeMatchers.Store(home, re)
	}
	return re.(*regexp.Regexp).ReplaceAllString(s, short+"${below}")
}

// maskQuotedNewlines writes a blank for every newline inside quotes: there
// it is data, not the end of a command. It runs on the raw command, before
// deobfuscation.
func maskQuotedNewlines(command string) string {
	if !strings.Contains(command, "\n") {
		return command
	}
	out := []byte(command)
	var quote byte
	for i := 0; i < len(out); i++ {
		c := out[i]
		switch {
		case quote == '"' && c == '\\':
			i++
		case quote != 0 && c == quote:
			quote = 0
		case quote != 0 && c == '\n':
			out[i] = ' '
		case quote == 0 && c == '\\':
			i++
		case quote == 0 && (c == '\'' || c == '"'):
			quote = c
		}
	}
	return string(out)
}
