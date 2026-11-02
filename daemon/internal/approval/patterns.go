package approval

import (
	"os"
	"regexp"
	"strings"
	"sync"

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
	ifsRe          = regexp.MustCompile(`\$\{IFS\b[^}]*\}|\$IFS\b`)
)

// normalize de-obfuscates a command the way the Python detection layer does:
// ANSI codes and NULs out, NFKC, line continuations folded, the instance and
// user home prefixes written as ~/.son-of-anton and ~, backslash escapes
// dropped, empty quotes dropped, $IFS collapsed to a space. A bare home with
// no path under it is not folded. ($HOME and ${HOME} are also written as ~,
// which the reference leaves to its patterns; both spellings classify alike.)
func normalize(command string) string {
	s := ansiRe.ReplaceAllString(command, "")
	s = strings.ReplaceAll(s, "\x00", "")
	s = norm.NFKC.String(s)
	s = continuationRe.ReplaceAllString(s, "")
	s = foldHome(s, soaHome(), "~/.son-of-anton")
	s = foldHome(s, userHome(), "~")
	s = homeVarRe.ReplaceAllString(s, "~")
	s = escapeRe.ReplaceAllString(s, "$1")
	s = emptyQuoteRe.ReplaceAllString(s, "")
	s = ifsRe.ReplaceAllString(s, " ")
	return s
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

// soaHome is the instance home: SON_OF_ANTON_HOME, else ~/.son-of-anton.
func soaHome() string {
	if h := os.Getenv("SON_OF_ANTON_HOME"); h != "" {
		return h
	}
	if h := userHome(); h != "" {
		return h + "/.son-of-anton"
	}
	return ""
}

// foldHome rewrites an absolute home path used as a directory prefix (repeated
// slashes allowed between its components) to replacement, like the
// reference's _fold_home_prefixes. A home with fewer than two components
// (unset, "/", "/x") is never folded, so a stray value cannot rewrite
// unrelated paths.
func foldHome(s, home, replacement string) string {
	var parts []string
	for _, c := range strings.Split(home, "/") {
		if c != "" {
			parts = append(parts, regexp.QuoteMeta(c))
		}
	}
	if len(parts) < 2 {
		return s
	}
	re, ok := homeFoldCache.Load(home)
	if !ok {
		re = regexp.MustCompile("/*" + strings.Join(parts, "/+") + `(?P<tail>(?:/[^/\s'"` + "`" + `;|&<>()]*)+)`)
		homeFoldCache.Store(home, re)
	}
	return re.(*regexp.Regexp).ReplaceAllString(s, replacement+"${tail}")
}

var homeFoldCache sync.Map

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

// interpreterPayloads extracts the payloads of `bash|sh|zsh|ksh -c <payload>`
// (also -lc and the other option bundles bash accepts) so they are classified
// as commands in their own right. The command word may follow the
// sudo/env/exec/... wrappers. marked has one command per line.
var interpreters = map[string]bool{"bash": true, "sh": true, "zsh": true, "ksh": true}

var wrapperWords = map[string]bool{
	"sudo": true, "env": true, "exec": true, "nohup": true, "setsid": true,
	"time": true, "command": true, "builtin": true,
}

var sudoOptionsWithArg = map[string]bool{
	"-c": true, "--close-from": true, "-g": true, "--group": true, "-h": true,
	"--host": true, "-p": true, "--prompt": true, "-u": true, "--user": true,
}

func interpreterPayloads(marked string) []string {
	var payloads []string
	for _, line := range strings.Split(marked, "\n") {
		words, ok := shellWords(line)
		if !ok {
			continue
		}
		i, skipOpts, skipArg := 0, false, false
		for ; i < len(words); i++ {
			w := strings.ToLower(words[i])
			switch {
			case skipArg:
				skipArg = false
				continue
			case skipOpts && strings.HasPrefix(w, "-"):
				name, _, hasEq := strings.Cut(w, "=")
				skipArg = !hasEq && sudoOptionsWithArg[name]
				continue
			case wrapperWords[w]:
				skipOpts = w == "sudo" || w == "env"
				continue
			case isEnvAssignment(words[i]):
				skipOpts = false
				continue
			}
			break
		}
		if i >= len(words) || !interpreters[lowerBasename(words[i])] {
			continue
		}
		if payload, found := bashExecPayload(words[i+1:]); found && payload != "" {
			payloads = append(payloads, payload)
		}
	}
	return payloads
}

func isEnvAssignment(w string) bool {
	eq := strings.IndexByte(w, '=')
	if eq <= 0 {
		return false
	}
	for i := 0; i < eq; i++ {
		c := w[i]
		if !(c == '_' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || i > 0 && c >= '0' && c <= '9') {
			return false
		}
	}
	return true
}

const bashShortOptionLetters = "ilrsDcabefhkmnptuvxBCEHPTOo"

// bashExecPayload finds bash's -c (alone or in an option bundle) and the
// command string it owns, honouring -O/-o and the startup-file options that
// consume the next word (the reference's _bash_exec_payload).
func bashExecPayload(args []string) (string, bool) {
	for index := 0; index < len(args); {
		token := args[index]
		if token == "--" || !(strings.HasPrefix(token, "-") || strings.HasPrefix(token, "+")) {
			break
		}
		switch token {
		case "-O", "+O", "-o", "+o", "--init-file", "--rcfile":
			index += 2
			continue
		}
		if strings.HasPrefix(token, "--") {
			index++
			continue
		}
		chars := token[1:]
		if strings.Trim(chars, bashShortOptionLetters) != "" {
			index++
			continue
		}
		consumed := 0
		if strings.ContainsAny(chars, "Oo") {
			consumed = 1
		}
		if !strings.Contains(chars, "c") {
			index += 1 + consumed
			continue
		}
		if p := index + 1 + consumed; p < len(args) {
			return args[p], true
		}
		return "", true
	}
	return "", false
}

// shellWords splits one command line into words the way POSIX shlex does for
// quoting: single quotes literal, double quotes with backslash escaping only
// " and \, a bare backslash escaping the next character. An unterminated
// quote reports false.
func shellWords(line string) ([]string, bool) {
	var words []string
	var cur strings.Builder
	inWord := false
	for i := 0; i < len(line); i++ {
		c := line[i]
		switch {
		case c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f':
			if inWord {
				words = append(words, cur.String())
				cur.Reset()
				inWord = false
			}
		case c == '\'':
			inWord = true
			j := strings.IndexByte(line[i+1:], '\'')
			if j < 0 {
				return nil, false
			}
			cur.WriteString(line[i+1 : i+1+j])
			i += j + 1
		case c == '"':
			inWord = true
			closed := false
			for i++; i < len(line); i++ {
				d := line[i]
				if d == '\\' && i+1 < len(line) && (line[i+1] == '"' || line[i+1] == '\\') {
					i++
					cur.WriteByte(line[i])
					continue
				}
				if d == '"' {
					closed = true
					break
				}
				cur.WriteByte(d)
			}
			if !closed {
				return nil, false
			}
		case c == '\\':
			inWord = true
			if i+1 >= len(line) {
				return nil, false
			}
			i++
			cur.WriteByte(line[i])
		default:
			inWord = true
			cur.WriteByte(c)
		}
	}
	if inWord {
		words = append(words, cur.String())
	}
	return words, true
}

// maskQuotedNewlines writes a space for every raw newline inside quotes: it
// is data to the shell, not a command separator (the reference's
// _mask_quoted_newlines). Run on the raw command, before normalization.
func maskQuotedNewlines(command string) string {
	if !strings.Contains(command, "\n") {
		return command
	}
	out := make([]byte, 0, len(command))
	var quote byte
	for i := 0; i < len(command); i++ {
		c := command[i]
		if quote != 0 {
			if c == '\\' && quote == '"' && i+1 < len(command) {
				out = append(out, c, command[i+1])
				i++
				continue
			}
			if c == quote {
				quote = 0
			}
			if c == '\n' {
				c = ' '
			}
			out = append(out, c)
			continue
		}
		if c == '\'' || c == '"' {
			quote = c
		} else if c == '\\' && i+1 < len(command) {
			out = append(out, c, command[i+1])
			i++
			continue
		}
		out = append(out, c)
	}
	return string(out)
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
	if parserLimitExceeded(command) {
		return []Finding{{Key: "parser-limit", Description: "command parser limit exceeded", Hardline: true}}
	}
	normalized := normalize(maskQuotedNewlines(command))
	marked := markSegments(normalized)

	findings := []Finding{}
	// The reference guards the command itself (not a bash -c payload) and
	// normalizes without the newline mask.
	if depth == 0 && sudoStdinGuard.MatchString(normalize(command)) {
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
		id := f.Key
		if f.Hardline {
			id = "hardline:" + id
		}
		if seen[id] {
			continue
		}
		seen[id] = true
		out = append(out, f)
	}
	return out
}

// --- hardline patterns ---------------------------------------------------

var sudoStdinGuard = regexp.MustCompile("(?i)(?:^|[;&|`\\n]|\\$\\()\\s*sudo\\s+-S\\b")

// cmdPos is the Python _CMDPOS: the start of a command (start of text, after
// a newline, backtick or `$(`), then optional sudo with flags, env with
// VAR=VAL pairs and exec/nohup/setsid/time wrappers. The reference has no
// timeout wrapper; the phase spec adds `timeout N`.
const cmdPos = "(?:^|[\\n`]|\\$\\()" +
	`\s*` +
	`(?:sudo\s+(?:-[^\s]+\s+)*)?` +
	`(?:env\s+(?:\w+=\S*\s+)*)?` +
	`(?:(?:exec|nohup|setsid|time|timeout\s+\S+)\s+)*` +
	`\s*`

// hardlineRMPath is _hardline_rm_path: the path fully wrapped in a matching
// quote pair, or bare with a terminator.
func hardlineRMPath(alt string) string {
	return `(?:["'](?:` + alt + `)["']|(?:` + alt + ")(?:\\s|$|[)`;|&]))"
}

const hardlineSystemDirs = `/home|/home/\*|/root|/root/\*|/etc|/etc/\*|/usr|/usr/\*|` +
	`/var|/var/\*|/bin|/bin/\*|/sbin|/sbin/\*|/boot|/boot/\*|/lib|/lib/\*`

const rmFlagPrefix = cmdPos + `rm\s+(-[^\s]*\s+)*`

type hardlinePattern struct {
	key, desc string
	re        *regexp.Regexp
	// whole: matched against the unmarked command (separators intact);
	// otherwise against the command-start-marked one.
	whole bool
}

func hl(key, desc, pattern string, whole bool) hardlinePattern {
	return hardlinePattern{key: key, desc: desc, re: regexp.MustCompile(`(?is)` + pattern), whole: whole}
}

// hardlinePatterns is HARDLINE_PATTERNS from the reference, in order.
var hardlinePatterns = []hardlinePattern{
	hl("rm-fs-root", "recursive delete of root filesystem", rmFlagPrefix+hardlineRMPath(`/(?:(?:\.\.?)?/)*(?:\.\.?)?\**|/ \*`), false),
	hl("rm-system-dir", "recursive delete of system directory", rmFlagPrefix+hardlineRMPath(hardlineSystemDirs), false),
	hl("rm-home-dir", "recursive delete of home directory", rmFlagPrefix+hardlineRMPath(`(?:~|\$\{?HOME\}?)(?:/?|/\*)?`), false),
	hl("mkfs", "format filesystem (mkfs)", `\bmkfs(\.[a-z0-9]+)?\b`, true),
	hl("dd-blockdev", "dd to a raw block device", `\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*`, true),
	hl("blockdev-write", "redirect to a raw block device", `>\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b`, true),
	hl("fork-bomb", "fork bomb", `:\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:`, true),
	hl("kill-all", "kill all processes", `\bkill\s+(-[^\s]+\s+)*-1\b`, true),
	hl("system-shutdown", "system shutdown/reboot", cmdPos+`(shutdown|reboot|halt|poweroff)\b`, false),
	hl("init-shutdown", "init 0/6 (shutdown/reboot)", cmdPos+`init\s+[06]\b`, false),
	hl("systemctl-shutdown", "systemctl poweroff/reboot", cmdPos+`systemctl\s+(poweroff|reboot|halt|kexec)\b`, false),
	hl("telinit-shutdown", "telinit 0/6 (shutdown/reboot)", cmdPos+`telinit\s+[06]\b`, false),
}

// hardlineFindings returns the hardline hits. The whole-command patterns run
// on the normalized text; the command-position ones on the text with a
// newline at every command start.
func hardlineFindings(normalized, marked string) []Finding {
	var out []Finding
	for _, p := range hardlinePatterns {
		text := marked
		if p.whole {
			text = normalized
		}
		if p.re.MatchString(text) {
			out = append(out, Finding{Key: p.key, Description: p.desc, Hardline: true})
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
	fragProjectTarget   = `(?:` + fragProjectEnv + `|` + fragProjectConfig + `)`
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
		soft("rm-root", "delete in root path", `\brm\s+(-[^\s]*\s+)*/`),
		soft("rm-recursive", "recursive delete", `\brm\s+-[^\s]*r`),
		soft("rm-recursive-long", "recursive delete (long flag)", `\brm\s+--recursive\b`),
		{key: "rm-flag-after-arg", description: "recursive delete (flag after argument)", custom: rmFlagAfterArg},
		soft("chmod-writable", "world/other-writable permissions", `\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b`),
		soft("chmod-recursive-writable", "recursive world/other-writable (long flag)", `\bchmod\s+--recursive\b.*(777|666|o\+[rwx]*w|a\+[rwx]*w)`),
		soft("chown-root-recursive", "recursive chown to root", `\bchown\s+(-[^\s]*)?R\s+root`),
		soft("chown-root-recursive-long", "recursive chown to root (long flag)", `\bchown\s+--recur[a-z]*\b.*root`),
		soft("mkfs", "file system format", `\bmkfs\b`),
		soft("dd", "disk copy", `\bdd\s+.*if=`),
		soft("blockdev-write", "write to a block device", `>\s*/dev/sd`),
		soft("sql-drop", "SQL DROP", `\bDROP\s+(TABLE|DATABASE)\b`),
		{key: "sql-delete-no-where", description: "SQL DELETE without WHERE", custom: deleteFromNoWhere},
		soft("sql-truncate", "SQL TRUNCATE", `\bTRUNCATE\s+(TABLE)?\s*\w`),
		soft("system-config-overwrite", "overwrite system config", `>\s*/etc/`),
		soft("systemctl-stop", "stop/restart a system service", `\bsystemctl\s+(-[^\s]+\s+)*(stop|restart|disable|mask)\b`),
		soft("kill-all", "kill all processes", `\bkill\s+-9\s+-1\b`),
		soft("pkill-9", "force kill a process", `\bpkill\s+-9\b`),
		soft("killall-9", "force kill a process (killall -KILL)", `\bkillall\s+(-[^\s]*\s+)*-(9|KILL|SIGKILL)\b`),
		soft("killall-s-kill", "force kill a process (killall -s KILL)", `\bkillall\s+(-[^\s]*\s+)*-s\s+(KILL|SIGKILL|9)\b`),
		soft("killall-regex", "kill processes by regex (killall -r)", `\bkillall\s+(-[^\s]*\s+)*-r\b`),
		soft("fork-bomb", "fork bomb", `:\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:`),
		soft("pipe-to-shell", "pipe remote content to shell", `\b(curl|wget)\b.*\|\s*(?:[/\w]*/)?(?:ba)?sh(?:\s|$|-c)`),
		soft("process-substitution", "execute a remote script via process substitution", `\b(bash|sh|zsh|ksh)\s+<\s*<?\s*\(\s*(curl|wget)\b`),
		soft("cmdsub-remote", "execute remote content via command substitution", `(?:\beval\b|\bsource\b|\.)\s*(?:\$\(\s*|`+"`"+`\s*)(?:curl|wget)\b`),
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
		soft("sed-inplace-sensitive", "in-place edit of a sensitive credentials/SSH/shell rc path", `\bsed\s+-[^\s]*i.*(?:`+fragUserSensitive+`)[^"\s']*`),
		soft("sed-inplace-sensitive-long", "in-place edit of a sensitive credentials/SSH/shell rc path (long flag)", `\bsed\s+--in-place\b.*(?:`+fragUserSensitive+`)[^"\s']*`),
		soft("perl-ruby-inplace-sensitive", "in-place edit of a sensitive credentials/SSH/shell rc path (perl/ruby)", `\b(?:perl|ruby)\b.*(?:^|\s)-[^\s]*i\b.*(?:`+fragUserSensitive+`)[^"\s']*`),
		soft("sed-inplace-system", "in-place edit of a system config", `\bsed\s+-[^\s]*i.*\s`+fragSystemConfig),
		soft("sed-inplace-system-long", "in-place edit of a system config (long flag)", `\bsed\s+--in-place\b.*\s`+fragSystemConfig),
		soft("sed-inplace-soa", "in-place edit of son-of-anton config/env", `\bsed\s+-[^\s]*i.*(?:`+fragSOAConfig+`|`+fragSOAEnv+`)`),
		soft("sed-inplace-soa-long", "in-place edit of son-of-anton config/env (long flag)", `\bsed\s+--in-place\b.*(?:`+fragSOAConfig+`|`+fragSOAEnv+`)`),
		soft("perl-ruby-inplace-soa", "in-place edit of son-of-anton config/env (perl/ruby)", `\b(?:perl|ruby)\b.*(?:^|\s)-[^\s]*i\b.*(?:`+fragSOAConfig+`|`+fragSOAEnv+`)`),
		soft("shell-heredoc", "execute shell via heredoc", `\b(?:bash|sh|zsh|ksh)\s+<<`),
		soft("git-reset-hard", "git reset --hard (destroys uncommitted changes)", `\bgit\s+reset\s+--h(?:a(?:r(?:d)?)?)?\b`),
		soft("git-push-force", "git force push (rewrites remote history)", `\bgit\s+push\b.*--forc[a-z]*\b`),
		soft("git-push-f", "git force push short flag (rewrites remote history)", `\bgit\s+push\b.*-f\b`),
		soft("git-clean-force", "git clean with force (deletes untracked files)", `\bgit\s+clean\s+-[^\s]*f`),
		soft("git-branch-d", "git branch force delete", `\bgit\s+branch\s+-D\b`),
		soft("git-branch-df", "git branch force delete (long flag)", `\bgit\s+branch\b[^;|\&\n]*?(?:-d\b|--delete\b)[^;|\&\n]*?(?:-f\b|--force\b)`),
		soft("git-branch-fd", "git branch force delete (long flag, force first)", `\bgit\s+branch\b[^;|\&\n]*?(?:-f\b|--force\b)[^;|\&\n]*?(?:-d\b|--delete\b)`),
		soft("chmod-x-run", "chmod +x followed by immediate execution", `\bchmod\s+\+x\b.*[;&|]+\s*\./`),
		soft("sudo-priv-flag", "sudo with privilege flag (stdin/askpass/shell/list)", `\bsudo\b[^;|\&\n]*?\s+(?:-s\b|--st[a-z]*\b|-a\b|--a[a-z]*\b)`),
		soft("sudo-combined-flag", "sudo with a combined-flag privilege escalation", `\bsudo\b[^;|\&\n]*?\s+-[a-z]*[sa][a-z]*\b`),
	}
}

var compiledDangerPatterns = dangerPatterns()

// rmFlagAfterArg is the reference's "recursive delete (flags after
// operands)" pattern, whose two lookaheads RE2 lacks:
//
//	\brm\s+(?!--(?:\s|$))(?:(?!\s--(?:\s|$))[^\n"';|&])*\s(?:-[a-z]*r[a-z]*\b|--recursive\b)
//
// For every `rm` word and every way its \s+ can end, the operand run is the
// longest stretch that holds no separator or quote and does not cross a bare
// " -- "; a recursive flag may follow any whitespace inside or right at the
// end of that run.
var (
	rmWordRe     = regexp.MustCompile(`(?i)\brm(\s+)`)
	rmRecFlagRe  = regexp.MustCompile(`(?i)^(?:-[a-z]*r[a-z]*\b|--recursive\b)`)
	rmRunStopSet = "\n\"';|&"
)

func isRegexSpace(c byte) bool {
	return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f'
}

// bareDashDash reports whether s[i:] is `--` followed by whitespace or the end.
func bareDashDash(s string, i int) bool {
	return strings.HasPrefix(s[i:], "--") && (i+2 == len(s) || isRegexSpace(s[i+2]))
}

func rmFlagAfterArg(s string) bool {
	for _, m := range rmWordRe.FindAllStringSubmatchIndex(s, -1) {
		for start := m[2] + 1; start <= m[3]; start++ {
			if bareDashDash(s, start) {
				continue
			}
			end := start
			for end < len(s) {
				c := s[end]
				if strings.IndexByte(rmRunStopSet, c) >= 0 {
					break
				}
				if isRegexSpace(c) && bareDashDash(s, end+1) {
					break
				}
				end++
			}
			for k := start; k <= end && k < len(s); k++ {
				if isRegexSpace(s[k]) && rmRecFlagRe.MatchString(s[k+1:]) {
					return true
				}
			}
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

// PatternKeys lists the key of every hardline and dangerous pattern: what
// Classify can report, without the parser limit.
func PatternKeys() []string {
	keys := []string{"sudo-stdin"}
	for _, p := range hardlinePatterns {
		keys = append(keys, p.key)
	}
	for _, p := range compiledDangerPatterns {
		keys = append(keys, p.key)
	}
	return keys
}
