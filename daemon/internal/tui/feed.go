package tui

import (
	"fmt"
	"strings"
	"time"
)

type blockKind int

const (
	bkLogo blockKind = iota
	bkIntro
	bkNote
	bkUser
	bkAssistant
	bkReasoning
	bkTool
	bkHint
)

type noteLine struct {
	text  string
	muted bool
	err   bool
}

// block is one thing in the transcript.
type block struct {
	kind  blockKind
	text  string // user, assistant and reasoning: what has arrived
	shown string // assistant and reasoning: what is drawn
	notes []noteLine

	// tool rows
	callID, name, label string
	running, isErr      bool
	started             time.Time
	took                time.Duration

	folded   bool   // reasoning
	expanded bool   // tool rows: the whole label, wrapped
	detail   string // tool rows: a muted second line

	ver   int // bumped on every change that alters the drawing
	cth   *theme
	cw    int
	cver  int
	cache []string
}

const maxNoteLines = 100

// feed is the transcript. It is drawn bottom-anchored, from cached lines.
type feed struct {
	blocks []*block
	calls  map[string]*block
}

func newFeed() *feed { return &feed{calls: map[string]*block{}} }

func (f *feed) add(b *block) *block {
	f.blocks = append(f.blocks, b)
	if b.kind == bkTool && b.callID != "" {
		f.calls[b.callID] = b
	}
	return b
}

// note adds muted or plain lines; consecutive notes share one block.
func (f *feed) note(text string, isMuted bool) { f.addNote(text, isMuted, false) }

// errNote adds lines in the error colour.
func (f *feed) errNote(text string) { f.addNote(text, false, true) }

func (f *feed) addNote(text string, isMuted, isErr bool) {
	lines := strings.Split(clean(text), "\n")
	if n := len(f.blocks); n > 0 && f.blocks[n-1].kind == bkNote && len(f.blocks[n-1].notes)+len(lines) <= maxNoteLines {
		b := f.blocks[n-1]
		for _, l := range lines {
			b.notes = append(b.notes, noteLine{l, isMuted, isErr})
		}
		b.ver++
		return
	}
	b := &block{kind: bkNote}
	for _, l := range lines {
		b.notes = append(b.notes, noteLine{l, isMuted, isErr})
	}
	f.add(b)
}

// clearKeepIntro drops everything but the wordmark and the intro at the top.
func (f *feed) clearKeepIntro() {
	n := 0
	for n < len(f.blocks) && (f.blocks[n].kind == bkLogo || f.blocks[n].kind == bkIntro) {
		n++
	}
	keep := f.blocks[:n]
	f.blocks = keep
	f.calls = map[string]*block{}
}

// gapBefore reports whether a blank line separates prev from cur (F._separate).
func gapBefore(prev, cur *block) bool {
	if prev == nil {
		return false
	}
	switch cur.kind {
	case bkUser, bkAssistant, bkReasoning:
		return true
	}
	switch prev.kind {
	case bkUser, bkAssistant, bkReasoning:
		return true
	case bkLogo, bkIntro:
		return true
	case bkNote:
		return len(prev.notes) > 1
	}
	return false
}

// count is the total number of rows at content width w.
func (f *feed) count(w int, th *theme, now time.Time, spin int) int {
	n := 0
	var prev *block
	for _, b := range f.blocks {
		if gapBefore(prev, b) {
			n++
		}
		n += len(b.lines(w, th, now, spin))
		prev = b
	}
	return n
}

// rowRef says which block, and which of its lines, a drawn row shows; b is
// nil for the blank row between blocks.
type rowRef struct {
	b    *block
	line int
}

// window returns rows rows of the feed starting at row top, each already
// inset by the content margin, and what each row shows.
func (f *feed) window(w int, th *theme, now time.Time, spin, top, rows int) ([]string, []rowRef) {
	out := make([]string, 0, rows)
	refs := make([]rowRef, 0, rows)
	pos := 0
	var prev *block
	emit := func(l string, b *block, line int) {
		if pos >= top && len(out) < rows {
			out = append(out, l)
			refs = append(refs, rowRef{b, line})
		}
		pos++
	}
	for _, b := range f.blocks {
		if pos >= top+rows {
			break
		}
		if gapBefore(prev, b) {
			emit("", nil, 0)
		}
		ls := b.lines(w, th, now, spin)
		if pos+len(ls) <= top { // wholly above the window
			pos += len(ls)
		} else {
			for i, l := range ls {
				emit(l, b, i)
			}
		}
		prev = b
	}
	return out, refs
}

// lines draws the block at content width w.
func (b *block) lines(w int, th *theme, now time.Time, spin int) []string {
	if b.kind == bkTool && b.running {
		return b.drawTool(w, th, now, spin)
	}
	if b.cache != nil && b.cw == w && b.cver == b.ver && b.cth == th {
		return b.cache
	}
	var ls []string
	switch b.kind {
	case bkLogo:
		ls = drawLogo(w, th)
	case bkIntro, bkNote:
		for _, n := range b.notes {
			for _, l := range wrapText(n.text, w-6) {
				switch {
				case n.err:
					l = th.errS.Render(l)
				case n.muted || b.kind == bkIntro:
					l = th.mut.Render(l)
				}
				ls = append(ls, "   "+l)
			}
		}
	case bkUser:
		ls = b.drawUser(w, th)
	case bkAssistant:
		ls = markdownRows(th, strings.TrimRight(strings.TrimLeft(b.shown, "\n"), " \n"), w)
	case bkReasoning:
		ls = b.drawReasoning(w, th)
	case bkTool:
		ls = b.drawTool(w, th, now, spin)
	case bkHint:
		ls = []string{th.plain.Render("ctrl+x down") + th.mut.Render(" view subagents")}
	}
	b.cache, b.cw, b.cver, b.cth = ls, w, b.ver, th
	return ls
}

// wordmark is the largest form of the name that fits w columns.
func wordmark(w int) string {
	switch {
	case w >= 109:
		return logoWide
	case w >= 52:
		return logoStacked
	}
	return "SON OF ANTON"
}

// drawLogo is the wordmark, bold in the accent colour, every line with the
// same left pad so the art's tapering rows stay under the rows above them.
func drawLogo(w int, th *theme) []string {
	lines := strings.Split(wordmark(w), "\n")
	widest := 0
	for _, l := range lines {
		widest = max(widest, textWidth(l))
	}
	pad := strings.Repeat(" ", max((w-widest)/2, 0))
	st := th.acc.Bold(true)
	out := make([]string, len(lines))
	for i, l := range lines {
		out[i] = pad + st.Render(cut(l, max(w-len(pad), 0)))
	}
	return out
}

func (b *block) drawUser(w int, th *theme) []string {
	bg := th.r.NewStyle().Background(th.panel)
	rail := bg.Foreground(th.accent).Render("▎")
	row := func(text string) string {
		if text == "" {
			return rail + bg.Render(strings.Repeat(" ", max(w-1, 0)))
		}
		body := "  " + text
		return rail + bg.Bold(true).Render(pad(body, max(w-1, 0)))
	}
	ls := []string{row("")}
	for _, l := range wrapText(strings.TrimSpace(b.text), w-6) {
		ls = append(ls, row(l))
	}
	return append(ls, row(""))
}

func (b *block) drawReasoning(w int, th *theme) []string {
	text := strings.TrimSpace(b.shown)
	if b.folded {
		n := len(strings.Split(strings.TrimSpace(b.text), "\n"))
		s := "line"
		if n != 1 {
			s = "lines"
		}
		return []string{"   " + th.think.Render(fmt.Sprintf("▶ reasoning · %d %s", n, s))}
	}
	ls := []string{"   " + th.think.Render("▼ reasoning")}
	if text != "" {
		for _, l := range wrapText(text, w-10) {
			ls = append(ls, "     "+th.think.Render(l))
		}
	}
	return ls
}

// drawTool is the row: icon, summary, and how long it took (or is taking),
// then a muted detail line. A task row shows no time of its own: its child
// has it. An expanded row wraps its whole label, continuation rows indented 2.
func (b *block) drawTool(w int, th *theme, now time.Time, spin int) []string {
	suffix := ""
	task := b.name == "task"
	switch {
	case b.running:
		if s := int(now.Sub(b.started).Seconds()); s >= 1 && !task {
			suffix = fmt.Sprintf("  %ds", s)
		}
	case b.took > 0 && !task:
		suffix = fmt.Sprintf("  %.1fs", b.took.Seconds())
	}
	room := max(w-2-textWidth(suffix), 1)
	parts := []string{ellipsize(b.label, room)}
	if b.expanded {
		parts = hardWrap(b.label, room)
	}
	var ls []string
	for i, p := range parts {
		sfx := ""
		if i == len(parts)-1 {
			sfx = suffix
		}
		first := i == 0
		switch {
		case b.running:
			head := "  "
			if first {
				head = th.toolS.Render(spinnerFrame(spin)) + " "
			}
			ls = append(ls, head+p+th.mut.Render(sfx))
		case b.isErr:
			head := "  "
			if first {
				head = "✗ "
			}
			ls = append(ls, th.errS.Render(head+p+sfx))
		default:
			if first {
				ls = append(ls, th.toolS.Render(toolIcon(b.name))+th.mut.Render(" "+p+sfx))
			} else {
				ls = append(ls, th.mut.Render("  "+p+sfx))
			}
		}
	}
	if b.detail != "" {
		ls = append(ls, "  "+th.mut.Render(ellipsize(b.detail, max(w-2, 1))))
	}
	return ls
}

// hardWrap cuts s into rows of at most w columns, wherever the column falls.
func hardWrap(s string, w int) []string {
	var out []string
	for textWidth(s) > w {
		c := cut(s, w)
		if c == "" {
			break
		}
		out = append(out, c)
		s = s[len(c):]
	}
	return append(out, s)
}
