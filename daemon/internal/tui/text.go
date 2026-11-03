package tui

import (
	"regexp"
	"strconv"
	"strings"
	"unicode"

	"github.com/charmbracelet/lipgloss"
)

var ansiRE = regexp.MustCompile("\x1b\\[[0-9;?<>=:]*[ -/]*[@-~]|\x1b\\][^\x07\x1b]*(?:\x07|\x1b\\\\)|\x1b[@-Z\\\\-_]")

func stripANSI(s string) string { return ansiRE.ReplaceAllString(s, "") }

// clean makes text safe to draw: no escapes, carriage returns or control
// characters, tabs as four spaces; newlines stay.
func clean(s string) string {
	s = stripANSI(s)
	s = strings.ReplaceAll(s, "\r\n", "\n")
	return strings.Map(func(r rune) rune {
		switch {
		case r == '\n':
			return r
		case r < 32 || r == 0x7f || (r >= 0x80 && r < 0xa0):
			return -1
		}
		return r
	}, strings.ReplaceAll(s, "\t", "    "))
}

// runeWidth is the number of terminal columns r takes.
func runeWidth(r rune) int {
	switch {
	case r < 0x20:
		return 0
	case r < 0x7f:
		return 1
	}
	return lipgloss.Width(string(r))
}

// textWidth is the width of plain text (no escapes).
func textWidth(s string) int {
	n := 0
	for _, r := range s {
		n += runeWidth(r)
	}
	return n
}

// cut returns the longest prefix of s at most w columns wide.
func cut(s string, w int) string {
	n := 0
	for i, r := range s {
		rw := runeWidth(r)
		if n+rw > w {
			return s[:i]
		}
		n += rw
	}
	return s
}

// ellipsize shortens s to w columns, ending in … when it was cut.
func ellipsize(s string, w int) string {
	if w < 1 {
		return ""
	}
	if textWidth(s) <= w {
		return s
	}
	return cut(s, w-1) + "…"
}

// wrapText wraps plain text to w columns: words move to the next row, a word
// wider than a row is split, and the text's own line breaks stay.
func wrapText(s string, w int) []string {
	if w < 1 {
		w = 1
	}
	var out []string
	for _, line := range strings.Split(s, "\n") {
		out = append(out, wrapLine(line, w)...)
	}
	return out
}

func wrapLine(s string, w int) []string {
	if textWidth(s) <= w {
		return []string{strings.TrimRight(s, " ")}
	}
	var (
		out  []string
		cur  strings.Builder
		curW int
	)
	flush := func() {
		out = append(out, strings.TrimRight(cur.String(), " "))
		cur.Reset()
		curW = 0
	}
	rs := []rune(s)
	for i := 0; i < len(rs); {
		j := i
		space := rs[i] == ' '
		for j < len(rs) && (rs[j] == ' ') == space {
			j++
		}
		tok := string(rs[i:j])
		tw := textWidth(tok)
		i = j
		switch {
		case space:
			if curW+tw <= w && (curW > 0 || len(out) == 0) {
				cur.WriteString(tok)
				curW += tw
			}
		case curW+tw <= w:
			cur.WriteString(tok)
			curW += tw
		case tw <= w:
			flush()
			cur.WriteString(tok)
			curW = tw
		default: // a word wider than a row
			if curW > 0 {
				flush()
			}
			for _, r := range tok {
				rw := runeWidth(r)
				if curW+rw > w {
					flush()
				}
				cur.WriteRune(r)
				curW += rw
			}
		}
	}
	if curW > 0 || len(out) == 0 {
		flush()
	}
	return out
}

// draftRows is the number of rows the text area gives a draft at width w: its
// own word wrapping (bubbles/textarea.wrap), line by line, at least one.
func draftRows(s string, w int) int {
	n := 0
	for _, line := range strings.Split(s, "\n") {
		n += len(taWrap([]rune(line), w))
	}
	return max(n, 1)
}

// taWrap mirrors the text area's soft wrapping, to know its height before it draws.
func taWrap(runes []rune, width int) [][]rune {
	var (
		lines  = [][]rune{{}}
		word   []rune
		row    int
		spaces int
	)
	spaceRunes := func(n int) []rune { return []rune(strings.Repeat(" ", n)) }
	for _, r := range runes {
		if r == ' ' || isSpaceRune(r) {
			spaces++
		} else {
			word = append(word, r)
		}
		if spaces > 0 {
			if textWidth(string(lines[row]))+textWidth(string(word))+spaces > width {
				row++
				lines = append(lines, []rune{})
			}
			lines[row] = append(lines[row], word...)
			lines[row] = append(lines[row], spaceRunes(spaces)...)
			spaces, word = 0, nil
		} else if textWidth(string(word))+runeWidth(word[len(word)-1]) > width {
			if len(lines[row]) > 0 {
				row++
				lines = append(lines, []rune{})
			}
			lines[row] = append(lines[row], word...)
			word = nil
		}
	}
	if textWidth(string(lines[row]))+textWidth(string(word))+spaces >= width {
		lines = append(lines, append(append([]rune{}, word...), spaceRunes(spaces+1)...))
	} else {
		lines[row] = append(append(lines[row], word...), spaceRunes(spaces+1)...)
	}
	return lines
}

func isSpaceRune(r rune) bool { return unicode.IsSpace(r) }

// pad appends spaces to s (styled or not) up to w columns.
func pad(s string, w int) string {
	if n := lipgloss.Width(s); n < w {
		return s + strings.Repeat(" ", w-n)
	}
	return s
}

// commas formats n with thousands separators.
func commas(n int) string {
	s := strconv.Itoa(abs(n))
	for i := len(s) - 3; i > 0; i -= 3 {
		s = s[:i] + "," + s[i:]
	}
	if n < 0 {
		s = "-" + s
	}
	return s
}

func abs(n int) int {
	if n < 0 {
		return -n
	}
	return n
}

// squash collapses every run of whitespace to one space.
func squash(s string) string { return strings.Join(strings.Fields(s), " ") }

// firstLine is the first line of s, cut to n runes.
func firstLine(s string, n int) string {
	s, _, _ = strings.Cut(s, "\n")
	if r := []rune(s); len(r) > n {
		return string(r[:n])
	}
	return s
}
