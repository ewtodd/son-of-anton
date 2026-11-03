package tui

import (
	"fmt"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
)

const margin = "  "

func (m *model) contentWidth() int { return max(m.w-4, 1) }

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

var plain = lipgloss.NewStyle()

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
	dock = 8 + m.taRows() + m.queueRows()
	return dock, max(m.h-dock, 0)
}

func (m *model) statusLine(cw int, now time.Time) string {
	var left, right []seg
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
		left = []seg{{spinnerFrame(m.spin), bold}, {" ", plain}, {label, plain}, {fmt.Sprintf("  %ds", secs), muted}}
		right = []seg{{"ctrl+c", bold}, {" interrupt", plain}}
	} else {
		left = []seg{{tilde(m.sess.Cwd), plain}}
		if m.sess.CtxLen > 0 {
			pct := (100*m.sess.CtxTokens + m.sess.CtxLen/2) / m.sess.CtxLen
			right = append(right, seg{fmt.Sprintf("%d%% context  ·  ", pct), muted})
		}
		right = append(right, seg{"/help", bold}, seg{" commands", plain})
	}
	var suffix []seg
	if n := len(m.queue); n > 0 {
		suffix = []seg{{fmt.Sprintf("  · %d queued", n), muted}}
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
	mode := plain
	switch m.sess.Mode {
	case "yolo":
		mode = errSt.Bold(true)
	case "lockdown":
		mode = toolSt
	}
	effort := m.sess.Effort
	if effort == "" {
		effort = "default"
	}
	sep := seg{"  ·  ", muted}
	return renderSegs([]seg{
		{m.sess.Mode, mode}, sep,
		{modelShort(m.sess.Model, m.sess.Served), bold}, sep,
		{"reasoning " + effort, muted},
	}, w)
}

func (m *model) queueStrip(cw int) []string {
	if len(m.queue) == 0 {
		return nil
	}
	var out []string
	for _, q := range m.queue[:min(len(m.queue), 3)] {
		out = append(out, muted.Render("↳ ")+ellipsize(squash(q), max(cw-5, 1)))
	}
	if n := len(m.queue) - 3; n > 0 {
		out = append(out, muted.Render(fmt.Sprintf("   +%d more", n)))
	}
	hint := "/sendnow sends this now"
	if len(m.queue) > 1 {
		hint = "/sendnow sends the oldest · /sendall sends all"
	}
	out = append(out, muted.Render(ellipsize(hint, cw)))
	for i := range out {
		out[i] = "   " + out[i]
	}
	return out
}

// render draws the whole screen: exactly m.h rows.
func (m *model) render() []string {
	w, h := m.w, m.h
	cw := m.contentWidth()
	now := time.Now()
	ta := m.taRows()
	m.ta.SetWidth(max(cw-3, 1))
	m.ta.SetHeight(ta)
	m.ta, _ = m.ta.Update(nil) // keep the cursor in view

	rail := accent.Render("▎")
	var dock []string
	if q := m.queueStrip(cw); q != nil {
		dock = append(dock, "")
		dock = append(dock, q...)
	}
	dock = append(dock, "", rail)
	for _, l := range strings.Split(m.ta.View(), "\n") {
		dock = append(dock, rail+"  "+l)
	}
	dock = append(dock, rail, rail+"  "+m.metaLine(max(cw-3, 0)), rail, "", m.statusLine(cw, now), "")
	for i, l := range dock {
		if l != "" {
			dock[i] = margin + l
		}
	}
	_, rows := m.feedRows()
	rows = min(rows, max(h-len(dock), 0))
	total := m.feed.count(cw, now, m.spin)
	top := max(total-rows, 0)
	if !m.follow {
		top = min(max(m.top, 0), top)
	}
	feedLines := m.feed.window(cw, now, m.spin, top, rows)
	lines := make([]string, 0, h)
	for i := len(feedLines); i < rows; i++ {
		lines = append(lines, "")
	}
	for _, l := range feedLines {
		lines = append(lines, margin+l)
	}
	lines = append(lines, dock...)

	if len(m.modals) > 0 {
		box := m.modals[0].view(cw)
		start := max((rows-len(box))/2, 0)
		bw := 0
		for _, l := range box {
			bw = max(bw, lipgloss.Width(l))
		}
		for i, l := range box {
			if start+i < len(lines) {
				lines[start+i] = margin + strings.Repeat(" ", max((cw-bw)/2, 0)) + l
			}
		}
	}
	switch {
	case len(lines) > h:
		lines = lines[len(lines)-h:]
	case len(lines) < h:
		lines = append(make([]string, h-len(lines)), lines...)
	}
	_ = w
	return lines
}

func (m *model) View() string { return strings.Join(m.render(), "\n") }
