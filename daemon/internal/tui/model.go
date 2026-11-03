package tui

import (
	"context"
	"errors"
	"os"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/cursor"
	"github.com/charmbracelet/bubbles/textarea"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	"github.com/ewtodd/son-of-anton/daemon/internal/client"
)

const (
	drawEvery   = 120 * time.Millisecond
	maxTextRows = 10
	minFeedRows = 3
	maxHeld     = 5000
)

// ---- messages the program receives besides keys ----

type (
	evMsg struct {
		ev client.Event
		c  *client.Client
	}
	lostMsg struct{ c *client.Client }
	// queryMsg runs fn on the program's goroutine and answers with its result.
	queryMsg struct {
		fn    func(*model) string
		reply chan string
	}
	// applyMsg is the result of a background call: fn runs on the program's goroutine.
	applyMsg       struct{ fn func(*model) tea.Cmd }
	tickMsg        struct{}
	drawMsg        struct{}
	reconnectMsg   struct{}
	reconnectedMsg struct {
		c   *client.Client
		res attachResult
		err error
	}
	quitMsg struct{}
)

type model struct {
	app  *App
	o    Options
	cl   *client.Client
	w, h int

	sess  sessState
	queue []string
	role  string

	feed       *feed
	curAsst    *block
	curReason  *block
	follow     bool
	top        int
	lastSeq    int64
	lastDraw   time.Time
	drawQueued bool

	ta       textarea.Model
	hist     []string
	histPos  int
	draft    string
	histPath string

	modals []*modal
	// held are events of other sessions: the one being switched to sends its
	// live tail right after the attach reply, before the switch is applied.
	held []client.Event

	spin       int
	ticking    bool
	lastCtrlC  time.Time
	quitting   bool
	reconnect  bool
	retryDelay time.Duration
	giveUp     bool
}

func newModel(a *App, c *client.Client, o Options, res attachResult, resumed bool) *model {
	ta := textarea.New()
	ta.Prompt = ""
	ta.ShowLineNumbers = false
	ta.Placeholder = "Message Son of Anton…"
	ta.CharLimit, ta.MaxHeight, ta.MaxWidth = 0, 0, 0
	for _, st := range []*textarea.Style{&ta.FocusedStyle, &ta.BlurredStyle} {
		st.CursorLine = lipgloss.NewStyle()
		st.Text = lipgloss.NewStyle()
		st.Placeholder = muted
		st.EndOfBuffer = lipgloss.NewStyle()
	}
	ta.Cursor.SetMode(cursor.CursorStatic)
	ta.Focus()
	m := &model{
		app: a, o: o, cl: c, w: 100, h: 30, feed: newFeed(), follow: true, ta: ta,
		histPath: o.Home + "/.son_of_anton_history",
	}
	m.hist = ReadHistory(m.histPath)
	m.histPos = len(m.hist)
	m.adopt(res, resumed)
	return m
}

func (m *model) Init() tea.Cmd { return m.afterAttach() }

// adopt makes the attached session the current one and redraws the feed,
// then plays the events it sent meanwhile.
func (m *model) adopt(res attachResult, resumed bool) tea.Cmd {
	m.sess = parseSession(res.Session, sessState{})
	m.role = res.Role
	m.queue = queueTexts(res.Queue)
	m.modals = nil
	m.curAsst, m.curReason = nil, nil
	m.lastSeq = 0
	m.follow = true
	m.feed = newFeed()
	m.addIntro()
	m.drawHistory(res, resumed)
	held := m.held
	m.held = nil
	var cmds []tea.Cmd
	for _, ev := range held {
		if ev.Session == m.sess.ID {
			cmds = append(cmds, m.onEvent(ev))
		}
	}
	return tea.Batch(cmds...)
}

// afterAttach starts what a running session needs: the spinner.
func (m *model) afterAttach() tea.Cmd { return m.startTick() }

func queueTexts(items []queueItem) []string {
	out := make([]string, len(items))
	for i, it := range items {
		out[i] = it.Text
	}
	return out
}

func (m *model) busy() bool { return m.sess.Status == "running" }

func (m *model) addIntro() {
	b := &block{kind: bkIntro}
	b.notes = []noteLine{
		{"v" + m.o.Version + "  ·  " + modelShort(m.sess.Model, m.sess.Served) + "  ·  session " + m.sess.ID + "  ·  " + tilde(m.sess.Cwd), true},
		{"/help for commands · :q to quit", true},
	}
	m.feed.add(b)
}

func tilde(p string) string {
	if h, err := os.UserHomeDir(); err == nil && h != "" && h != "/" {
		if p == h {
			return "~"
		}
		if strings.HasPrefix(p, h+"/") {
			return "~" + p[len(h):]
		}
	}
	return p
}

// ---- history ----

const (
	interruptedText = "[response interrupted]"
	failedText      = "[response failed]"
)

func (m *model) drawHistory(res attachResult, resumed bool) {
	if len(res.Messages) == 0 {
		return
	}
	if resumed {
		note := "resumed session " + m.sess.ID
		if res.MoreBefore {
			note += " · earlier messages not shown"
		}
		m.feed.note(note, true)
	}
	after := map[int64]string{}
	for _, c := range res.Compactions {
		after[c.ThroughSeq] = c.Summary
	}
	for _, msg := range res.Messages {
		m.lastSeq = max(m.lastSeq, msg.Seq)
		content := strings.TrimSpace(msg.Content)
		switch msg.Role {
		case "user":
			if strings.HasPrefix(content, "[Background process") {
				m.feed.note(firstLine(content, 200), true)
			} else if content != "" {
				m.feed.add(&block{kind: bkUser, text: content})
			}
		case "assistant":
			if content == interruptedText || content == failedText {
				m.feed.note(content, true)
				break
			}
			if strings.TrimSpace(msg.Reasoning) != "" {
				m.feed.add(&block{kind: bkReasoning, text: strings.TrimSpace(msg.Reasoning), shown: strings.TrimSpace(msg.Reasoning), folded: true})
			}
			if content != "" {
				m.feed.add(&block{kind: bkAssistant, text: content, shown: content})
			}
			for _, c := range msg.ToolCalls {
				m.feed.add(&block{kind: bkTool, callID: c.ID, name: c.Name, label: toolSummary(c.Name, c.Arguments)})
			}
		case "tool":
			if b := m.feed.calls[msg.ToolCallID]; b != nil && resultIsError(msg.Content) {
				b.isErr = true
				b.ver++
			}
		}
		if summary, ok := after[msg.Seq]; ok {
			m.addCompaction(summary, 0)
		}
	}
}

func (m *model) addCompaction(summary string, before int) {
	m.feed.note("── Compaction ──", true)
	m.feed.add(&block{kind: bkAssistant, text: summary, shown: summary})
	if before > 0 {
		m.feed.note("~"+commas(before)+" tokens before", true)
	}
}

// ---- Update ----

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case queryMsg:
		msg.reply <- msg.fn(m)
	case tea.WindowSizeMsg:
		if msg.Width > 0 && msg.Height > 0 {
			m.w, m.h = msg.Width, msg.Height
		}
	case tea.KeyMsg:
		return m, m.key(msg)
	case evMsg:
		if msg.c == m.cl && !m.quitting {
			if msg.ev.Session != m.sess.ID {
				if m.held = append(m.held, msg.ev); len(m.held) > maxHeld {
					m.held = m.held[len(m.held)-maxHeld:]
				}
				return m, nil
			}
			return m, m.onEvent(msg.ev)
		}
	case lostMsg:
		if msg.c == m.cl && !m.quitting {
			return m, m.onLost()
		}
	case applyMsg:
		if !m.quitting {
			return m, msg.fn(m)
		}
	case tickMsg:
		m.ticking = false
		return m, m.startTick()
	case drawMsg:
		m.drawQueued = false
		m.flush()
	case reconnectMsg:
		if !m.quitting && m.reconnect {
			return m, m.tryReconnect()
		}
	case reconnectedMsg:
		return m, m.onReconnected(msg)
	case quitMsg:
		return m, tea.Quit
	}
	return m, nil
}

// startTick runs the spinner while a turn runs; nothing ticks while idle.
func (m *model) startTick() tea.Cmd {
	if !m.busy() || m.ticking {
		return nil
	}
	m.ticking = true
	m.spin++
	return tea.Tick(drawEvery, func(time.Time) tea.Msg { return tickMsg{} })
}

// flush draws the streamed text that has arrived.
func (m *model) flush() {
	for _, b := range []*block{m.curAsst, m.curReason} {
		if b != nil && b.shown != b.text {
			b.shown = b.text
			b.ver++
		}
	}
	m.lastDraw = time.Now()
}

// delta appends streamed text, drawing at most every 120ms.
func (m *model) delta(b *block, text string) tea.Cmd {
	b.text += clean(text)
	if since := time.Since(m.lastDraw); since >= drawEvery || b.shown == "" {
		m.flush()
		return nil
	} else if !m.drawQueued {
		m.drawQueued = true
		return tea.Tick(drawEvery-since, func(time.Time) tea.Msg { return drawMsg{} })
	}
	return nil
}

func (m *model) foldReasoning() {
	if b := m.curReason; b != nil {
		b.shown = b.text
		b.folded = true
		b.ver++
		m.curReason = nil
	}
}

// ---- events ----

func (m *model) onEvent(ev client.Event) tea.Cmd {
	if ev.Session != m.sess.ID {
		return nil
	}
	p := ev.Params
	switch ev.Kind {
	case "turn.start":
		m.flush()
		m.foldReasoning()
		m.curAsst = nil
		text := strings.TrimSpace(str(p, "text"))
		if strings.HasPrefix(text, "[Background process") {
			m.feed.note(firstLine(text, 200), true)
		} else {
			m.feed.add(&block{kind: bkUser, text: clean(text)})
		}
		if m.sess.Status != "running" {
			m.sess.Status, m.sess.Started = "running", time.Now()
		}
		return m.startTick()
	case "reasoning.delta":
		if m.curReason == nil {
			m.curAsst = nil
			m.curReason = m.feed.add(&block{kind: bkReasoning})
		}
		return m.delta(m.curReason, str(p, "text"))
	case "assistant.delta":
		m.foldReasoning()
		if m.curAsst == nil {
			m.curAsst = m.feed.add(&block{kind: bkAssistant})
		}
		return m.delta(m.curAsst, str(p, "text"))
	case "tool.start":
		m.flush()
		m.foldReasoning()
		m.curAsst = nil
		id := str(p, "call_id")
		if b := m.feed.calls[id]; b != nil && b.running {
			return nil
		}
		m.feed.add(&block{kind: bkTool, callID: id, name: str(p, "name"), label: squash(clean(str(p, "summary"))),
			running: true, started: time.Now()})
	case "tool.end":
		id := str(p, "call_id")
		b := m.feed.calls[id]
		if b == nil {
			b = m.feed.add(&block{kind: bkTool, callID: id, name: str(p, "name"), label: str(p, "name")})
		}
		b.running = false
		b.isErr, _ = p["is_error"].(bool)
		b.took = time.Duration(num(p, "duration_ms")) * time.Millisecond
		b.ver++
	case "compaction":
		m.flush()
		m.foldReasoning()
		m.curAsst = nil
		m.addCompaction(clean(str(p, "summary")), num(p, "before_tokens"))
	case "message":
		if msg, ok := p["message"].(map[string]any); ok {
			m.lastSeq = max(m.lastSeq, int64(num(msg, "seq")))
		}
	case "turn.end":
		m.flush()
		m.foldReasoning()
		m.curAsst = nil
		m.finishTools()
		m.lastCtrlC = time.Time{}
		m.sess.Status = "idle"
		switch str(p, "status") {
		case "interrupted":
			m.feed.note("interrupted", true)
		case "error":
			e := str(p, "error")
			if e == "" {
				e = "the turn failed"
			}
			m.feed.note("error: "+e, false)
		}
	case "session.updated":
		if st, ok := p["state"].(map[string]any); ok {
			m.sess = parseSession(st, m.sess)
			return m.startTick()
		}
	case "queue.updated":
		var items []queueItem
		if raw, ok := p["items"].([]any); ok {
			for _, r := range raw {
				if o, ok := r.(map[string]any); ok {
					items = append(items, queueItem{ID: str(o, "id"), Kind: str(o, "kind"), Text: str(o, "text")})
				}
			}
		}
		m.queue = queueTexts(items)
	case "role":
		m.role = str(p, "as")
		if m.role == "viewer" {
			m.feed.note("another client is now the driver of this session", true)
		}
	case "notice":
		m.feed.note(str(p, "text"), str(p, "level") == "info")
	case "approval.request", "question.request":
		m.openModal(newModal(ev.Kind, p))
	case "approval.resolved":
		m.closeModal(str(p, "approval_id"))
	case "question.resolved":
		m.closeModal(str(p, "question_id"))
	}
	return nil
}

// ---- reconnecting ----

// finishTools ends the rows of tools still shown as running.
func (m *model) finishTools() {
	for _, b := range m.feed.blocks {
		if b.kind == bkTool && b.running {
			b.running = false
			b.ver++
		}
	}
}

func (m *model) onLost() tea.Cmd {
	m.modals = nil
	m.finishTools()
	m.sess.Status = "idle"
	m.flush()
	m.foldReasoning()
	m.curAsst = nil
	m.feed.note("lost the connection to soa-d; reconnecting…", true)
	m.reconnect = true
	m.retryDelay = 500 * time.Millisecond
	return tea.Tick(m.retryDelay, func(time.Time) tea.Msg { return reconnectMsg{} })
}

func (m *model) tryReconnect() tea.Cmd {
	home, autostart, sid, after := m.o.Home, m.o.Autostart, m.sess.ID, m.lastSeq
	return func() tea.Msg {
		var (
			c   *client.Client
			err error
		)
		if autostart {
			c, err = client.Connect(home, "tui")
		} else {
			c, err = client.Dial(home, "tui")
		}
		if err != nil {
			return reconnectedMsg{err: err}
		}
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		var res attachResult
		params := map[string]any{"session": sid, "as": "driver", "after_seq": after}
		if err := c.Call(ctx, "session.attach", params, &res); err != nil {
			_ = c.Close()
			return reconnectedMsg{err: err}
		}
		return reconnectedMsg{c: c, res: res}
	}
}

func (m *model) onReconnected(msg reconnectedMsg) tea.Cmd {
	if m.quitting {
		if msg.c != nil {
			_ = msg.c.Close()
		}
		return nil
	}
	if msg.err != nil {
		var rpc *client.Error
		if errors.As(msg.err, &rpc) && rpc.Code == codeNoSession {
			m.reconnect = false
			m.feed.note("session "+m.sess.ID+" is not in soa-d", true)
			return nil
		}
		m.retryDelay = min(m.retryDelay*2, 10*time.Second)
		return tea.Tick(m.retryDelay, func(time.Time) tea.Msg { return reconnectMsg{} })
	}
	old := m.cl
	m.cl, m.reconnect = msg.c, false
	_ = old.Close()
	go m.app.pump(msg.c)
	if msg.res.Gap {
		m.feed.clearKeepIntro()
		m.drawHistory(msg.res, false)
	} else {
		m.drawHistory(attachResult{Messages: msg.res.Messages, Compactions: msg.res.Compactions}, false)
	}
	m.sess = parseSession(msg.res.Session, m.sess)
	m.queue = queueTexts(msg.res.Queue)
	m.role = msg.res.Role
	m.feed.note("reconnected to soa-d", true)
	return m.startTick()
}
