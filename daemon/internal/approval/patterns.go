package approval

import (
	"os"
	"regexp"
	"strings"

	"golang.org/x/text/unicode/norm"
)

// The pattern data is ported from the Python fork's tools/approval.py
// (DANGEROUS_PATTERNS and HARDLINE_PATTERNS, plus the detection
// normalization). Three Python patterns used lookarounds, which RE2 does not
// support; they are implemented as small code checks instead. The
// son-of-anton config patterns target config.toml (this daemon's config
// file), where the Python ones target config.yaml.

const (
	maxCommandChars     = 128_000
	maxSeparatorFreeLen = 4_096
	maxSegments         = 25_000
)

var (
	ansiRe         = regexp.MustCompile(`\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]`)
	continuationRe = regexp.MustCompile(`\\(?:\r?\n)`)
	homeVarRe      = regexp.MustCompile(`\$\{HOME\}|\$HOME\b`)
	escapeRe       = regexp.MustCompile(`\\([^\n])`)
	emptyQuoteRe   = regexp.MustCompile(`''|""`)
	ifsRe          = regexp.MustCompile(`\$\{IFS[^}]*\}|\$IFS(?:\b|$)`)
)

// normalize de-obfuscates a command the way the Python detection layer does:
// ANSI codes and NULs out, NFKC, line continuations folded, the absolute user
// home written as ~, backslash escapes dropped, empty quotes dropped, $IFS
// collapsed to a space.
func normalize(command string) string {
	s := ansiRe.ReplaceAllString(command, "")
	s = strings.ReplaceAll(s, "\x00", "")
	s = norm.NFKC.String(s)
	s = continuationRe.ReplaceAllString(s, "")
	if home := userHome(); home != "" && len(home) > 2 {
		s = strings.ReplaceAll(s, home+"/", "~/")
		s = strings.ReplaceAll(s, home, "~")
	}
	s = homeVarRe.ReplaceAllString(s, "~")
	s = escapeRe.ReplaceAllString(s, "$1")
	s = emptyQuoteRe.ReplaceAllString(s, "")
	s = ifsRe.ReplaceAllString(s, " ")
	return s
}

func userHome() string {
	if h, err := os.UserHomeDir(); err == nil {
		return h
	}
	return ""
}

// markSegments turns the shell separators into newlines (outside quotes), so
// "command position" means "start of line".
func markSegments(command string) string {
	var b strings.Builder
	b.Grow(len(command) + 8)
	inSingle, inDouble := false, false
	for i := 0; i < len(command); i++ {
		c := command[i]
		switch {
		case inSingle:
			b.WriteByte(c)
			if c == '\'' {
				inSingle = false
			}
		case inDouble:
			b.WriteByte(c)
			switch c {
			case '\\':
				if i+1 < len(command) {
					i++
					b.WriteByte(command[i])
				}
			case '"':
				inDouble = false
			}
		default:
			switch c {
			case '\\':
				if i+1 < len(command) {
					i++
					b.WriteByte(command[i])
				}
			case '\'':
				inSingle = true
				b.WriteByte(c)
			case '"':
				inDouble = true
				b.WriteByte(c)
			case ';', '\n', '(', '{', '`':
				b.WriteByte('\n')
			case '&':
				if i+1 < len(command) && command[i+1] == '&' {
					i++
				}
				b.WriteByte('\n')
			case '|':
				if i+1 < len(command) && command[i+1] == '|' {
					i++
				}
				b.WriteByte('\n')
			case '$':
				if i+1 < len(command) && command[i+1] == '(' {
					i++
					b.WriteByte('\n')
				} else {
					b.WriteByte(c)
				}
			default:
				b.WriteByte(c)
			}
		}
	}
	return b.String()
}

func parserLimitExceeded(command string) bool {
	if len(command) > maxCommandChars {
		return true
	}
	if len(command) > maxSeparatorFreeLen && !strings.ContainsAny(command, ";&|\n") {
		return true
	}
	n := 0
	for i := 0; i < len(command); i++ {
		if command[i] == ';' || command[i] == '&' || command[i] == '|' || command[i] == '\n' {
			n++
			if n >= maxSegments {
				return true
			}
		}
	}
	return false
}

// hideGrepPatterns blanks the quoted operands of search commands, whose
// pattern argument is data, not a command. It works on the unmarked string:
// a segment starts at position 0 and after each quote-aware `;`, `&`, `|`,
// newline, `(`, `{`, “ ` “ or `$(`. A quote left unterminated disables
// hiding entirely (fail closed).
var grepTools = map[string]bool{"grep": true, "egrep": true, "rg": true, "ag": true, "ack": true}

type quotedSpan struct{ start, end int }

// quotedSpans returns every quoted span (quotes included) and whether all
// quotes are balanced.
func quotedSpans(s string) ([]quotedSpan, bool) {
	var spans []quotedSpan
	inSingle, inDouble := false, false
	start := 0
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case inSingle:
			if c == '\'' {
				inSingle = false
				spans = append(spans, quotedSpan{start, i + 1})
			}
		case inDouble:
			switch c {
			case '\\':
				i++
			case '"':
				inDouble = false
				spans = append(spans, quotedSpan{start, i + 1})
			}
		case c == '\'':
			inSingle = true
			start = i
		case c == '"':
			inDouble = true
			start = i
		}
	}
	return spans, !inSingle && !inDouble
}

func hideGrepPatterns(command string) string {
	spans, balanced := quotedSpans(command)
	if !balanced {
		return command
	}
	cuts := segmentCuts(command)
	out := make([]byte, len(command))
	copy(out, command)
	for _, sp := range spans {
		segStart := 0
		for _, c := range cuts {
			if c <= sp.start {
				segStart = c
			} else {
				break
			}
		}
		if !isGrepSegment(command[segStart:sp.start]) {
			continue
		}
		for i := sp.start + 1; i < sp.end-1; i++ {
			if out[i] != ' ' && out[i] != '	' {
				out[i] = ' '
			}
		}
	}
	return string(out)
}

// segmentCuts returns the byte offsets where a new shell segment starts.
func segmentCuts(s string) []int {
	cuts := []int{0}
	inSingle, inDouble := false, false
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case inSingle:
			if c == '\'' {
				inSingle = false
			}
		case inDouble:
			switch c {
			case '\\':
				i++
			case '"':
				inDouble = false
			}
		case c == '\\':
			i++
		case c == '\'':
			inSingle = true
		case c == '"':
			inDouble = true
		case c == ';' || c == '\n' || c == '(' || c == '{' || c == '`':
			cuts = append(cuts, i+1)
		case c == '&':
			if i+1 < len(s) && s[i+1] == '&' {
				i++
			}
			cuts = append(cuts, i+1)
		case c == '|':
			if i+1 < len(s) && s[i+1] == '|' {
				i++
			}
			cuts = append(cuts, i+1)
		case c == '$' && i+1 < len(s) && s[i+1] == '(':
			i++
			cuts = append(cuts, i+1)
		}
	}
	return cuts
}

// isGrepSegment reports whether the segment's first word is a search tool.
func isGrepSegment(prefix string) bool {
	i := 0
	for i < len(prefix) && (prefix[i] == ' ' || prefix[i] == '	') {
		i++
	}
	wordStart := i
	for i < len(prefix) && prefix[i] != ' ' && prefix[i] != '	' {
		i++
	}
	word := strings.ToLower(prefix[wordStart:i])
	if len(word) >= 2 && (word[0] == '\'' || word[0] == '"') && word[len(word)-1] == word[0] {
		word = word[1 : len(word)-1]
	}
	return grepTools[lowerBasename(word)]
}

// interpreterPayloads extracts the payloads of `bash|sh|zsh|ksh|dash -c
// <payload>` so they are classified as commands in their own right.
var interpreters = map[string]bool{"bash": true, "sh": true, "zsh": true, "ksh": true, "dash": true}

func interpreterPayloads(marked string) []string {
	var payloads []string
	for _, line := range strings.Split(marked, "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 || !interpreters[lowerBasename(fields[0])] {
			continue
		}
		for i := 1; i+1 < len(fields); i++ {
			if fields[i] == "-c" {
				payloads = append(payloads, unquote(fields[i+1]))
				break
			}
		}
	}
	return payloads
}

func unquote(s string) string {
	if len(s) >= 2 {
		if (s[0] == '\'' && s[len(s)-1] == '\'') || (s[0] == '"' && s[len(s)-1] == '"') {
			return s[1 : len(s)-1]
		}
	}
	return s
}

func lowerBasename(p string) string {
	if i := strings.LastIndexByte(p, '/'); i >= 0 {
		p = p[i+1:]
	}
	return strings.ToLower(p)
}

// Classify returns every blocklist hit for a command, hardline first.
func Classify(command string) []Finding {
	return classify(command, 0)
}

func classify(command string, depth int) []Finding {
	normalized := normalize(command)
	if parserLimitExceeded(normalized) {
		return []Finding{{Key: "parser-limit", Description: "command parser limit exceeded", Hardline: true}}
	}
	marked := markSegments(normalized)

	findings := []Finding{}
	if sudoStdinGuard.MatchString(marked) {
		findings = append(findings, Finding{Key: "sudo-stdin", Description: "sudo with -S (password via stdin)", Hardline: true})
	}
	findings = append(findings, hardlineFindings(normalized, marked)...)

	// Soft patterns match the unmarked string (the pipes must stay intact);
	// quoted grep operands are blanked first.
	hidden := hideGrepPatterns(normalized)
	for _, p := range compiledDangerPatterns {
		matched := false
		if p.re != nil {
			matched = p.re.MatchString(hidden)
		} else if p.custom != nil {
			matched = p.custom(hidden)
		}
		if matched {
			findings = append(findings, Finding{Key: p.key, Description: p.description})
		}
	}

	if depth < 2 {
		for _, payload := range interpreterPayloads(marked) {
			findings = append(findings, classify(payload, depth+1)...)
		}
	}
	return dedupeFindings(findings)
}

func dedupeFindings(findings []Finding) []Finding {
	seen := map[string]bool{}
	out := findings[:0:0]
	for _, f := range findings {
		if seen[f.Key] {
			continue
		}
		seen[f.Key] = true
		out = append(out, f)
	}
	return out
}

// --- hardline patterns ---------------------------------------------------

var sudoStdinGuard = regexp.MustCompile(`(?m)^\s*sudo\s+-S\b`)

// hardlineRM: `rm <flags> <path>` at segment start, where the path is the
// filesystem root (with /.. and * variants), a system directory, or the
// home directory — ported from _HARDLINE_RM_PATH and the system-dirs list.
var hardlineRM = regexp.MustCompile(`(?i)^rm\s+(-\S+\s+)*(["\']?)(/(?:(?:\.\.?)?/)*(?:\.\.?)?\**|(?:/home|/root|/etc|/usr|/var|/bin|/sbin|/boot|/lib)(?:/\*)?|(?:~|\$\{?HOME\}?)(?:/?|/\*)?)(["\']?)(?:[\s);|&]|$)`)

var hardlineSegmentPatterns = []struct {
	key, desc string
	re        *regexp.Regexp
}{
	{"mkfs", "file system format (mkfs)", regexp.MustCompile(`(?i)^mkfs(\.[a-z0-9]+)?\b`)},
	{"init-shutdown", "init 0/6 (shutdown/reboot)", regexp.MustCompile(`(?i)^init\s+[06]\b`)},
	{"systemctl-shutdown", "systemctl poweroff/reboot", regexp.MustCompile(`(?i)^systemctl\s+(poweroff|reboot|halt|kexec)\b`)},
	{"telinit-shutdown", "telinit 0/6 (shutdown/reboot)", regexp.MustCompile(`(?i)^telinit\s+[06]\b`)},
	{"kill-all", "kill all processes", regexp.MustCompile(`(?i)^kill\s+(-[\s]+)*-1\b`)},
	{"system-shutdown", "system shutdown/reboot", regexp.MustCompile(`(?i)^(shutdown|reboot|halt|poweroff)\b`)},
}

var (
	hardlineDD       = regexp.MustCompile(`(?i)\bdd\b[^\n]*\bof=/dev/(?:sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*`)
	hardlineBlockDev = regexp.MustCompile(`(?i)>\s*/dev/(?:sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b`)
	forkBomb         = regexp.MustCompile(`(?s):\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:`)
)

// stripWrappers removes the optional sudo/env/exec/nohup/setsid/time/timeout
// prefixes that may precede a command at segment start.
var (
	wrapperSudo    = regexp.MustCompile(`(?i)^sudo\s+(-\S+\s+)*`)
	wrapperEnv     = regexp.MustCompile(`(?i)^env\s+(\w+=\S*\s+)+`)
	wrapperOnce    = regexp.MustCompile(`(?i)^(?:exec|nohup|setsid|time)\s+`)
	wrapperTimeout = regexp.MustCompile(`(?i)^timeout\s+\S+\s+`)
)

func stripWrappers(segment string) string {
	s := strings.TrimLeft(segment, " \t")
	for i := 0; i < 8; i++ {
		before := s
		s = wrapperSudo.ReplaceAllString(s, "")
		s = wrapperEnv.ReplaceAllString(s, "")
		s = wrapperOnce.ReplaceAllString(s, "")
		s = wrapperTimeout.ReplaceAllString(s, "")
		s = strings.TrimLeft(s, " \t")
		if s == before {
			break
		}
	}
	return s
}

// hardlineFindings returns the command-position hardline hits. The fork bomb,
// dd and block-device-redirect checks run against the unmarked string (their
// pipes would be split into newlines by marking); the rest run per segment.
func hardlineFindings(normalized, marked string) []Finding {
	var out []Finding
	// Whole-command hardlines (no command-position requirement).
	if forkBomb.MatchString(normalized) {
		out = append(out, Finding{Key: "fork-bomb", Description: "fork bomb", Hardline: true})
	}
	if hardlineDD.MatchString(normalized) {
		out = append(out, Finding{Key: "dd-blockdev", Description: "dd to a raw block device", Hardline: true})
	}
	if hardlineBlockDev.MatchString(normalized) {
		out = append(out, Finding{Key: "blockdev-write", Description: "redirect to a raw block device", Hardline: true})
	}
	// Command-position hardlines, one per segment.
	for _, segment := range strings.Split(marked, "\n") {
		s := stripWrappers(segment)
		if s == "" {
			continue
		}
		if hardlineRM.MatchString(s) {
			out = append(out, Finding{Key: "rm-absolute", Description: "recursive delete of the filesystem root or a system directory", Hardline: true})
			continue
		}
		for _, p := range hardlineSegmentPatterns {
			if p.re.MatchString(s) {
				out = append(out, Finding{Key: p.key, Description: p.desc, Hardline: true})
				break
			}
		}
	}
	return out
}

// --- dangerous (soft) patterns -------------------------------------------

type dangerPattern struct {
	key, description string
	re               *regexp.Regexp
	custom           func(string) bool
}

// Sensitive path fragments (from the Python definitions; the son-of-anton
// config is config.toml here, not config.yaml).
const (
	fragSystemConfig    = `(?:/etc/)`
	fragSSH             = `(?:~|\$home|\$\{home\})/\.ssh(?:/|$)`
	fragSOAEnv          = `(?:~/\.son-of-anton/|(?:\$home|\$\{home\})/\.son-of-anton/|(?:\$son_of_anton_home|\$\{son_of_anton_home\})/)\.env\b`
	fragSOAConfig       = `(?:~/\.son-of-anton/|(?:\$home|\$\{home\})/\.son-of-anton/|(?:\$son_of_anton_home|\$\{son_of_anton_home\})/)config\.toml\b`
	fragProjectEnv      = `(?:(?:/|\.{1,2}/)?(?:[^\s/"'` + "`" + `]+/)*\.env(?:\.[^\s/"'` + "`" + `]+)*)`
	fragProjectConfig   = `(?:(?:/|\.{1,2}/)?(?:[^\s/"'` + "`" + `]+/)*config\.toml)`
	fragShellRC         = `(?:~|\$home|\$\{home\})/\.(?:bashrc|zshrc|profile|bash_profile|zprofile)\b`
	fragCredentials     = `(?:~|\$home|\$\{home\})/\.(?:netrc|pgpass|npmrc|pypirc)\b`
	fragSensitiveTarget = `(?:/etc/|/dev/sd|` + fragSSH + `|` + fragSOAEnv + `|` + fragSOAConfig + `|` + fragShellRC + `|` + fragCredentials + `)`
	fragUserSensitive   = fragSSH + `|` + fragShellRC + `|` + fragCredentials
	fragProjectTarget   = fragProjectEnv + `|` + fragProjectConfig
	fragCommandTail     = `(?:\s*(?:&&|\|\||;).*)?$`
	// The Python boundary is a lookahead; this is the RE2 equivalent,
	// grouped so its alternation cannot leak out of the surrounding pattern.
	fragBoundary = `(?:[\s;&|<>"'` + "`" + `]|$)`
)

func soft(key, desc, pattern string) dangerPattern {
	return dangerPattern{key: key, description: desc, re: regexp.MustCompile(`(?is)` + pattern)}
}

func dangerPatterns() []dangerPattern {
	return []dangerPattern{
		soft("rm-root", "delete in root path", `\brm\s+(-[\s]*\s+)*/`),
		soft("rm-recursive", "recursive delete", `\brm\s+-[\s]*r`),
		soft("rm-recursive-long", "recursive delete (long flag)", `\brm\s+--recursive\b`),
		{key: "rm-flag-after-arg", description: "recursive delete (flag after argument)", custom: rmFlagAfterArg},
		soft("chmod-writable", "world/other-writable permissions", `\bchmod\s+(-[\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b`),
		soft("chmod-recursive-writable", "recursive world/other-writable (long flag)", `\bchmod\s+--recursive\b.*(777|666|o\+[rwx]*w|a\+[rwx]*w)`),
		soft("chown-root-recursive", "recursive chown to root", `\bchown\s+(-[\s]*)?R\s+root`),
		soft("chown-root-recursive-long", "recursive chown to root (long flag)", `\bchown\s+--recur[a-z]*\b.*root`),
		soft("mkfs", "file system format", `\bmkfs\b`),
		soft("dd", "disk copy", `\bdd\s+.*if=`),
		soft("blockdev-write", "write to a block device", `>\s*/dev/sd`),
		soft("sql-drop", "SQL DROP", `\bDROP\s+(TABLE|DATABASE)\b`),
		{key: "sql-delete-no-where", description: "SQL DELETE without WHERE", custom: deleteFromNoWhere},
		soft("sql-truncate", "SQL TRUNCATE", `\bTRUNCATE\s+(TABLE)?\s*\w`),
		soft("system-config-overwrite", "overwrite system config", `>\s*/etc/`),
		soft("systemctl-stop", "stop/restart a system service", `\bsystemctl\s+(-[\s]+)*\s*(stop|restart|disable|mask)\b`),
		soft("kill-all", "kill all processes", `\bkill\s+-9\s+-1\b`),
		soft("pkill-9", "force kill a process", `\bpkill\s+-9\b`),
		soft("killall-9", "force kill a process (killall -KILL)", `\bkillall\s+(-[\s]*\s+)*-(9|KILL|SIGKILL)\b`),
		soft("killall-s-kill", "force kill a process (killall -s KILL)", `\bkillall\s+(-[\s]*\s+)*-s\s+(KILL|SIGKILL|9)\b`),
		soft("killall-regex", "kill processes by regex (killall -r)", `\bkillall\s+(-[\s]*\s+)*-r\b`),
		soft("fork-bomb", "fork bomb", `:\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:`),
		soft("pipe-to-shell", "pipe remote content to shell", `\b(curl|wget)\b.*\|\s*(?:[/\w]*/)?(?:ba)?sh(?:\s|$|-c)`),
		soft("process-substitution", "execute a remote script via process substitution", `\b(bash|sh|zsh|ksh)\s+<\s*<?\s*\(\s*(curl|wget)\b`),
		soft("cmdsub-remote", "execute remote content via command substitution", `\s*\$\s*\(\s*(curl|wget)\b`),
		soft("decode-to-shell", "pipe decoded content to a shell (possible obfuscated command)", `\b(?:base64|base32|base16)\s+(?:-[dD]|--decode)\b.*\|\s*\b(?:bash|sh|zsh|ksh|dash)\b`),
		soft("xxd-to-shell", "pipe xxd-decoded content to a shell (possible obfuscated command)", `\bxxd\s+-r\b.*\|\s*\b(?:bash|sh|zsh|ksh|dash)\b`),
		soft("tr-to-shell", "pipe tr-transformed output to a shell (possible obfuscated command)", `\becho\b[^|]*\|\s*\btr\b[^|]*\|\s*\b(?:bash|sh|zsh|ksh|dash)\b`),
		soft("openssl-to-shell", "pipe openssl-decoded content to a shell (possible obfuscated command)", `\bopenssl\b.*\b(?:base64|enc)\b[^|]*\s+-[dD]\b[^|]*\|\s*\b(?:bash|sh|zsh|ksh|dash)\b`),
		soft("tee-system", "overwrite a system file via tee", `\btee\b.*["\']?`+fragSensitiveTarget),
		soft("redirect-system", "overwrite a system file via redirection", `>>?\s*["\']?`+fragSensitiveTarget),
		soft("tee-project", "overwrite a project env/config via tee", `\btee\b.*["\']?`+fragProjectTarget+`["\']?`+fragBoundary),
		soft("redirect-project", "overwrite a project env/config via redirection", `>>?\s*["\']?`+fragProjectTarget+`["\']?`+fragBoundary),
		soft("xargs-rm", "xargs with rm", `\bxargs\s+.*\brm\b`),
		soft("find-exec-rm", "find -exec/-execdir rm", `\bfind\b.*-exec(?:dir)?\s+(/\S*/)?rm\b`),
		soft("find-delete", "find -delete", `\bfind\b.*-delete\b`),
		soft("soa-gateway-stop", "stop/restart the son-of-anton gateway (kills running agents)", `\bson-of-anton\s+(?:-{1,2}\S+(?:\s+\S+)?\s+)*gateway\s+(stop|restart)\b`),
		soft("docker-remote-host", "docker with a remote daemon redirect (-H/--host)", `\bdocker\s+(?:-{1,2}\S+(?:[=\s]\S+)?\s+)*(?:-h|--host)[=\s]+\S+`),
		soft("docker-remote-context", "docker with a daemon redirect (--context: another daemon)", `\bdocker\s+(?:-{1,2}\S+(?:[=\s]\S+)?\s+)*(?:-c|--context)[=\s]+\S+`),
		soft("docker-context-use", "docker context use (switches the default daemon for later commands)", `\bdocker\s+context\s+use\b`),
		soft("podman-remote", "podman with a remote daemon redirect (--url/--connection/--identity)", `\bpodman\s+(?:-{1,2}\S+(?:[=\s]\S+)?\s+)*(?:--url|--connection|--identity)[=\s]+\S+`),
		soft("podman-remote-flag", "podman remote mode (-r/--remote: remote daemon)", `\bpodman\s+(?:-{1,2}\S+(?:[=\s]\S+)?\s+)*(?:-r\b|--remote\b)`),
		soft("daemon-env-redirect", "docker/podman daemon redirect via env var (DOCKER_HOST/CONTAINER_HOST)", `\b(?:docker_host|docker_context|container_host|container_connection)=\S+`),
		soft("docker-compose-lifecycle", "docker compose restart/stop/kill/down (container lifecycle)", `\bdocker(?:-compose|\s+compose)\s+(?:-{1,2}\S+(?:[=\s]\S+)?\s+)*(restart|stop|kill|down)\b`),
		soft("docker-lifecycle", "docker restart/stop/kill (container lifecycle)", `\bdocker\s+(?:-{1,2}\S+(?:[=\s]\S+)?\s+)*(restart|stop|kill)\b`),
		soft("gateway-bg-run", "start the gateway outside systemd (use 'systemctl --user restart son-of-anton-gateway')", `gateway\s+run\b.*(&\s*$|&\s*;|\bdisown\b|\bsetsid\b)`),
		soft("nohup-gateway", "start the gateway outside systemd (use 'systemctl --user restart son-of-anton-gateway')", `\bnohup\b.*gateway\s+run\b`),
		soft("kill-soa-process", "kill son-of-anton/gateway processes (self-termination)", `\b(?:pkill|killall)\b.*\b(?:son-of-anton|gateway|cli\.py)\b`),
		soft("kill-pgrep", "kill processes via pgrep/pidof expansion (self-termination)", `\bkill\b.*\$\s*\(\s*(?:pgrep|pidof)\b`),
		{key: "kill-pgrep-backtick", description: "kill processes via backtick pgrep/pidof expansion (self-termination)", re: regexp.MustCompile(`(?is)\bkill\b.*` + "`" + `\s*(?:pgrep|pidof)\b`)},
		soft("cp-system", "copy/move a file into a system config path", `\b(?:cp|mv|install)\b.*\s`+fragSystemConfig),
		soft("cp-project", "overwrite a project env/config file", `\b(?:cp|mv|install)\b.*\s["\']?`+fragProjectTarget+`["\']?`+fragCommandTail),
		soft("cp-sensitive", "copy/move a file into a sensitive credentials/SSH/shell rc path", `\b(?:cp|mv|install)\b.*\s["\']?`+fragSensitiveTarget+`[^"\s']*["\']?`+fragCommandTail),
		soft("sed-inplace-sensitive", "in-place edit of a sensitive credentials/SSH/shell rc path", `\bsed\s+-[\s]*i.*(?:`+fragUserSensitive+`)[^"\s']*`),
		soft("sed-inplace-sensitive-long", "in-place edit of a sensitive credentials/SSH/shell rc path (long flag)", `\bsed\s+--in-place\b.*(?:`+fragUserSensitive+`)[^"\s']*`),
		soft("perl-ruby-inplace-sensitive", "in-place edit of a sensitive credentials/SSH/shell rc path (perl/ruby)", `\b(?:perl|ruby)\b.*(?:^|\s)-[\s]*i\b.*(?:`+fragUserSensitive+`)[^"\s']*`),
		soft("sed-inplace-system", "in-place edit of a system config", `\bsed\s+-[\s]*i.*\s`+fragSystemConfig),
		soft("sed-inplace-system-long", "in-place edit of a system config (long flag)", `\bsed\s+--in-place\b.*\s`+fragSystemConfig),
		soft("sed-inplace-soa", "in-place edit of son-of-anton config/env", `\bsed\s+-[\s]*i.*(?:`+fragSOAConfig+`|`+fragSOAEnv+`)`),
		soft("sed-inplace-soa-long", "in-place edit of son-of-anton config/env (long flag)", `\bsed\s+--in-place\b.*(?:`+fragSOAConfig+`|`+fragSOAEnv+`)`),
		soft("perl-ruby-inplace-soa", "in-place edit of son-of-anton config/env (perl/ruby)", `\b(?:perl|ruby)\b.*(?:^|\s)-[\s]*i\b.*(?:`+fragSOAConfig+`|`+fragSOAEnv+`)`),
		soft("shell-heredoc", "execute shell via heredoc", `\b(?:bash|sh|zsh|ksh)\s+<<`),
		soft("git-reset-hard", "git reset --hard (destroys uncommitted changes)", `\bgit\s+reset\s+--h(?:a(?:r(?:d)?)?)?\b`),
		soft("git-push-force", "git force push (rewrites remote history)", `\bgit\s+push\b.*--forc[a-z]*\b`),
		soft("git-push-f", "git force push short flag (rewrites remote history)", `\bgit\s+push\b.*-f\b`),
		soft("git-clean-force", "git clean with force (deletes untracked files)", `\bgit\s+clean\s+-[\s]*f`),
		soft("git-branch-d", "git branch force delete", `\bgit\s+branch\s+-D\b`),
		soft("git-branch-df", "git branch force delete (long flag)", `\bgit\s+branch\b[^;|\&\n]*?(?:-d\b|--delete\b)[^;|\&\n]*?(?:-f\b|--force\b)`),
		soft("git-branch-fd", "git branch force delete (long flag, force first)", `\bgit\s+branch\b[^;|\&\n]*?(?:-f\b|--force\b)[^;|\&\n]*?(?:-d\b|--delete\b)`),
		soft("chmod-x-run", "chmod +x followed by immediate execution", `\bchmod\s+\+x\b.*[;&|]+\s*\./`),
		soft("sudo-priv-flag", "sudo with privilege flag (stdin/askpass/shell/list)", `\bsudo\b[^;|\&\n]*?\s+(?:-s\b|--st[a-z]*\b|-a\b|--a[a-z]*\b)`),
		soft("sudo-combined-flag", "sudo with a combined-flag privilege escalation", `\bsudo\b[^;|\&\n]*?\s+-[a-z]*[sa][a-z]*\b`),
		// The Python list leaves plain flagged sudo to the TTY-bound comment;
		// this daemon has no TTY, so any flagged sudo is a privilege
		// operation.
		soft("sudo-flag", "sudo with a flag (privilege operation)", `\bsudo\s+-[a-zA-Z]`),
	}
}

var compiledDangerPatterns = dangerPatterns()

// rmFlagAfterArg: `rm <args> -r` where the args do not contain a `--`
// terminator (the Python pattern's two lookaheads, as code).
var rmFlagAfterArgRe = regexp.MustCompile(`(?is)\brm\s+([^\n"';|&]*)\s(?:-[a-z]*r[a-z]*\b|--recursive\b)`)

func rmFlagAfterArg(s string) bool {
	for _, m := range rmFlagAfterArgRe.FindAllStringSubmatch(s, -1) {
		args := m[1]
		if args == "" {
			continue
		}
		if strings.HasPrefix(args, "--") && (len(args) == 2 || args[2] == ' ' || args[2] == '\t') {
			continue
		}
		hasTerminator := false
		for _, tok := range strings.Fields(args) {
			if tok == "--" {
				hasTerminator = true
				break
			}
		}
		if !hasTerminator {
			return true
		}
	}
	return false
}

// deleteFromNoWhere: `DELETE FROM` with no WHERE on the same line (the
// Python pattern's negative lookahead, as code).
var (
	deleteFromRe = regexp.MustCompile(`(?i)\bDELETE\s+FROM\b`)
	whereRe      = regexp.MustCompile(`(?i)\bWHERE\b`)
)

func deleteFromNoWhere(s string) bool {
	for _, loc := range deleteFromRe.FindAllStringIndex(s, -1) {
		rest := s[loc[1]:]
		if i := strings.IndexByte(rest, '\n'); i >= 0 {
			rest = rest[:i]
		}
		if !whereRe.MatchString(rest) {
			return true
		}
	}
	return false
}
