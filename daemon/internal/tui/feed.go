package tui

import (
	"fmt"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
)

type blockKind int

const (
	bkIntro blockKind = iota
	bkNote
	bkUser
	bkAssistant
	bkReasoning
	bkTool
)

type noteLine struct {
	text  string
	muted bool
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

	folded bool // reasoning

	ver   int // bumped on every change that alters the drawing
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
func (f *feed) note(text string, isMuted bool) {
	lines := strings.Split(clean(text), "\n")
	if n := len(f.blocks); n > 0 && f.blocks[n-1].kind == bkNote && len(f.blocks[n-1].notes)+len(lines) <= maxNoteLines {
		b := f.blocks[n-1]
		for _, l := range lines {
			b.notes = append(b.notes, noteLine{l, isMuted})
		}
		b.ver++
		return
	}
	b := &block{kind: bkNote}
	for _, l := range lines {
		b.notes = append(b.notes, noteLine{l, isMuted})
	}
	f.add(b)
}

// reset drops everything but the first block when it is the intro.
func (f *feed) clearKeepIntro() {
	var keep []*block
	if len(f.blocks) > 0 && f.blocks[0].kind == bkIntro {
		keep = f.blocks[:1]
	}
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
	case bkIntro:
		return true
	case bkNote:
		return len(prev.notes) > 1
	}
	return false
}

// count is the total number of rows at content width w.
func (f *feed) count(w int, now time.Time, spin int) int {
	n := 0
	var prev *block
	for _, b := range f.blocks {
		if gapBefore(prev, b) {
			n++
		}
		n += len(b.lines(w, now, spin))
		prev = b
	}
	return n
}

// window returns rows rows of the feed starting at row top, each already
// inset by the content margin.
func (f *feed) window(w int, now time.Time, spin, top, rows int) []string {
	out := make([]string, 0, rows)
	pos := 0
	var prev *block
	emit := func(l string) {
		if pos >= top && len(out) < rows {
			out = append(out, l)
		}
		pos++
	}
	for _, b := range f.blocks {
		if pos >= top+rows {
			break
		}
		if gapBefore(prev, b) {
			emit("")
		}
		ls := b.lines(w, now, spin)
		if pos+len(ls) <= top { // wholly above the window
			pos += len(ls)
		} else {
			for _, l := range ls {
				emit(l)
			}
		}
		prev = b
	}
	return out
}

// lines draws the block at content width w.
func (b *block) lines(w int, now time.Time, spin int) []string {
	if b.kind == bkTool && b.running {
		return b.drawTool(w, now, spin)
	}
	if b.cache != nil && b.cw == w && b.cver == b.ver {
		return b.cache
	}
	var ls []string
	switch b.kind {
	case bkIntro, bkNote:
		for _, n := range b.notes {
			for _, l := range wrapText(n.text, w-6) {
				if n.muted || b.kind == bkIntro {
					l = muted.Render(l)
				}
				ls = append(ls, "   "+l)
			}
		}
	case bkUser:
		ls = b.drawUser(w)
	case bkAssistant:
		for _, l := range wrapText(strings.TrimRight(strings.TrimLeft(b.shown, "\n"), " \n"), w-6) {
			ls = append(ls, "   "+l)
		}
	case bkReasoning:
		ls = b.drawReasoning(w)
	case bkTool:
		ls = b.drawTool(w, now, spin)
	}
	b.cache, b.cw, b.cver = ls, w, b.ver
	return ls
}

func (b *block) drawUser(w int) []string {
	bg := lipgloss.NewStyle().Background(panelBg)
	rail := bg.Foreground(lipgloss.Color("220")).Render("▎")
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

func (b *block) drawReasoning(w int) []string {
	text := strings.TrimSpace(b.shown)
	if b.folded {
		n := len(strings.Split(strings.TrimSpace(b.text), "\n"))
		s := "line"
		if n != 1 {
			s = "lines"
		}
		return []string{"   " + thinking.Render(fmt.Sprintf("▶ reasoning · %d %s", n, s))}
	}
	ls := []string{"   " + thinking.Render("▼ reasoning")}
	if text != "" {
		for _, l := range wrapText(text, w-10) {
			ls = append(ls, "     "+thinking.Render(l))
		}
	}
	return ls
}

// drawTool is one row: icon, summary, and how long it took (or is taking).
func (b *block) drawTool(w int, now time.Time, spin int) []string {
	label := b.label
	suffix := ""
	switch {
	case b.running:
		if s := int(now.Sub(b.started).Seconds()); s >= 1 {
			suffix = fmt.Sprintf("  %ds", s)
		}
	case b.took > 0:
		suffix = fmt.Sprintf("  %.1fs", b.took.Seconds())
	}
	room := max(w-2-textWidth(suffix), 1)
	label = ellipsize(label, room)
	switch {
	case b.running:
		return []string{toolSt.Render(spinnerFrame(spin)) + " " + label + muted.Render(suffix)}
	case b.isErr:
		return []string{errSt.Render("✗ " + label + suffix)}
	}
	return []string{muted.Render(toolIcon(b.name)+" "+label) + muted.Render(suffix)}
}
