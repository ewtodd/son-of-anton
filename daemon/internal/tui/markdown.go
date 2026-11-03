package tui

import (
	"fmt"
	"strings"

	"github.com/yuin/goldmark"
	"github.com/yuin/goldmark/ast"
	"github.com/yuin/goldmark/extension"
	extast "github.com/yuin/goldmark/extension/ast"
	"github.com/yuin/goldmark/text"
	"github.com/yuin/goldmark/util"
)

// Markdown is a markdown block's rows at feed width width, styled: the Textual
// feed's renderer (son_of_anton_tui/feed.py render_markdown_lines) in the
// fork's plain aesthetic. Rows are not padded to the width.
func Markdown(src string, width int) []string {
	return markdownRows(markdownTheme(), src, width)
}

var mdParser = goldmark.New(goldmark.WithExtensions(extension.Table, extension.Strikethrough, extension.Linkify)).Parser()

// rstyle is the inline style of a rune.
type rstyle uint8

const (
	sBold rstyle = 1 << iota
	sItalic
	sStrike
	sUnder
	sMuted
)

type srune struct {
	r  rune
	st rstyle
}

type mdr struct {
	th    *theme
	src   []byte
	lines []string // the source's lines, for tables
	w     int      // the feed width
}

// layout is where a block's rows go: top level rows carry the 3-column pad
// and may use the whole width (code, rules, HTML); rows of a list item or a
// quote are drawn at avail columns and prefixed by their container.
type layout struct {
	top   bool
	avail int
}

func markdownRows(th *theme, src string, width int) []string {
	b := []byte(src)
	doc := mdParser.Parse(text.NewReader(b))
	m := &mdr{th: th, src: b, lines: strings.Split(src, "\n"), w: width}
	return m.blocks(doc, layout{true, max(width-6, 1)})
}

func (m *mdr) pad(l layout) string {
	if l.top {
		return "   "
	}
	return ""
}

func paragraphLike(n ast.Node) bool {
	switch n.(type) {
	case *ast.Paragraph, *ast.TextBlock, *ast.Heading:
		return true
	}
	return false
}

func (m *mdr) blocks(parent ast.Node, l layout) []string {
	var out []string
	pad := m.pad(l)
	// code, rules and HTML run from column 0 at the top level and are cropped.
	wide := func() int {
		if l.top {
			return m.w
		}
		return l.avail
	}
	for c := parent.FirstChild(); c != nil; c = c.NextSibling() {
		if !l.top && len(out) > 0 && paragraphLike(c) && paragraphLike(c.PreviousSibling()) {
			out = append(out, "") // parts of a list item or a quote are a blank row apart
		}
		switch v := c.(type) {
		case *ast.Heading:
			runs := m.inline(v, 0)
			for _, row := range wrapRuns(runs, l.avail) {
				out = append(out, pad+m.renderRuns(row, v.Level <= 2, true))
			}
		case *ast.Paragraph, *ast.TextBlock:
			for _, row := range wrapRuns(m.inline(v, 0), l.avail) {
				out = append(out, pad+m.renderRuns(row, false, false))
			}
		case *ast.FencedCodeBlock, *ast.CodeBlock:
			for _, line := range m.codeLines(v) {
				out = append(out, m.th.mut.Render(cut("▎"+strings.ReplaceAll(line, "\t", "    "), wide()-m.codeMargin(l))))
			}
		case *extast.Table:
			for _, line := range m.table(v, l) {
				out = append(out, pad+m.th.plain.Render(cut(line, l.avail)))
			}
		case *ast.List:
			out = append(out, m.list(v, l)...)
		case *ast.Blockquote:
			for _, row := range m.blocks(v, layout{false, max(l.avail-2, 1)}) {
				out = append(out, pad+m.th.think.Render("▎")+row)
			}
		case *ast.ThematicBreak:
			out = append(out, m.th.mut.Render(strings.Repeat("─", wide())))
		case *ast.HTMLBlock:
			var html strings.Builder
			for i := 0; i < v.Lines().Len(); i++ {
				s := v.Lines().At(i)
				html.Write(s.Value(m.src))
			}
			if v.HasClosure() {
				html.Write(v.ClosureLine.Value(m.src))
			}
			for _, line := range strings.Split(strings.TrimRight(html.String(), "\n"), "\n") {
				out = append(out, cut(strings.ReplaceAll(line, "\t", "    "), wide()-m.codeMargin(l)))
			}
		}
	}
	return out
}

// codeMargin is the columns a full-width row leaves free at the right.
func (m *mdr) codeMargin(l layout) int {
	if l.top {
		return 3
	}
	return 0
}

func (m *mdr) codeLines(n ast.Node) []string {
	var b strings.Builder
	ls := n.Lines()
	for i := 0; i < ls.Len(); i++ {
		s := ls.At(i)
		b.Write(s.Value(m.src))
	}
	return strings.Split(strings.TrimRight(b.String(), "\n"), "\n")
}

func (m *mdr) list(v *ast.List, l layout) []string {
	var out []string
	pad := m.pad(l)
	n := 1
	if v.IsOrdered() {
		n = max(v.Start, 0)
	}
	for item := v.FirstChild(); item != nil; item = item.NextSibling() {
		marker := "• "
		if v.IsOrdered() {
			marker = fmt.Sprintf("%d. ", n)
			n++
		}
		hang := textWidth(marker)
		rows := m.blocks(item, layout{false, max(l.avail-hang, 1)})
		if len(rows) == 0 {
			rows = []string{""}
		}
		for k, row := range rows {
			lead := strings.Repeat(" ", hang)
			if k == 0 {
				lead = m.th.think.Render(marker)
			}
			out = append(out, pad+lead+row)
		}
	}
	return out
}

// table is the rows of a table, realigned. Top-level tables are realigned from
// their source lines, as F does, so cells keep their markup; others from the
// text of their cells.
func (m *mdr) table(t *extast.Table, l layout) []string {
	nrows := t.ChildCount()
	var raw string
	if l.top {
		start := -1
		_ = ast.Walk(t, func(n ast.Node, entering bool) (ast.WalkStatus, error) {
			if tx, ok := n.(*ast.Text); ok && entering && start < 0 {
				start = strings.Count(string(m.src[:tx.Segment.Start]), "\n")
				return ast.WalkStop, nil
			}
			return ast.WalkContinue, nil
		})
		if start >= 0 && start+nrows+1 <= len(m.lines) {
			raw = strings.Join(m.lines[start:start+nrows+1], "\n")
		}
	}
	if raw == "" {
		var rows []string
		for r := t.FirstChild(); r != nil; r = r.NextSibling() {
			var cells []string
			for c := r.FirstChild(); c != nil; c = c.NextSibling() {
				cells = append(cells, strings.ReplaceAll(squash(m.plain(c)), "|", "\\|"))
			}
			rows = append(rows, "| "+strings.Join(cells, " | ")+" |")
			if len(rows) == 1 {
				rows = append(rows, "|"+strings.Repeat("---|", len(cells)))
			}
		}
		raw = strings.Join(rows, "\n")
	}
	return strings.Split(realignMarkdownTables(raw, l.avail), "\n")
}

// ---- inline ----

func (m *mdr) text(v []byte) string {
	v = util.ResolveEntityNames(util.ResolveNumericReferences(util.UnescapePunctuations(v)))
	return string(v)
}

func collapseWS(s string) string {
	var b strings.Builder
	space := false
	for _, r := range s {
		if r == ' ' || r == '\t' || r == '\n' || r == '\r' || r == '\f' || r == '\v' {
			if !space {
				b.WriteByte(' ')
			}
			space = true
			continue
		}
		space = false
		b.WriteRune(r)
	}
	return b.String()
}

func appendRunes(out []srune, s string, st rstyle) []srune {
	for _, r := range s {
		out = append(out, srune{r, st})
	}
	return out
}

// plain is the visible text of a node.
func (m *mdr) plain(n ast.Node) string {
	var b strings.Builder
	for _, r := range m.inline(n, 0) {
		b.WriteRune(r.r)
	}
	return b.String()
}

func (m *mdr) inline(n ast.Node, base rstyle) []srune {
	var out []srune
	// The parser cuts a line of text at every space, so whitespace runs are
	// collapsed across those pieces.
	space := false
	addText := func(s string) {
		for _, r := range collapseWS(s) {
			if r == ' ' && space {
				continue
			}
			space = r == ' '
			out = append(out, srune{r, base})
		}
	}
	for c := n.FirstChild(); c != nil; c = c.NextSibling() {
		if _, ok := c.(*ast.Text); !ok {
			space = false
		}
		switch v := c.(type) {
		case *ast.Text:
			addText(m.text(v.Segment.Value(m.src)))
			switch {
			case v.HardLineBreak():
				out = append(out, srune{'\n', base})
				space = false
			case v.SoftLineBreak():
				addText(" ")
			}
		case *ast.String:
			out = appendRunes(out, collapseWS(string(v.Value)), base)
		case *ast.CodeSpan:
			var b strings.Builder
			for t := v.FirstChild(); t != nil; t = t.NextSibling() {
				if tx, ok := t.(*ast.Text); ok {
					b.Write(tx.Segment.Value(m.src))
				}
			}
			out = appendRunes(out, strings.ReplaceAll(b.String(), "\n", " "), base|sMuted)
		case *ast.Emphasis:
			if v.Level >= 2 {
				out = append(out, m.inline(v, base|sBold)...)
			} else {
				out = append(out, m.inline(v, base|sItalic)...)
			}
		case *extast.Strikethrough:
			out = append(out, m.inline(v, base|sStrike)...)
		case *ast.Link:
			out = append(out, m.inline(v, base|sUnder)...)
		case *ast.AutoLink:
			out = appendRunes(out, string(v.Label(m.src)), base|sUnder)
		case *ast.Image:
			alt := strings.TrimSpace(m.plain(v))
			if alt != "" {
				alt = "🖼 " + alt
			} else {
				alt = "🖼"
			}
			out = appendRunes(out, alt, base|sMuted)
		case *ast.RawHTML:
			for i := 0; i < v.Segments.Len(); i++ {
				s := v.Segments.At(i)
				out = appendRunes(out, collapseWS(string(s.Value(m.src))), base)
			}
		default:
			out = append(out, m.inline(c, base)...)
		}
	}
	return out
}

// renderRuns draws one row of styled runes; a heading is bold, and in the
// accent colour for levels 1 and 2.
func (m *mdr) renderRuns(row []srune, accent, heading bool) string {
	var b strings.Builder
	for i := 0; i < len(row); {
		j := i
		for j < len(row) && row[j].st == row[i].st {
			j++
		}
		var s strings.Builder
		for _, r := range row[i:j] {
			s.WriteRune(r.r)
		}
		st := m.th.plain
		if row[i].st&sMuted != 0 {
			st = m.th.mut
		}
		if row[i].st&sBold != 0 || heading {
			st = st.Bold(true)
		}
		if row[i].st&sItalic != 0 {
			st = st.Italic(true)
		}
		if row[i].st&sStrike != 0 {
			st = st.Strikethrough(true)
		}
		if row[i].st&sUnder != 0 {
			st = st.Underline(true)
		}
		if accent {
			st = st.Foreground(m.th.accent)
		}
		b.WriteString(st.Render(s.String()))
		i = j
	}
	return b.String()
}

// ---- wrapping ----

func runsWidth(rs []srune) int {
	n := 0
	for _, r := range rs {
		n += runeWidth(r.r)
	}
	return n
}

func trimRuns(rs []srune) []srune {
	for len(rs) > 0 && rs[len(rs)-1].r == ' ' {
		rs = rs[:len(rs)-1]
	}
	return rs
}

// wrapRuns wraps styled text to w columns like wrapText: words move down, a
// word wider than a row is split, hard breaks stay.
func wrapRuns(rs []srune, w int) [][]srune {
	w = max(w, 1)
	var out [][]srune
	start := 0
	for i := 0; i <= len(rs); i++ {
		if i == len(rs) || rs[i].r == '\n' {
			out = append(out, wrapRunLine(rs[start:i], w)...)
			start = i + 1
		}
	}
	return out
}

func wrapRunLine(rs []srune, w int) [][]srune {
	if runsWidth(rs) <= w {
		return [][]srune{trimRuns(rs)}
	}
	var (
		out  [][]srune
		cur  []srune
		curW int
	)
	flush := func() {
		out = append(out, trimRuns(cur))
		cur, curW = nil, 0
	}
	for i := 0; i < len(rs); {
		j := i
		space := rs[i].r == ' '
		for j < len(rs) && (rs[j].r == ' ') == space {
			j++
		}
		tok := rs[i:j]
		tw := runsWidth(tok)
		i = j
		switch {
		case space:
			if curW+tw <= w && (curW > 0 || len(out) == 0) {
				cur = append(cur, tok...)
				curW += tw
			}
		case curW+tw <= w:
			cur = append(cur, tok...)
			curW += tw
		case tw <= w:
			flush()
			cur = append(cur, tok...)
			curW = tw
		default: // a word wider than a row
			if curW > 0 {
				flush()
			}
			for _, r := range tok {
				rw := runeWidth(r.r)
				if curW+rw > w {
					flush()
				}
				cur = append(cur, r)
				curW += rw
			}
		}
	}
	if curW > 0 || len(out) == 0 {
		flush()
	}
	return out
}
