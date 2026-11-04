package tui

// Phase 9d: task rows, the "view subagents" hint and the child view
// (daemon/phases/09d-tui-subagents-mouse.md).

import (
	"context"
	"fmt"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	"github.com/ewtodd/son-of-anton/daemon/internal/client"
)

const leaderWindow = 2 * time.Second

// pane is what one transcript view owns: a feed and where it is. The model
// holds the main view's in its own fields; a child's lives in its childView
// and is swapped in to run the same code on it.
type pane struct {
	feed               *feed
	curAsst, curReason *block
	follow             bool
	top                int
	lastSeq            int64
	sess               sessState
}

// swap exchanges the model's pane fields with p's; calling it twice puts
// everything back. While a child's pane is in, inPane is true.
func (m *model) swap(p *pane) {
	m.feed, p.feed = p.feed, m.feed
	m.curAsst, p.curAsst = p.curAsst, m.curAsst
	m.curReason, p.curReason = p.curReason, m.curReason
	m.follow, p.follow = p.follow, m.follow
	m.top, p.top = p.top, m.top
	m.lastSeq, p.lastSeq = p.lastSeq, m.lastSeq
	m.sess, p.sess = p.sess, m.sess
	m.inPane = !m.inPane
}

// childView is the child on show: its pane, filled once its attach returns.
type childView struct {
	id     string
	loaded bool
	p      pane
}

// inChild runs fn with the child's pane in place of the main one.
func (m *model) inChild(fn func() tea.Cmd) tea.Cmd {
	v := m.view
	if v == nil {
		return nil
	}
	m.swap(&v.p)
	defer m.swap(&v.p)
	return fn()
}

// onChildEvent draws an event of the child on show through the main feed's
// handlers. Prompts are the parent's: a child asks through it.
func (m *model) onChildEvent(ev client.Event) tea.Cmd {
	switch ev.Kind {
	case "queue.updated", "role", "approval.request", "approval.resolved", "question.request", "question.resolved",
		"child.created", "child.progress", "child.done":
		return nil
	}
	return m.inChild(func() tea.Cmd { return m.onEvent(ev) })
}

func messageCallsTask(msg map[string]any) bool {
	calls, _ := msg["tool_calls"].([]any)
	for _, c := range calls {
		if o, ok := c.(map[string]any); ok && str(o, "name") == "task" {
			return true
		}
	}
	return false
}

// ---- the children ----

// onChild handles the parent's child.* events.
func (m *model) onChild(kind string, p map[string]any) {
	switch kind {
	case "child.created":
		o, _ := p["child"].(map[string]any)
		k := parseKid(o)
		if k.ID == "" {
			return
		}
		m.taskRows[k.ID] = str(p, "call_id")
		if m.kidIndex(k.ID) < 0 {
			m.kids = append(m.kids, k)
		}
		m.running[k.ID] = true
	case "child.progress":
		if call := m.taskRows[str(p, "child_id")]; call != "" {
			m.setDetail(call, strings.TrimRight("↳ "+str(p, "name")+" "+str(p, "summary"), " "))
		}
	case "child.done":
		id := str(p, "child_id")
		delete(m.running, id)
		if i := m.kidIndex(id); i >= 0 {
			m.kids[i].Status = str(p, "status")
		}
		if call := m.taskRows[id]; call != "" {
			detail := ""
			if str(p, "status") == "done" {
				detail = doneDetail(num(p, "tool_calls"), num(p, "duration_ms"))
			}
			m.setDetail(call, detail)
		}
	}
}

func (m *model) setDetail(callID, detail string) {
	if b := m.feed.calls[callID]; b != nil && b.detail != detail {
		b.detail = detail
		b.ver++
	}
}

func (m *model) kidIndex(id string) int {
	for i, k := range m.kids {
		if k.ID == id {
			return i
		}
	}
	return -1
}

// kidTitle is "<Agent> Task — <description>" for a child of this session.
func (m *model) kidTitle(id string) string {
	if i := m.kidIndex(id); i >= 0 {
		return m.kids[i].taskTitle()
	}
	return "Subagent"
}

func (m *model) setKids(list []map[string]any) {
	m.kids = m.kids[:0:0]
	m.running = map[string]bool{}
	for _, o := range list {
		k := parseKid(o)
		m.kids = append(m.kids, k)
		if k.Status == "running" {
			m.running[k.ID] = true
		}
	}
}

type childrenReply struct {
	Children []map[string]any `json:"children"`
}

// loadKids fetches the children of an attached session when its stored rows
// call task (one session.children call), for fillTasks to use.
func loadKids(ctx context.Context, c *client.Client, sid string, res *attachResult) {
	calls := false
	for _, msg := range res.Messages {
		calls = calls || callsTask(msg.ToolCalls)
	}
	if !calls {
		return
	}
	var kr childrenReply
	if c.Call(ctx, "session.children", map[string]any{"session": sid}, &kr) == nil {
		res.Kids, res.HaveKids = kr.Children, true
	}
}

// fillTasks maps the history's task rows to their children and writes what
// each did.
func (m *model) fillTasks(rs []taskRes, res attachResult) {
	if !res.HaveKids {
		return
	}
	m.setKids(res.Kids)
	for _, r := range rs {
		m.taskRows[r.childID] = r.callID
		if i := m.kidIndex(r.childID); i >= 0 && (r.state == "completed" || r.state == "running") && m.kids[i].Status != "running" {
			m.setDetail(r.callID, doneDetail(m.kids[i].ToolCalls, m.kids[i].DurationMS))
		}
	}
}

// ---- the child view ----

// openNewestChild is ctrl+x down: the newest child of the session, if any.
func (m *model) openNewestChild() tea.Cmd {
	c, sid := m.cl, m.sess.ID
	var res childrenReply
	return call(c, 10*time.Second, "session.children", map[string]any{"session": sid}, &res, func(m *model, err error) tea.Cmd {
		if err != nil || m.cl != c || m.sess.ID != sid || m.view != nil || len(res.Children) == 0 {
			return nil
		}
		m.setKids(res.Children)
		return m.showChild(m.kids[len(m.kids)-1].ID)
	})
}

// showChild puts the child on show: it is attached as a viewer on this
// connection, beside the driven parent.
func (m *model) showChild(id string) tea.Cmd {
	old := ""
	if m.view != nil {
		old = m.view.id
	}
	m.view = &childView{id: id, p: pane{feed: newFeed(), follow: true}}
	m.sel, m.press = nil, nil
	c := m.cl
	return func() tea.Msg {
		ctx, cancel := ctxTimeout(30 * time.Second)
		defer cancel()
		if old != "" && old != id {
			_ = c.Call(ctx, "session.detach", map[string]any{"session": old}, nil)
		}
		var res attachResult
		err := c.Call(ctx, "session.attach", map[string]any{"session": id, "as": "viewer"}, &res)
		return applyMsg{func(m *model) tea.Cmd {
			if m.cl != c {
				return nil
			}
			if m.view == nil || m.view.id != id {
				if err == nil { // released while the call was in flight
					return detachCmd(c, id)
				}
				return nil
			}
			if err != nil {
				m.view = nil
				m.feed.note("could not open the subagent: "+err.Error(), false)
				return nil
			}
			if m.view.loaded {
				return nil
			}
			return m.loadChild(res)
		}}
	}
}

func detachCmd(c *client.Client, id string) tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := ctxTimeout(3 * time.Second)
		defer cancel()
		_ = c.Call(ctx, "session.detach", map[string]any{"session": id}, nil)
		return nil
	}
}

// loadChild draws the attached child's history, then the events it sent
// since.
func (m *model) loadChild(res attachResult) tea.Cmd {
	v := m.view
	v.p = pane{feed: newFeed(), follow: true, sess: parseSession(res.Session, sessState{})}
	m.swap(&v.p)
	m.drawHistory(res, false)
	m.swap(&v.p)
	v.loaded = true
	held := m.held
	m.held = nil
	var cmds []tea.Cmd
	for _, ev := range held {
		if ev.Session == v.id {
			cmds = append(cmds, m.onChildEvent(ev))
		}
	}
	cmds = append(cmds, m.startTick())
	return tea.Batch(cmds...)
}

// dropView takes the child off show and detaches it.
func (m *model) dropView() tea.Cmd {
	v := m.view
	if v == nil {
		return nil
	}
	m.view, m.sel, m.press = nil, nil, nil
	m.held = nil
	return detachCmd(m.cl, v.id)
}

// stepChild shows the next (dir 1) or previous sibling, wrapping.
func (m *model) stepChild(dir int) tea.Cmd {
	n := len(m.kids)
	i := m.kidIndex(m.view.id)
	if i < 0 || n < 2 {
		return nil
	}
	m.held = nil
	return m.showChild(m.kids[(i+dir+n)%n].ID)
}

// childKey handles a key on the child view (no modal open).
func (m *model) childKey(k tea.KeyMsg) tea.Cmd {
	switch k.String() {
	case "up":
		return m.dropView()
	case "right":
		return m.stepChild(1)
	case "left":
		return m.stepChild(-1)
	case "ctrl+c":
		return m.ctrlC()
	case "ctrl+q":
		return m.quit(false)
	case "shift+tab":
		return m.setMode(nextMode(m.sess.Mode))
	case "pgup":
		return m.inChild(func() tea.Cmd { m.scroll(-1); return nil })
	case "pgdown":
		return m.inChild(func() tea.Cmd { m.scroll(1); return nil })
	}
	return nil
}

// viewBusy reports whether the child on show is running a turn.
func (m *model) viewBusy() bool {
	return m.view != nil && m.view.p.sess.Status == "running"
}

// ---- drawing the child view ----

const childFooterRows = 4

// childFooter is opencode's SubagentFooter: a pad, the row, a pad on the
// panel colour with the accent rail, then a blank row.
func (m *model) childFooter(cw int) []string {
	th, v := m.th, m.view
	s := v.p.sess
	i := m.kidIndex(v.id)
	agent := s.Agent
	if agent == "" && i >= 0 {
		agent = m.kids[i].Agent
	}
	label := "Subagent"
	if agent != "" {
		label = titlecase(agent)
	}
	left := []seg{{label, th.bld}}
	if i >= 0 {
		left = append(left, seg{fmt.Sprintf(" (%d of %d)", i+1, len(m.kids)), th.mut})
	}
	if s.CtxTokens > 0 {
		t := " " + fmtTokens(s.CtxTokens)
		if s.CtxLen > 0 {
			t += fmt.Sprintf(" (%d%%)", (100*s.CtxTokens+s.CtxLen/2)/s.CtxLen)
		}
		left = append(left, seg{t, th.mut})
	}
	right := []seg{
		{"Parent ", th.plain}, {"↑", th.mut}, {"   Prev ", th.plain}, {"←", th.mut}, {"   Next ", th.plain}, {"→", th.mut},
	}
	inner := max(cw-2, 1) // the rail and a space
	rw := segsWidth(right)
	row := renderSegs(left, max(inner-rw-2, 0))
	gap := max(inner-lipgloss.Width(row)-rw, 0)
	rail := th.acc.Render("▎")
	pad := th.paint(rail, th.panel, cw)
	line := th.paint(rail+" "+row+strings.Repeat(" ", gap)+renderSegs(right, rw), th.panel, cw)
	return []string{margin + pad, margin + line, margin + pad, ""}
}

// renderChild draws the child view: exactly m.h rows.
func (m *model) renderChild() []string {
	h := m.h
	cw := max(m.w-4, 1)
	rows := max(h-childFooterRows, 0)
	now := time.Now()
	var lines []string
	m.inChild(func() tea.Cmd { lines = m.paneRows(cw, rows, now); return nil })
	lines = append(lines, m.childFooter(cw)...)
	switch {
	case len(lines) > h:
		lines = lines[len(lines)-h:]
	case len(lines) < h:
		lines = append(make([]string, h-len(lines)), lines...)
	}
	m.overlay(lines, cw)
	for i, l := range lines {
		lines[i] = m.th.fix(l)
	}
	return lines
}
