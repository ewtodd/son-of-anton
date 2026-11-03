package approval

import (
	"strings"
)

// The detector reads a command in three steps: deobfuscate (text in, text
// out), lex (text in, a flat token stream out) and the rules (rules.go),
// which are written as questions about that stream. Nothing here knows what
// is dangerous; it only knows how a shell splits a line.

type tokKind uint8

const (
	kWord tokKind = iota
	kSep
	kRedir
)

type sepKind uint8

const (
	sepSemi sepKind = iota
	sepNL
	sepAnd
	sepOr
	sepBg
	sepPipe
	sepOpen
	sepClose
	sepSubst // $( or a backtick
	sepProc  // <( or >(
)

// tok is one piece of the command. A quoted string is cut at its blanks, so
// every word of "rm -rf /" is a token of its own with q set: the rules look
// inside quotes on purpose, because a model can wrap a command in `echo`,
// `eval` or `bash -c` and the text is still what ends up running.
type tok struct {
	t, l string // as written, and lower-cased
	kind tokKind
	sep  sepKind
	q    bool // some of the token came from inside quotes
	data bool // an operand of a search command: inert text
	seg  int  // counts every separator
	blk  int  // counts every separator except a newline
	line int  // counts real newlines
	wid  int  // index into scan.words
}

type scan struct {
	toks      []tok
	words     []string // whole shell words, quotes removed
	balanced  bool     // every quote was closed
	cost      int
	over      bool
	shellPipe int // last pipe that feeds a shell, -2 until computed
}

func isBlank(c byte) bool { return c == ' ' || c == '\t' || c == '\r' || c == '\f' }

func isWordChar(c byte) bool {
	return c == '_' || c >= '0' && c <= '9' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z'
}

func allDigits(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}

func lex(s string) *scan {
	sc := &scan{balanced: true, shellPipe: -2}
	var cur, word strings.Builder
	curQ := false
	seg, blk, line := 0, 0, 0
	var quote byte

	endTok := func() {
		if cur.Len() > 0 {
			t := cur.String()
			sc.toks = append(sc.toks, tok{t: t, l: strings.ToLower(t), kind: kWord, q: curQ,
				seg: seg, blk: blk, line: line, wid: len(sc.words)})
			cur.Reset()
		}
		curQ = false
	}
	endWord := func() {
		endTok()
		if word.Len() > 0 {
			sc.words = append(sc.words, word.String())
			word.Reset()
		}
	}
	sep := func(k sepKind, text string, q bool) {
		sc.toks = append(sc.toks, tok{t: text, l: text, kind: kSep, sep: k, q: q,
			seg: seg, blk: blk, line: line, wid: len(sc.words)})
		seg++
		if k == sepNL {
			line++
		} else {
			blk++
		}
	}
	redir := func(op string, q bool) {
		if !curQ && allDigits(cur.String()) && word.String() == cur.String() {
			op = cur.String() + op
			cur.Reset()
			word.Reset()
		} else {
			endWord()
		}
		sc.toks = append(sc.toks, tok{t: op, l: op, kind: kRedir, q: q,
			seg: seg, blk: blk, line: line, wid: len(sc.words)})
	}
	add := func(c byte) {
		cur.WriteByte(c)
		word.WriteByte(c)
		if quote != 0 {
			curQ = true
		}
	}
	// redirOp reads the operator that starts at s[i] ('<' or '>') and
	// returns its text.
	redirOp := func(i int) string {
		c := s[i]
		rest := s[i:]
		if c == '>' {
			for _, op := range []string{">>", ">&", ">|"} {
				if strings.HasPrefix(rest, op) {
					return op
				}
			}
			return ">"
		}
		for _, op := range []string{"<<<", "<<-", "<<", "<&"} {
			if strings.HasPrefix(rest, op) {
				return op
			}
		}
		return "<"
	}

	for i := 0; i < len(s); i++ {
		c := s[i]
		if quote != 0 {
			if c == quote {
				quote = 0
				continue
			}
			// Inside quotes a separator does not end the command, but it is
			// still recorded (q set, counters untouched) so that a pipe or a
			// substitution written inside quotes is seen by the rules that
			// look for one.
			qsep := func(k sepKind, text string) {
				endTok()
				word.WriteString(text)
				sc.toks = append(sc.toks, tok{t: text, l: text, kind: kSep, sep: k, q: true,
					seg: seg, blk: blk, line: line, wid: len(sc.words)})
			}
			next := byte(0)
			if i+1 < len(s) {
				next = s[i+1]
			}
			switch {
			case isBlank(c):
				endTok()
				word.WriteByte(c)
			case c == ';':
				qsep(sepSemi, ";")
			case c == '&' && next == '&':
				qsep(sepAnd, "&&")
				i++
			case c == '&':
				qsep(sepBg, "&")
			case c == '|' && next == '|':
				qsep(sepOr, "||")
				i++
			case c == '|':
				qsep(sepPipe, "|")
			case c == '(' || c == '{':
				qsep(sepOpen, string(c))
			case c == ')' || c == '}':
				qsep(sepClose, string(c))
			case c == '<' || c == '>':
				op := redirOp(i)
				endTok()
				word.WriteString(op)
				sc.toks = append(sc.toks, tok{t: op, l: op, kind: kRedir, q: true,
					seg: seg, blk: blk, line: line, wid: len(sc.words)})
				i += len(op) - 1
			case quote == '"' && c == '$' && next == '(':
				qsep(sepSubst, "$(")
				i++
			case quote == '"' && c == '`':
				qsep(sepSubst, "`")
			case c == '\'' || c == '"':
				endTok()
				word.WriteByte(c)
			default:
				add(c)
			}
			continue
		}
		switch c {
		case ' ', '\t', '\r', '\f':
			endWord()
		case '\n':
			endWord()
			sep(sepNL, "\n", false)
		case ';':
			endWord()
			sep(sepSemi, ";", false)
		case '&':
			endWord()
			switch {
			case i+1 < len(s) && s[i+1] == '&':
				sep(sepAnd, "&&", false)
				i++
			case i+1 < len(s) && s[i+1] == '>':
				op := "&>"
				if i+2 < len(s) && s[i+2] == '>' {
					op = "&>>"
				}
				redir(op, false)
				i += len(op) - 1
			default:
				sep(sepBg, "&", false)
			}
		case '|':
			endWord()
			if i+1 < len(s) && s[i+1] == '|' {
				sep(sepOr, "||", false)
				i++
			} else {
				if i+1 < len(s) && s[i+1] == '&' {
					i++
				}
				sep(sepPipe, "|", false)
			}
		case '(':
			endWord()
			sep(sepOpen, "(", false)
		case ')':
			endWord()
			sep(sepClose, ")", false)
		case '{', '}':
			atStart := cur.Len() == 0
			next := byte(' ')
			if i+1 < len(s) {
				next = s[i+1]
			}
			if atStart && (isBlank(next) || next == '\n' || next == ';' || (c == '}' && i+1 == len(s))) {
				endWord()
				if c == '{' {
					sep(sepOpen, "{", false)
				} else {
					sep(sepClose, "}", false)
				}
			} else {
				add(c)
			}
		case '`':
			endWord()
			sep(sepSubst, "`", false)
		case '$':
			if i+1 < len(s) && s[i+1] == '(' {
				endWord()
				sep(sepSubst, "$(", false)
				i++
			} else {
				add(c)
			}
		case '<', '>':
			if i+1 < len(s) && s[i+1] == '(' {
				endWord()
				sep(sepProc, string(c)+"(", false)
				i++
				continue
			}
			op := redirOp(i)
			redir(op, false)
			i += len(op) - 1
		case '\'', '"':
			quote = c
			curQ = true
		default:
			add(c)
		}
	}
	sc.balanced = quote == 0
	endWord()
	sc.markSearchOperands()
	return sc
}

var searchTools = map[string]bool{"grep": true, "egrep": true, "fgrep": true, "rg": true, "ag": true, "ack": true}

// markSearchOperands flags the quoted words of a segment that starts with a
// search tool: the pattern given to grep is text to look for, not something
// that runs. With an unbalanced quote nothing is flagged, so a stray quote
// cannot be used to hide a command.
func (sc *scan) markSearchOperands() {
	if !sc.balanced {
		return
	}
	for i := 0; i < len(sc.toks); i++ {
		if !sc.isSegStart(i) {
			continue
		}
		first := i
		for first < len(sc.toks) && sc.toks[first].kind == kRedir {
			first++
		}
		if first >= len(sc.toks) || sc.toks[first].kind != kWord || !searchTools[base(sc.toks[first].l)] {
			continue
		}
		for j := first; j < len(sc.toks) && sc.toks[j].seg == sc.toks[i].seg; j++ {
			if sc.toks[j].q {
				sc.toks[j].data = true
			}
		}
	}
}

func (sc *scan) isSegStart(i int) bool {
	return sc.toks[i].kind != kSep && (i == 0 || sc.toks[i-1].kind == kSep && !sc.toks[i-1].q)
}

// base is the last path component of a lower-cased word.
func base(l string) string {
	if i := strings.LastIndexByte(l, '/'); i >= 0 && i+1 < len(l) {
		return l[i+1:]
	}
	return l
}

// wordArgs are the word tokens after position i up to the end of the block
// (bySeg false) or segment (true), skipping redirections with their targets
// and the search operands.
func (sc *scan) wordArgs(i int, bySeg bool) []tok {
	var out []tok
	for j := i + 1; j < len(sc.toks); j++ {
		if sc.spend(1) {
			return nil
		}
		t := sc.toks[j]
		if t.kind == kSep && !t.q && (bySeg || t.sep != sepNL) {
			break
		}
		if t.kind == kSep {
			continue
		}
		if t.seg != sc.toks[i].seg && bySeg {
			break
		}
		if t.kind == kRedir {
			j++
			continue
		}
		if t.data {
			continue
		}
		out = append(out, t)
	}
	return out
}

// chain is what a segment's first word turns out to be once the wrappers in
// front of it (sudo, env, nohup, time, timeout, shell keywords, VAR=value)
// are looked through.
type chain struct {
	cmd  int   // token index of the command word, -1 when none
	sudo []int // option tokens of every sudo seen on the way
}

var lineKeywords = map[string]bool{
	"if": true, "then": true, "else": true, "elif": true, "do": true,
	"while": true, "until": true, "!": true,
}

var plainWrappers = map[string]bool{
	"exec": true, "nohup": true, "setsid": true, "time": true, "command": true,
	"builtin": true, "nice": true, "ionice": true, "stdbuf": true,
}

func isAssignment(w string) bool {
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

// sudo options that take a value as the next word.
const sudoValueFlags = "CDgprRtTuU"

var sudoValueLong = map[string]bool{
	"--close-from": true, "--chdir": true, "--group": true, "--prompt": true,
	"--role": true, "--chroot": true, "--type": true, "--command-timeout": true,
	"--user": true, "--other-user": true, "--host": true,
}

func (sc *scan) chainAt(start int) chain {
	ch := chain{cmd: -1}
	seg := sc.toks[start].seg
	i := start
	for i < len(sc.toks) && sc.toks[i].seg == seg {
		t := sc.toks[i]
		switch {
		case t.kind == kRedir:
			i += 2
		case t.kind == kSep && t.q:
			i++
		case t.kind != kWord:
			return ch
		case isAssignment(t.t) || lineKeywords[t.l]:
			i++
		case base(t.l) == "sudo":
			i++
			for i < len(sc.toks) && sc.toks[i].seg == seg && sc.toks[i].kind == kWord && strings.HasPrefix(sc.toks[i].t, "-") && len(sc.toks[i].t) > 1 {
				o := sc.toks[i].t
				ch.sudo = append(ch.sudo, i)
				i++
				if o == "--" {
					break
				}
				takesValue := false
				if strings.HasPrefix(o, "--") {
					takesValue = !strings.Contains(o, "=") && sudoValueLong[o]
				} else {
					takesValue = strings.IndexByte(sudoValueFlags, o[len(o)-1]) >= 0
				}
				if takesValue && i < len(sc.toks) && sc.toks[i].seg == seg {
					i++
				}
			}
		case base(t.l) == "env":
			i++
			for i < len(sc.toks) && sc.toks[i].seg == seg && sc.toks[i].kind == kWord && strings.HasPrefix(sc.toks[i].t, "-") && len(sc.toks[i].t) > 1 {
				o := sc.toks[i].t
				i++
				if (o == "-u" || o == "-C" || o == "-S") && i < len(sc.toks) && sc.toks[i].seg == seg {
					i++
				}
			}
		case base(t.l) == "timeout":
			i++
			for i < len(sc.toks) && sc.toks[i].seg == seg && sc.toks[i].kind == kWord && strings.HasPrefix(sc.toks[i].t, "-") && len(sc.toks[i].t) > 1 {
				o := sc.toks[i].t
				i++
				if (o == "-k" || o == "-s") && i < len(sc.toks) && sc.toks[i].seg == seg {
					i++
				}
			}
			if i < len(sc.toks) && sc.toks[i].seg == seg {
				i++ // the duration
			}
		case plainWrappers[base(t.l)]:
			i++
			if base(t.l) == "nice" && i+1 < len(sc.toks) && sc.toks[i].t == "-n" {
				i += 2
			}
		default:
			ch.cmd = i
			return ch
		}
	}
	return ch
}

// commands lists the effective command of every segment.
func (sc *scan) commands() []chain {
	var out []chain
	for i := range sc.toks {
		if sc.isSegStart(i) {
			if ch := sc.chainAt(i); ch.cmd >= 0 || len(ch.sudo) > 0 {
				out = append(out, ch)
			}
		}
	}
	return out
}

// stageCommand is the effective command word of the pipeline stage that
// starts at token i, or "".
func (sc *scan) stageCommand(i int) string {
	if i >= len(sc.toks) || sc.toks[i].kind == kSep {
		return ""
	}
	if ch := sc.chainAt(i); ch.cmd >= 0 {
		return base(sc.toks[ch.cmd].l)
	}
	return ""
}

var shells = map[string]bool{"sh": true, "bash": true, "zsh": true, "ksh": true, "dash": true, "ash": true}

// pipedIntoShell reports whether any pipeline stage after token i is a shell.
func (sc *scan) pipedIntoShell(i int) bool {
	if sc.shellPipe == -2 {
		sc.shellPipe = -1
		for j := 0; j+1 < len(sc.toks); j++ {
			if sc.toks[j].kind == kSep && sc.toks[j].sep == sepPipe && shells[sc.stageCommand(j+1)] {
				sc.shellPipe = j
			}
		}
	}
	return sc.shellPipe > i
}

// verbs lists the token indexes of every non-inert word whose last path
// component is name.
func (sc *scan) verbs(name string) []int {
	var out []int
	for i, t := range sc.toks {
		if t.kind == kWord && !t.data && base(t.l) == name {
			out = append(out, i)
		}
	}
	return out
}

// payloads returns the strings that a shell is told to run with -c.
func (sc *scan) payloads() []string {
	var out []string
	for _, ch := range sc.commands() {
		if ch.cmd < 0 || !shells[base(sc.toks[ch.cmd].l)] {
			continue
		}
		var args []string // whole words after the shell
		last := -1
		for j := ch.cmd + 1; j < len(sc.toks) && sc.toks[j].seg == sc.toks[ch.cmd].seg; j++ {
			if sc.toks[j].kind == kWord && sc.toks[j].wid != last {
				last = sc.toks[j].wid
				args = append(args, sc.words[last])
			}
		}
		if p, ok := shellCommandString(args); ok && p != "" {
			out = append(out, p)
		}
	}
	return out
}

// shellCommandString finds the operand of -c among a shell's arguments. An
// option bundle such as -lc may carry the c; every o or O in a bundle takes
// the next word as its value, and so do --init-file and --rcfile.
func shellCommandString(args []string) (string, bool) {
	for k := 0; k < len(args); k++ {
		a := args[k]
		if len(a) < 2 || a == "--" || (a[0] != '-' && a[0] != '+') {
			return "", false
		}
		if strings.HasPrefix(a, "--") {
			if a == "--init-file" || a == "--rcfile" {
				k++
			}
			continue
		}
		bundle := a[1:]
		values := strings.Count(bundle, "o") + strings.Count(bundle, "O")
		if a[0] == '-' && strings.Contains(bundle, "c") {
			if k+1+values < len(args) {
				return args[k+1+values], true
			}
			return "", true
		}
		k += values
	}
	return "", false
}

// A command with thousands of verbs and thousands of tokens would make the
// rules quadratic. Each scan spends from a budget; running out is reported
// like the parser limit (hardline) instead of taking unbounded time.
const scanBudget = 4_000_000

// spend charges n steps and reports whether the budget is gone.
func (sc *scan) spend(n int) bool {
	sc.cost += n
	if sc.cost > scanBudget {
		sc.over = true
	}
	return sc.over
}

// rest is the token stream from position i on, charged to the budget.
func (sc *scan) rest(i int) []tok {
	if i > len(sc.toks) {
		i = len(sc.toks)
	}
	sc.spend(len(sc.toks) - i)
	if sc.over {
		return nil
	}
	return sc.toks[i:]
}
