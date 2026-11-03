package tui

// Phase 9d: the mouse folds rows, copies a selection and scrolls
// (daemon/phases/09d-tui-subagents-mouse.md).

import (
	"strings"
	"time"
	"unicode/utf8"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/x/ansi"
)

const (
	wheelRows = 3
	copyShow  = 1500 * time.Millisecond
)

type copyExpiredMsg struct{}

// feedLayout is the feed as last drawn: where it starts and what each row shows.
type feedLayout struct {
	top, rows, cw int
	refs          []rowRef
}

// cell is where the left button went down: a screen column and row, and the
// feed line under it.
type cell struct{ x, y, line int }

// selection runs from (l0,c0) to (l1,c1), the last cell excluded: feed
// lines and screen columns, so it stays on its text as the feed scrolls.
type selection struct{ l0, c0, l1, c1 int }

// cols is the columns of line the selection covers, within room columns.
func (s *selection) cols(line, room int) (a, b int) {
	a, b = 0, room
	if line == s.l0 {
		a = s.c0
	}
	if line == s.l1 {
		b = s.c1
	}
	return min(max(a, 0), room), min(max(b, 0), room)
}

// selected draws the part of the row l (a feed line, margin included) that
// the selection covers in reverse video.
func (m *model) selected(l string, line int) string {
	s := m.sel
	if s == nil || line < s.l0 || line > s.l1 {
		return l
	}
	a, b := s.cols(line, m.room())
	if b <= a {
		return l
	}
	rev := m.th.r.NewStyle().Reverse(true)
	return ansi.Cut(l, 0, a) + rev.Render(stripANSI(ansi.Cut(l, a, b))) + ansi.Cut(l, b, 1<<20)
}

// selText is the selected text: each row's trailing spaces trimmed, rows
// joined with newlines.
func (m *model) selText() string {
	s := m.sel
	if s == nil {
		return ""
	}
	now := time.Now()
	var rows []string
	for line := s.l0; line <= s.l1; line++ {
		ls, _ := m.feed.window(m.contentWidth(), m.th, now, m.spin, line, 1)
		full := margin
		if len(ls) > 0 {
			full += stripANSI(ls[0])
		}
		a, b := s.cols(line, m.room())
		text := ""
		if b > a {
			text = ansi.Cut(full, a, b)
		}
		rows = append(rows, strings.TrimRight(text, " "))
	}
	return strings.Join(rows, "\n")
}

func (m *model) copyShown(now time.Time) bool {
	return !m.copyAt.IsZero() && now.Sub(m.copyAt) < copyShow
}

// mouse handles a mouse event on whichever view is showing.
func (m *model) mouse(k tea.MouseMsg) tea.Cmd {
	if m.quitting || len(m.modals) > 0 || m.picker != nil {
		return nil
	}
	if m.view != nil {
		if !m.view.loaded {
			return nil
		}
		return m.inChild(func() tea.Cmd { return m.mouseIn(k) })
	}
	return m.mouseIn(k)
}

func (m *model) mouseIn(k tea.MouseMsg) tea.Cmd {
	inFeed := k.Y >= 0 && k.Y < m.lay.rows && k.X >= 0 && k.X < m.room()
	switch {
	case k.Button == tea.MouseButtonWheelUp || k.Button == tea.MouseButtonWheelDown:
		if k.Action == tea.MouseActionPress && inFeed {
			if k.Button == tea.MouseButtonWheelUp {
				m.scrollBy(-wheelRows)
			} else {
				m.scrollBy(wheelRows)
			}
		}
	case k.Action == tea.MouseActionPress && k.Button == tea.MouseButtonLeft:
		m.sel, m.press = nil, nil
		if inFeed {
			m.press = &cell{k.X, k.Y, m.lay.top + k.Y}
		}
	case k.Action == tea.MouseActionMotion && m.press != nil:
		m.dragTo(k.X, k.Y)
	case k.Action == tea.MouseActionRelease && m.press != nil:
		p := m.press
		m.press = nil
		m.dragTo(k.X, k.Y)
		if m.sel == nil {
			m.click(p)
			return nil
		}
		return m.copySelection()
	}
	return nil
}

// dragTo extends the selection from the press to the cell (x, y).
func (m *model) dragTo(x, y int) {
	p := m.press
	if p == nil {
		return
	}
	y = min(max(y, 0), max(m.lay.rows-1, 0))
	x = min(max(x, 0), m.room())
	a, b := [2]int{p.line, p.x}, [2]int{m.lay.top + y, x}
	if a == b {
		m.sel = nil
		return
	}
	if b[0] < a[0] || (b[0] == a[0] && b[1] < a[1]) {
		a, b = b, a
	}
	m.sel = &selection{a[0], a[1], b[0], b[1]}
}

// click folds or unfolds the row under the press.
func (m *model) click(p *cell) {
	if p.y >= len(m.lay.refs) {
		return
	}
	ref := m.lay.refs[p.y]
	b := ref.b
	switch {
	case b == nil:
	case b.kind == bkReasoning && ref.line == 0:
		b.folded = !b.folded
		b.ver++
	case b.kind == bkTool:
		b.expanded = !b.expanded
		b.ver++
	}
}

// copySelection copies the selected text to the terminal (OSC 52) and notes
// it on the status line for a moment. Nothing but blanks copies nothing.
func (m *model) copySelection() tea.Cmd {
	text := m.selText()
	if strings.TrimSpace(text) == "" {
		return nil
	}
	m.copied, m.copiedN, m.copyAt = text, utf8.RuneCountInString(strings.TrimSpace(text)), time.Now()
	m.app.copy(text)
	return tea.Tick(copyShow+50*time.Millisecond, func(time.Time) tea.Msg { return copyExpiredMsg{} })
}

// scrollBy moves the feed by rows (negative up), following again at the bottom.
func (m *model) scrollBy(rows int) {
	_, vis := m.feedRows()
	total := m.feed.count(m.contentWidth(), m.th, time.Now(), m.spin)
	bottom := max(total-vis, 0)
	if m.follow {
		m.top = bottom
	}
	m.top = min(max(m.top+rows, 0), bottom)
	m.follow = m.top >= bottom
}
