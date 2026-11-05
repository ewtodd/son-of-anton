package tui

import (
	"fmt"
	"math"
	"strconv"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/x/ansi"
)

const margin = "  "

const (
	sidebarWidth     = 42  // columns
	sidebarThreshold = 120 // the sidebar shows by itself on a screen wider than this
)

// sidebarState reports whether the sidebar is drawn, and whether it takes
// room from the feed (else it is drawn over the right 42 columns).
func (m *model) sidebarState() (show, inline bool) {
	wide := m.w > sidebarThreshold
	show = wide
	if m.sbForced != nil {
		show = *m.sbForced
	}
	return show, show && wide
}

// room is the width the feed column has.
func (m *model) room() int {
	if m.inPane { // a child's view has no sidebar
		return m.w
	}
	if _, inline := m.sidebarState(); inline {
		return m.w - sidebarWidth
	}
	return m.w
}

func (m *model) contentWidth() int { return max(m.room()-4, 1) }

// seg is a styled piece of a row.
type seg struct {
	t  string
	st lipgloss.Style
}

func segsWidth(ss []seg) int {
	n := 0
	for _, s := range ss {
		n += textWidth(s.t)
	}
	return n
}

// renderSegs draws the pieces in w columns, cutting the last that fits.
func renderSegs(ss []seg, w int) string {
	var b strings.Builder
	left := w
	for _, s := range ss {
		if left <= 0 {
			break
		}
		t := s.t
		if textWidth(t) > left {
			t = ellipsize(t, left)
		}
		b.WriteString(s.st.Render(t))
		left -= textWidth(t)
	}
	return b.String()
}

// taRows is the height of the text area: it grows with the draft, to ten rows
// at most and never so far that fewer than three feed rows remain.
func (m *model) taRows() int {
	rows := draftRows(m.ta.Value(), max(m.contentWidth()-3, 1))
	return min(rows, m.taCap())
}

// taCap is the most rows the text area may have now.
func (m *model) taCap() int {
	return max(min(maxTextRows, m.h-8-minFeedRows-m.queueRows()), 1)
}

func (m *model) queueRows() int {
	n := len(m.queue)
	if n == 0 {
		return 0
	}
	return min(n, 3) + b2i(n > 3) + 1 + 1 // strip, hint, and the blank row above
}

func b2i(b bool) int {
	if b {
		return 1
	}
	return 0
}

// feedRows is the number of rows left for the feed, and the dock's height.
func (m *model) feedRows() (dock, rows int) {
	if m.inPane {
		return childFooterRows, max(m.h-childFooterRows, 0)
	}
	dock = 8 + m.taRows() + m.queueRows() + m.compRows()
	return dock, max(m.h-dock, 0)
}

func (m *model) statusLine(cw int, now time.Time) string {
	th := m.th
	var left, right []seg
	idleRight := func() {
		if m.sess.CtxLen > 0 {
			pct := (100*m.sess.CtxTokens + m.sess.CtxLen/2) / m.sess.CtxLen
			right = append(right, seg{fmt.Sprintf("%d%% context  ·  ", pct), th.mut})
		}
		right = append(right, seg{"/help", th.bld}, seg{" commands", th.plain})
	}
	if m.busy() {
		label := "thinking"
		for i := len(m.feed.blocks) - 1; i >= 0; i-- {
			if b := m.feed.blocks[i]; b.kind == bkTool && b.running {
				label = b.label
				break
			}
		}
		secs := 0
		if !m.sess.Started.IsZero() {
			secs = max(int(now.Sub(m.sess.Started).Seconds()), 0)
		}
		left = []seg{{spinnerFrame(m.spin), th.bld}, {" ", th.plain}, {label, th.plain}, {fmt.Sprintf("  %ds", secs), th.mut}}
		right = []seg{{"ctrl+c", th.bld}, {" interrupt", th.plain}}
	} else {
		left = []seg{{tilde(m.sess.Cwd), th.plain}}
		if m.editing {
			left = []seg{{spinnerFrame(m.spin), th.bld}, {" ", th.plain}, {"running editor", th.plain}}
		}
		idleRight()
	}
	var pre []seg
	if len(m.running) > 0 {
		pre = append(pre, seg{"ctrl+x down", th.bld}, seg{" view subagents  ·  ", th.plain})
	}
	if m.copyShown(now) {
		pre = append(pre, seg{fmt.Sprintf("copied %d chars  ·  ", m.copiedN), th.mut})
	}
	right = append(pre, right...)
	var suffix []seg
	if n := len(m.queue); n > 0 {
		suffix = []seg{{fmt.Sprintf("  · %d queued", n), th.mut}}
	}
	rw := segsWidth(right)
	if rw+1 > cw { // too narrow for both: the right side alone
		return renderSegs(right, cw)
	}
	// The variable part of the left side (its widest piece) gives way first.
	room := cw - rw - 1 - segsWidth(suffix)
	if segsWidth(left) > room {
		ls := append([]seg(nil), left...)
		wi := 0
		for i, s := range ls {
			if textWidth(s.t) > textWidth(ls[wi].t) {
				wi = i
			}
		}
		other := segsWidth(ls) - textWidth(ls[wi].t)
		ls[wi].t = ellipsize(ls[wi].t, max(room-other, 1))
		left = ls
	}
	l := renderSegs(append(left, suffix...), max(cw-rw-1, 0))
	gap := cw - lipgloss.Width(l) - rw
	return l + strings.Repeat(" ", max(gap, 0)) + renderSegs(right, rw)
}

func (m *model) metaLine(w int) string {
	th := m.th
	mode := th.plain
	switch m.sess.Mode {
	case "yolo":
		mode = th.errS.Bold(true)
	case "lockdown":
		mode = th.toolS
	}
	effort := m.sess.Effort
	if effort == "" {
		effort = "default"
	}
	sep := seg{"  ·  ", th.mut}
	return renderSegs([]seg{
		{m.sess.Mode, mode}, sep,
		{modelShort(m.sess.Model, m.sess.Served), th.bld}, sep,
		{"reasoning " + effort, th.mut},
	}, w)
}

func (m *model) queueStrip(cw int) []string {
	th := m.th
	if len(m.queue) == 0 {
		return nil
	}
	var out []string
	for _, q := range m.queue[:min(len(m.queue), 3)] {
		out = append(out, th.mut.Render("↳ ")+ellipsize(squash(q), max(cw-5, 1)))
	}
	if n := len(m.queue) - 3; n > 0 {
		out = append(out, th.mut.Render(fmt.Sprintf("   +%d more", n)))
	}
	hint := "/sendnow sends this now"
	switch {
	case m.ctrlEnter && len(m.queue) > 1:
		hint = "ctrl+enter or /sendnow to send the oldest · /sendall to send all"
	case m.ctrlEnter:
		hint = "ctrl+enter or /sendnow to send now"
	case len(m.queue) > 1:
		hint = "/sendnow sends the oldest · /sendall sends all"
	}
	out = append(out, th.mut.Render(ellipsize(hint, cw)))
	for i := range out {
		out[i] = "   " + out[i]
	}
	return out
}

// render draws the whole screen: exactly m.h rows.
func (m *model) render() []string {
	if m.view != nil {
		return m.renderChild()
	}
	w, h := m.w, m.h
	th := m.th
	cw := m.contentWidth()
	now := time.Now()
	ta := m.taRows()
	m.ta.SetWidth(max(cw-3, 1))
	m.ta.SetHeight(ta)
	m.ta, _ = m.ta.Update(nil) // keep the cursor in view

	rail := th.acc.Render("▎")
	var dock []string
	if q := m.queueStrip(cw); q != nil {
		dock = append(dock, "")
		dock = append(dock, q...)
	}
	dock = append(dock, m.compLines(cw)...)
	// The prompt frame, from its first rail to its last, is on the surface.
	frame := func(l string) string {
		if th.surface == nil {
			return l
		}
		return th.paint(l, th.surface, cw)
	}
	dock = append(dock, "", frame(rail))
	taLines := strings.Split(m.ta.View(), "\n")
	if sg := m.suggestion(); sg != "" && ta == 1 {
		if row := m.suggestRow(sg, max(cw-3, 1)); row != "" {
			taLines[0] = row
		}
	}
	for _, l := range taLines {
		dock = append(dock, frame(rail+"  "+l))
	}
	dock = append(dock, frame(rail), frame(rail+"  "+m.metaLine(max(cw-3, 0))), frame(rail), "", m.statusLine(cw, now), "")
	for i, l := range dock {
		if l != "" {
			dock[i] = margin + l
		}
	}
	_, rows := m.feedRows()
	rows = min(rows, max(h-len(dock), 0))
	lines := m.paneRows(cw, rows, now)
	lines = append(lines, dock...)
	switch {
	case len(lines) > h:
		lines = lines[len(lines)-h:]
	case len(lines) < h:
		lines = append(make([]string, h-len(lines)), lines...)
	}
	if show, _ := m.sidebarState(); show && w > sidebarWidth {
		sb := m.sidebar(h, now)
		left := w - sidebarWidth
		for i, l := range lines {
			if lipgloss.Width(l) > left {
				l = ansi.Truncate(l, left, "")
			}
			lines[i] = l + strings.Repeat(" ", max(left-lipgloss.Width(l), 0)) + sb[i]
		}
	}
	m.overlay(lines, cw)
	for i, l := range lines {
		lines[i] = th.fix(l)
	}
	return lines
}

func (m *model) View() string { return strings.Join(m.render(), "\n") }

// ---- the sidebar ----

func fmtTokens(n int) string {
	switch {
	case n >= 1_000_000:
		return fmt.Sprintf("%.1fM", float64(n)/1e6)
	case n >= 1_000:
		return fmt.Sprintf("%.1fK", float64(n)/1e3)
	}
	return strconv.Itoa(n)
}

// fmtDuration is a session's age: 45s, 12m, 3h 5m, 2d 4h.
func fmtDuration(d time.Duration) string {
	s := max(int(d.Seconds()), 0)
	switch {
	case s < 60:
		return fmt.Sprintf("%ds", s)
	case s < 3600:
		return fmt.Sprintf("%dm", s/60)
	case s < 86400:
		return fmt.Sprintf("%dh %dm", s/3600, s%3600/60)
	}
	return fmt.Sprintf("%dd %dh", s/86400, s%86400/3600)
}

// sidebar is the 42 columns at the right edge: h rows on the panel colour.
func (m *model) sidebar(h int, now time.Time) []string {
	th, s := m.th, m.sess
	const inner = sidebarWidth - 5 // the border, then 2 columns of padding either side
	var body []string
	add := func(text string, st lipgloss.Style) { body = append(body, st.Render(ellipsize(text, inner))) }
	label := func(text string) {
		body = append(body, "")
		add(text, th.mut)
	}
	title := s.Title
	if title == "" {
		title = "untitled"
	}
	add(title, th.bld)
	add(s.ID, th.mut)
	add(tilde(s.Cwd), th.mut)
	label("mode")
	add(s.Mode, th.plain)
	label("reasoning")
	effort := s.Effort
	if effort == "" {
		effort = "default"
	}
	add(effort, th.mut)
	label("context")
	if s.CtxLen > 0 && s.CtxTokens > 0 {
		pct := (100*s.CtxTokens + s.CtxLen/2) / s.CtxLen
		filled := min(max(int(math.RoundToEven(float64(pct)/10)), 0), 10)
		add(strings.Repeat("▰", filled)+strings.Repeat("▱", 10-filled)+fmt.Sprintf("  %d%%", pct), th.acc)
		tokens := fmtTokens(s.CtxTokens) + " / " + fmtTokens(s.CtxLen)
		if s.Compactions > 0 {
			tokens += fmt.Sprintf(" · %d compacted", s.Compactions)
		}
		add(tokens, th.mut)
	} else {
		add("▱▱▱▱▱▱▱▱▱▱  —", th.mut)
		add("no turns yet", th.mut)
	}
	label("session usage")
	add(fmt.Sprintf("in %s · out %s · %d calls", fmtTokens(s.In), fmtTokens(s.Out), s.Calls), th.mut)
	// The todo list stays in the sidebar: the latest todo tool result is the
	// complete checklist.
	label("todo")
	if len(m.todos) == 0 {
		add("none", th.mut)
	} else {
		count := map[string]int{}
		for _, t := range m.todos {
			count[t.Status]++
		}
		parts := []string{}
		for _, st := range []string{"pending", "in_progress", "completed", "cancelled"} {
			if count[st] > 0 {
				parts = append(parts, fmt.Sprintf("%d %s", count[st], st))
			}
		}
		add(strings.Join(parts, " · "), th.mut)
		shown := 0
		for _, t := range m.todos {
			if len(body) >= h-3 {
				break
			}
			glyph, style := "·", th.mut
			switch t.Status {
			case "completed":
				glyph, style = "✓", th.okS
			case "in_progress":
				glyph, style = "▶", th.acc
			case "cancelled":
				glyph, style = "✗", th.errS
			}
			add(glyph+" "+squash(t.Content), style)
			shown++
		}
		if shown < len(m.todos) {
			add(fmt.Sprintf("+%d more", len(m.todos)-shown), th.mut)
		}
	}
	if s.Bg > 0 {
		label("background")
		add(fmt.Sprintf("%d processes", s.Bg), th.mut)
	}
	elapsed := "session"
	if !s.Created.IsZero() {
		elapsed += " " + fmtDuration(now.Sub(s.Created))
	}
	add(elapsed, th.mut)

	footer := th.okS.Render("•") + " " + th.bld.Render("Son of Anton")
	if v := m.o.Version; v != "" {
		footer += " " + th.mut.Render(ellipsize(v, max(inner-14, 1)))
	}
	rows := make([]string, h)
	border := th.acc.Render("│")
	for i := range rows {
		var content string
		switch {
		case i == h-2:
			content = footer
		case i >= 1 && i-1 < len(body) && i < h-2:
			content = body[i-1]
		}
		rows[i] = th.paint(border+"  "+content, th.panel, sidebarWidth)
	}
	return rows
}

// suggestRow is the draft's row with the history suggestion after the
// cursor: the cursor sits on the suggestion's first character. "" when it
// does not fit on the row.
func (m *model) suggestRow(sg string, w int) string {
	v := m.ta.Value()
	sg = cut(sg, max(w-textWidth(v), 0))
	r := []rune(sg)
	if len(r) == 0 {
		return ""
	}
	cursor := m.th.r.NewStyle().Reverse(true).Render(string(r[0]))
	return v + cursor + m.th.mut.Render(string(r[1:]))
}

// paneRows draws the feed of the pane in place: exactly rows rows, each with
// its margin, a short feed starting at the top. It remembers what each row
// shows, for the mouse.
func (m *model) paneRows(cw, rows int, now time.Time) []string {
	th := m.th
	total := m.feed.count(cw, th, now, m.spin)
	top := max(total-rows, 0)
	if !m.follow {
		top = min(max(m.top, 0), top)
	}
	feedLines, refs := m.feed.window(cw, th, now, m.spin, top, rows)
	m.lay = feedLayout{top: top, rows: rows, refs: refs, cw: cw}
	lines := make([]string, 0, rows)
	for i, l := range feedLines {
		lines = append(lines, m.selected(margin+l, top+i))
	}
	for i := len(feedLines); i < rows; i++ {
		lines = append(lines, "")
	}
	return lines
}

// overlay draws the open modal or picker over the whole screen: the screen is
// dimmed to the backdrop colour, and the dialog sits on an opaque card, as in
// Textual's ModalScreen. A terminal cannot blend, so the dim is one colour.
func (m *model) overlay(lines []string, cw int) {
	var box []string
	switch {
	case len(m.modals) > 0:
		box = m.modals[0].view(cw, m.th)
	case m.picker != nil:
		box = m.picker.view(cw, m.h, m.th)
	}
	if box == nil {
		return
	}
	bw := 0
	for _, l := range box {
		bw = max(bw, lipgloss.Width(l))
	}
	screen := m.w
	for i := range lines {
		lines[i] = m.th.paint(m.th.mut.Render(stripANSI(lines[i])), m.th.backdrop, screen)
	}
	start := max((len(lines)-len(box))/2, 0)
	cardW := min(bw, screen)
	x := max((screen-cardW)/2, 0)
	right := max(screen-x-cardW, 0)
	for i, l := range box {
		if start+i < len(lines) {
			lines[start+i] = m.th.paint(strings.Repeat(" ", x), m.th.backdrop, x) +
				m.th.paint(ansi.Truncate(l, cardW, ""), m.th.card, cardW) +
				m.th.paint(strings.Repeat(" ", right), m.th.backdrop, right)
		}
	}
}
