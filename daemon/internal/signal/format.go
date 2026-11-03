package signal

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"
)

// ws is the whitespace class the patterns below need: RE2's \s is ASCII
// only, and this adds the separators and control characters that count.
const ws = `[\s\x0b\x1c-\x1f\x85\p{Z}]`

var (
	reBlankRuns = regexp.MustCompile(`\n{3,}`)
	reFence     = regexp.MustCompile("(?s)```.*?```")
	reBullet    = regexp.MustCompile(`(?m)^([ \t]{0,3})[-*+]` + ws + `+`)
	reCodeBlock = regexp.MustCompile("(?s)```[a-zA-Z0-9_+-]*\n?(.*?)```")
	reHeading   = regexp.MustCompile(`(?m)^#{1,6}` + ws + `+`)
)

// inline is one inline pattern whose match rule RE2 can state; the two
// italic patterns need lookarounds and are matched by hand.
var inline = []struct {
	find  func(text string, runes []rune) []span
	style string
}{
	{regexFinder(`(?s)\*\*(.+?)\*\*`), "BOLD"},
	{regexFinder(`(?s)__(.+?)__`), "BOLD"},
	{regexFinder(`(?s)~~(.+?)~~`), "STRIKETHROUGH"},
	{regexFinder("`(.+?)`"), "MONOSPACE"},
	{func(_ string, r []rune) []span { return italics(r, '*') }, "ITALIC"},
	{func(_ string, r []rune) []span { return italics(r, '_') }, "ITALIC"},
}

// span is a match and its first group, in code points.
type span struct{ ms, me, g1s, g1e int }

type styled struct {
	start, length int
	style         string
}

// Format turns Markdown into plain text and Signal text styles
// ("start:length:STYLE", in UTF-16 code units). Signal does not render
// Markdown, so the text loses its markers and the styles say what they
// covered. The output is kept exactly as it has always been, quirks included,
// because it is what the owner's chats have always shown.
func Format(markdown string) (string, []string) {
	text := reBlankRuns.ReplaceAllString(markdown, "\n\n")
	text = strings.TrimFunc(text, isPySpace)
	text = normalizeBullets(text)

	var styles []styled

	for {
		m := reCodeBlock.FindStringSubmatchIndex(text)
		if m == nil {
			break
		}
		inner := strings.TrimRight(text[m[2]:m[3]], "\n")
		styles = append(styles, styled{runeLen(text[:m[0]]), runeLen(inner), "MONOSPACE"})
		text = text[:m[0]] + inner + text[m[1]:]
	}

	var out strings.Builder
	outRunes, lastEnd := 0, 0
	for _, m := range reHeading.FindAllStringIndex(text, -1) {
		before := slice(text, lastEnd, m[0])
		out.WriteString(before)
		outRunes += runeLen(before)
		eol := strings.Index(text[m[1]:], "\n")
		if eol == -1 {
			eol = len(text)
		} else {
			eol += m[1]
		}
		heading := slice(text, m[1], eol)
		styles = append(styles, styled{outRunes, runeLen(heading), "BOLD"})
		out.WriteString(heading)
		outRunes += runeLen(heading)
		lastEnd = eol
	}
	out.WriteString(slice(text, lastEnd, len(text)))
	text = out.String()

	runes := []rune(text)
	type match struct {
		span
		style string
	}
	var all []match
	var occupied []span
	for _, p := range inline {
		for _, sp := range p.find(text, runes) {
			clash := false
			for _, o := range occupied {
				if sp.ms < o.me && sp.me > o.ms {
					clash = true
					break
				}
			}
			if !clash {
				all = append(all, match{sp, p.style})
				occupied = append(occupied, sp)
			}
		}
	}
	sort.Slice(all, func(i, j int) bool {
		a, b := all[i], all[j]
		switch {
		case a.ms != b.ms:
			return a.ms < b.ms
		case a.me != b.me:
			return a.me < b.me
		case a.g1s != b.g1s:
			return a.g1s < b.g1s
		case a.g1e != b.g1e:
			return a.g1e < b.g1e
		}
		return a.style < b.style
	})

	type removal struct{ pos, length int }
	var removals []removal
	for _, m := range all {
		if m.g1s > m.ms {
			removals = append(removals, removal{m.ms, m.g1s - m.ms})
		}
		if m.me > m.g1e {
			removals = append(removals, removal{m.g1e, m.me - m.g1e})
		}
	}
	sort.Slice(removals, func(i, j int) bool {
		if removals[i].pos != removals[j].pos {
			return removals[i].pos < removals[j].pos
		}
		return removals[i].length < removals[j].length
	})
	adjust := func(pos int) int {
		shift := 0
		for _, r := range removals {
			if r.pos >= pos {
				break
			}
			shift += min(r.length, pos-r.pos)
		}
		return pos - shift
	}

	var prior []styled
	for _, s := range styles {
		start, end := adjust(s.start), adjust(s.start+s.length)
		if end > start {
			prior = append(prior, styled{start, end - start, s.style})
		}
	}

	var result []rune
	last := 0
	var inl []styled
	for _, m := range all {
		result = append(result, runes[last:m.ms]...)
		pos := len(result)
		inner := runes[m.g1s:m.g1e]
		result = append(result, inner...)
		inl = append(inl, styled{pos, len(inner), m.style})
		last = m.me
	}
	result = append(result, runes[last:]...)

	all2 := append(prior, inl...)
	sort.Slice(all2, func(i, j int) bool {
		a, b := all2[i], all2[j]
		switch {
		case a.start != b.start:
			return a.start < b.start
		case a.length != b.length:
			return a.length < b.length
		}
		return a.style < b.style
	})
	var strs []string
	for _, s := range all2 {
		if s.start < 0 || s.start+s.length > len(result) {
			continue
		}
		strs = append(strs, fmt.Sprintf("%d:%d:%s", utf16Len(result[:s.start]), utf16Len(result[s.start:s.start+s.length]), s.style))
	}
	return string(result), strs
}

// normalizeBullets turns "- item", "* item" and "+ item" into "• item"
// outside fenced code blocks, which stay byte for byte.
func normalizeBullets(source string) string {
	var b strings.Builder
	last := 0
	for _, m := range reFence.FindAllStringIndex(source, -1) {
		b.WriteString(reBullet.ReplaceAllString(source[last:m[0]], "${1}• "))
		b.WriteString(source[m[0]:m[1]])
		last = m[1]
	}
	b.WriteString(reBullet.ReplaceAllString(source[last:], "${1}• "))
	return b.String()
}

func regexFinder(pattern string) func(string, []rune) []span {
	re := regexp.MustCompile(pattern)
	return func(text string, _ []rune) []span {
		// Byte offsets to code point offsets, once.
		at := make([]int, len(text)+1)
		n := 0
		for i := range text {
			at[i] = n
			n++
		}
		at[len(text)] = n
		var out []span
		for _, m := range re.FindAllStringSubmatchIndex(text, -1) {
			out = append(out, span{at[m[0]], at[m[1]], at[m[2]], at[m[3]]})
		}
		return out
	}
}

// italics finds the two italic patterns for the marker * or _:
//
//	(?<!\*)\*(?!\*| )(.+?)(?<!\*)\*(?!\*)
//	(?<!\w)_(?!_)(.+?)(?<!_)_(?!\w)
//
// A group is one or more characters other than a newline, as short as will
// let the closing marker match.
func italics(r []rune, marker rune) []span {
	var out []span
	n := len(r)
	for p := 0; p < n; {
		open := r[p] == marker && p+1 < n && r[p+1] != marker
		if marker == '*' {
			open = open && (p == 0 || r[p-1] != '*') && r[p+1] != ' '
		} else {
			open = open && (p == 0 || !isWordRune(r[p-1]))
		}
		end := -1
		if open {
			for e := p + 2; e < n && r[e-1] != '\n'; e++ {
				if r[e] != marker || r[e-1] == marker {
					continue
				}
				if e+1 < n && (r[e+1] == marker && marker == '*' || marker == '_' && isWordRune(r[e+1])) {
					continue
				}
				end = e
				break
			}
		}
		if end < 0 {
			p++
			continue
		}
		out = append(out, span{p, end + 1, p + 1, end})
		p = end + 1
	}
	return out
}

func isWordRune(r rune) bool { return r == '_' || unicode.IsLetter(r) || unicode.IsNumber(r) }

// isPySpace is whitespace for the characters that matter here: Unicode
// spaces plus the control separators 0x1c to 0x1f.
func isPySpace(r rune) bool { return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f) }

func runeLen(s string) int { return utf8.RuneCountInString(s) }

// slice is s[a:b] with no panics: empty when a >= b, never out of range.
func slice(s string, a, b int) string {
	if a >= b {
		return ""
	}
	return s[a:b]
}

func utf16Len(r []rune) int {
	n := 0
	for _, c := range r {
		if c >= 0x10000 {
			n += 2
		} else {
			n++
		}
	}
	return n
}
