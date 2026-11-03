package tui

import (
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

const (
	codeNoSession = -32011
	codeNotDriver = -32010 // a physics session: only its run drives it
)

var modes = []string{"default", "ask", "lockdown", "yolo"}

// nextMode is the mode shift+tab switches to; an unknown mode goes to default.
func nextMode(cur string) string {
	for i, m := range modes {
		if m == cur {
			return modes[(i+1)%len(modes)]
		}
	}
	return modes[0]
}

func (m *model) key(k tea.KeyMsg) tea.Cmd {
	if m.quitting {
		return nil
	}
	s := k.String()
	m.sel, m.press = nil, nil
	if len(m.modals) > 0 && s != "ctrl+c" && s != "ctrl+q" {
		return m.modalKey(k)
	}
	if m.picker != nil && s != "ctrl+c" && s != "ctrl+q" && s != "ctrl+p" {
		return m.pickerKey(k)
	}
	leader := !m.leaderAt.IsZero() && time.Since(m.leaderAt) < leaderWindow
	m.leaderAt = time.Time{}
	if s == "ctrl+x" {
		m.leaderAt = time.Now()
		return nil
	}
	if leader && s == "down" && m.view == nil {
		return m.openNewestChild()
	}
	if m.view != nil {
		return m.childKey(k)
	}
	if m.comp != nil {
		if cmd, ok := m.compKey(s); ok {
			return cmd
		}
	}
	switch s {
	case "ctrl+c":
		if m.picker != nil {
			m.picker = nil
			return nil
		}
		return m.ctrlC()
	case "ctrl+p":
		if m.picker == nil {
			m.palettePicker()
		} else if m.picker.palette {
			m.picker = nil
		}
		return nil
	case "ctrl+g":
		return m.editor()
	case "tab", "right", "end":
		// The grey history suggestion is taken by any of them; without one,
		// Tab completes and → and End move the cursor.
		if sg := m.suggestion(); sg != "" {
			m.ta.SetValue(m.ta.Value() + sg)
			return m.refreshComp()
		}
		if s == "tab" {
			return m.tab()
		}
	case "ctrl+q":
		return m.quit(false)
	case "esc":
		if m.busy() {
			m.feed.note("interrupting…", true)
			return m.interrupt()
		}
		return nil
	case "shift+tab":
		return m.setMode(nextMode(m.sess.Mode))
	case "ctrl+b":
		show, _ := m.sidebarState()
		show = !show
		m.sbForced = &show
		return nil
	case "ctrl+l":
		m.feed.clearKeepIntro()
		m.curAsst, m.curReason = nil, nil
		m.follow = true
		return nil
	case "pgup":
		m.scroll(-1)
		return nil
	case "pgdown":
		m.scroll(1)
		return nil
	case "enter":
		return m.submit()
	case "alt+enter", "ctrl+j":
		return m.edit(tea.KeyMsg{Type: tea.KeyEnter})
	case "up":
		if m.onFirstRow() {
			m.recall(-1)
			return nil
		}
	case "down":
		if m.onLastRow() {
			m.recall(1)
			return nil
		}
	}
	return m.edit(k)
}

// edit hands a key to the text area.
func (m *model) edit(k tea.KeyMsg) tea.Cmd {
	m.ta.SetHeight(m.taCap()) // so a key never scrolls a draft that will fit
	var cmd tea.Cmd
	m.ta, cmd = m.ta.Update(k)
	return tea.Batch(cmd, m.refreshComp())
}

func (m *model) onFirstRow() bool {
	return m.ta.Line() == 0 && m.ta.LineInfo().RowOffset == 0
}

func (m *model) onLastRow() bool {
	li := m.ta.LineInfo()
	return m.ta.Line() == m.ta.LineCount()-1 && li.RowOffset == li.Height-1
}

// recall moves through the history: -1 older, 1 newer, ending at the draft.
func (m *model) recall(dir int) {
	next := m.histPos + dir
	if next < 0 || next > len(m.hist) {
		return
	}
	if m.histPos == len(m.hist) {
		m.draft = m.ta.Value()
	}
	m.histPos = next
	m.ta.SetHeight(m.taCap())
	if next == len(m.hist) {
		m.ta.SetValue(m.draft)
	} else {
		m.ta.SetValue(m.hist[next])
	}
}

// record puts a sent text in the history, in memory and in the shared file.
func (m *model) record(text string) {
	text = strings.Trim(text, "\n")
	if strings.TrimSpace(text) == "" {
		return
	}
	m.histPos = len(m.hist)
	if n := len(m.hist); n > 0 && m.hist[n-1] == text {
		return
	}
	m.hist = append(m.hist, text)
	m.histPos = len(m.hist)
	_ = AppendHistory(m.histPath, text) // unwritable: history stays in memory
}

func (m *model) ctrlC() tea.Cmd {
	if m.busy() {
		if time.Since(m.lastCtrlC) < 2*time.Second {
			return m.quit(true)
		}
		m.lastCtrlC = time.Now()
		m.feed.note("interrupting… (ctrl+c again to force quit)", true)
		return m.interrupt()
	}
	if m.ta.Value() != "" {
		m.ta.Reset()
		m.histPos = len(m.hist)
		m.resetPrompt()
		return nil
	}
	return m.quit(false)
}

// scroll pages the feed: dir -1 up (stops following), 1 down (follows again at the bottom).
func (m *model) scroll(dir int) {
	_, rows := m.feedRows()
	m.scrollBy(dir * max(rows-1, 1))
}

// quit detaches and closes the connection, leaving any turn running; with
// force it interrupts the turn first.
func (m *model) quit(interrupt bool) tea.Cmd {
	m.quitting = true
	c, sid := m.cl, m.sess.ID
	return func() tea.Msg {
		if interrupt {
			ctx, cancel := ctxTimeout(2 * time.Second)
			_ = c.Call(ctx, "turn.interrupt", map[string]any{"session": sid}, nil)
			cancel()
		}
		ctx, cancel := ctxTimeout(3 * time.Second)
		_ = c.Call(ctx, "session.detach", map[string]any{"session": sid}, nil)
		cancel()
		_ = c.Close()
		return quitMsg{}
	}
}
