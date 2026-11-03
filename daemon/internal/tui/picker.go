package tui

import (
	"fmt"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

const maxPickRows = 14

type pickRow struct {
	key, label, desc string
	def              bool // drawn bold, and where the highlight starts
}

// picker is a list the user chooses from: one row, or several.
type picker struct {
	title      string
	rows       []pickRow
	filterable bool
	multi      bool
	palette    bool

	filter string
	sel    int // into the visible rows
	top    int // first visible row shown
	picked map[string]bool

	// done acts on the keys chosen, after the picker has closed.
	done func(m *model, keys []string) tea.Cmd
}

func newPicker(title string, rows []pickRow, filterable, multi bool, done func(*model, []string) tea.Cmd) *picker {
	p := &picker{title: title, rows: rows, filterable: filterable, multi: multi, done: done, picked: map[string]bool{}}
	for i, r := range rows {
		if r.def {
			p.sel = i
			break
		}
	}
	return p
}

// visible are the indexes of the rows the filter leaves.
func (p *picker) visible() []int {
	f := strings.ToLower(p.filter)
	var out []int
	if p.palette && f != "" { // the command name first, the description when no name matches
		for i, r := range p.rows {
			if strings.Contains(strings.ToLower(r.key), f) {
				out = append(out, i)
			}
		}
		if len(out) > 0 {
			return out
		}
		for i, r := range p.rows {
			if strings.Contains(strings.ToLower(r.desc), f) {
				out = append(out, i)
			}
		}
		return out
	}
	for i, r := range p.rows {
		if f == "" || strings.Contains(strings.ToLower(r.label), f) || strings.Contains(strings.ToLower(r.key), f) {
			out = append(out, i)
		}
	}
	return out
}

// pick closes the picker and acts on keys.
func (m *model) pick(p *picker, keys []string) tea.Cmd {
	m.picker = nil
	return p.done(m, keys)
}

func (m *model) pickerKey(k tea.KeyMsg) tea.Cmd {
	p := m.picker
	vis := p.visible()
	s := k.String()
	switch s {
	case "esc":
		m.picker = nil
		return nil
	case "up":
		p.sel = max(p.sel-1, 0)
		return nil
	case "down":
		p.sel = min(p.sel+1, max(len(vis)-1, 0))
		return nil
	case "enter":
		if p.multi {
			var keys []string
			for _, r := range p.rows {
				if p.picked[r.key] {
					keys = append(keys, r.key)
				}
			}
			return m.pick(p, keys)
		}
		if p.sel < len(vis) {
			return m.pick(p, []string{p.rows[vis[p.sel]].key})
		}
		return nil
	}
	if p.multi {
		if s == " " && p.sel < len(vis) {
			key := p.rows[vis[p.sel]].key
			p.picked[key] = !p.picked[key]
		}
		return nil
	}
	if !p.filterable {
		if len(s) == 1 && s[0] >= '1' && s[0] <= '9' {
			if i := int(s[0] - '1'); i < len(vis) {
				return m.pick(p, []string{p.rows[vis[i]].key})
			}
		}
		return nil
	}
	switch k.Type {
	case tea.KeyBackspace:
		if r := []rune(p.filter); len(r) > 0 {
			p.filter = string(r[:len(r)-1])
			p.sel, p.top = 0, 0
		}
	case tea.KeyRunes, tea.KeySpace:
		p.filter += clean(string(k.Runes))
		p.sel, p.top = 0, 0
	}
	return nil
}

// view draws the picker as a bordered box no wider than w nor taller than h.
func (p *picker) view(w, h int, th *theme) []string {
	bw := min(76, w)
	inner := max(bw-4, 1)
	var body []string
	body = append(body, th.bld.Render(ellipsize(p.title, inner)))
	if p.filterable {
		if p.filter == "" {
			body = append(body, th.mut.Render("type to filter…"))
		} else {
			body = append(body, th.acc.Render("> ")+ellipsize(p.filter, inner-3)+"▏")
		}
	}
	vis := p.visible()
	p.sel = min(max(p.sel, 0), max(len(vis)-1, 0))
	room := min(maxPickRows, max(h-len(body)-4, 1)) // the border, a blank row and the hint
	if p.sel < p.top {
		p.top = p.sel
	}
	if p.sel >= p.top+room {
		p.top = p.sel - room + 1
	}
	p.top = max(min(p.top, max(len(vis)-room, 0)), 0)
	if len(vis) == 0 {
		body = append(body, th.mut.Render("no matches"))
	}
	for n := p.top; n < len(vis) && n < p.top+room; n++ {
		r := p.rows[vis[n]]
		prefix := ""
		switch {
		case p.multi:
			prefix = "[ ] "
			if p.picked[r.key] {
				prefix = "[x] "
			}
		case !p.filterable:
			prefix = "  "
			if n < 9 {
				prefix = fmt.Sprintf("%d ", n+1)
			}
		}
		if n == p.sel {
			line := prefix + r.label
			if r.desc != "" {
				line += "   " + r.desc
			}
			body = append(body, th.bld.Render(th.acc.Render(ellipsize(line, inner))))
			continue
		}
		label := th.plain
		if r.def {
			label = th.bld
		}
		segs := []seg{{prefix, th.mut}, {r.label, label}}
		if r.desc != "" {
			segs = append(segs, seg{"   " + r.desc, th.mut})
		}
		body = append(body, renderSegs(segs, inner))
	}
	hint := "type to filter · ↑↓ move · enter pick · esc cancel"
	switch {
	case p.multi:
		hint = "space toggle · enter confirm · esc cancel"
	case !p.filterable:
		hint = "1-9 pick · ↑↓ move · enter pick · esc cancel"
	}
	body = append(body, "", th.mut.Render(ellipsize(hint, inner)))
	box := th.r.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(th.acc.GetForeground()).
		Padding(0, 1).Width(bw - 2).Render(strings.Join(body, "\n"))
	return strings.Split(box, "\n")
}

// ---- the pickers of the commands ----

// fetchModels fetches model.list and opens the picker built from it.
func (m *model) fetchModels(then func(m *model, res modelList) tea.Cmd) tea.Cmd {
	var res modelList
	return call(m.cl, 15*time.Second, "model.list", map[string]any{"session": m.sess.ID}, &res, func(m *model, err error) tea.Cmd {
		if err != nil {
			m.feed.errNote("/model: " + err.Error())
			return nil
		}
		return then(m, res)
	})
}

// openPicker shows p; an approval or question still takes the screen first.
func (m *model) openPicker(p *picker) { m.picker = p }

func (m *model) modelPicker() tea.Cmd {
	return m.fetchModels(func(m *model, res modelList) tea.Cmd {
		var rows []pickRow
		for _, mod := range res.Models {
			r := pickRow{key: mod.Name, label: mod.Name, def: mod.Name == res.Current}
			if r.def {
				r.label += "  ●"
			}
			if len(mod.Efforts) > 0 {
				r.desc = "reasoning: " + strings.Join(mod.Efforts, ", ")
			}
			rows = append(rows, r)
		}
		if len(rows) == 0 {
			m.feed.note("no models", false)
			return nil
		}
		m.openPicker(newPicker("Switch model  ·  now "+res.Current, rows, true, false, func(m *model, keys []string) tea.Cmd {
			return m.command("model", keys[0], "/model "+keys[0])
		}))
		return nil
	})
}

func (m *model) reasoningPicker() tea.Cmd {
	cur, mdl := m.sess.Effort, m.sess.Model
	return m.fetchModels(func(m *model, res modelList) tea.Cmd {
		var efforts []string
		for _, mod := range res.Models {
			if mod.Name == mdl || (mdl == "" && mod.Name == res.Current) {
				efforts = mod.Efforts
			}
		}
		if len(efforts) == 0 {
			m.feed.note(modelOr(mdl, res.Current)+" has no configured reasoning levels", false)
			return nil
		}
		if cur == "" {
			cur = "default"
		}
		rows := []pickRow{{key: "", label: "default", desc: "the configured default", def: true}}
		for _, e := range efforts {
			rows = append(rows, pickRow{key: e, label: e})
		}
		m.openPicker(newPicker("Reasoning  ·  now "+cur, rows, false, false, func(m *model, keys []string) tea.Cmd {
			arg := keys[0]
			if arg == "" {
				arg = "default"
			}
			return m.command("reasoning", arg, "/reasoning "+arg)
		}))
		return nil
	})
}

// sessionPicker is /resume (kinds nil) and /pickup (the workers).
func (m *model) sessionPicker(name string, kinds []string) tea.Cmd {
	params := map[string]any{"limit": 500}
	title, empty := "Resume session", "no sessions to resume"
	if kinds != nil {
		params = map[string]any{"kinds": kinds}
		title, empty = "Pick up a worker", "no workers to pick up"
	}
	var res struct {
		Sessions []map[string]any `json:"sessions"`
	}
	return call(m.cl, 15*time.Second, "session.list", params, &res, func(m *model, err error) tea.Cmd {
		if err != nil {
			m.feed.errNote("/" + name + ": " + err.Error())
			return nil
		}
		if len(res.Sessions) == 0 {
			m.feed.note(empty, false)
			return nil
		}
		var rows []pickRow
		for _, s := range res.Sessions {
			title := str(s, "title")
			if title == "" {
				title = "untitled"
			}
			desc := tilde(str(s, "cwd"))
			if t, err := time.Parse(time.RFC3339, str(s, "updated")); err == nil {
				desc += " · " + t.Local().Format("2006-01-02 15:04")
			}
			rows = append(rows, pickRow{key: str(s, "id"), label: ellipsizeRunes(title, 80), desc: desc})
		}
		m.openPicker(newPicker(title, rows, true, false, func(m *model, keys []string) tea.Cmd {
			return m.command(name, keys[0], "/"+name+" "+keys[0])
		}))
		return nil
	})
}

// cancelPicker is /cancel: queued messages to drop.
func (m *model) cancelPicker() tea.Cmd {
	if len(m.qitems) == 0 {
		m.feed.note("nothing queued", false)
		return nil
	}
	var rows []pickRow
	for _, q := range m.qitems {
		rows = append(rows, pickRow{key: q.ID, label: cutRunes(squash(q.Text), 100)})
	}
	sid := m.sess.ID
	m.openPicker(newPicker("Drop queued messages", rows, false, true, func(m *model, keys []string) tea.Cmd {
		if len(keys) == 0 {
			return nil
		}
		return call(m.cl, 15*time.Second, "queue.remove", map[string]any{"session": sid, "items": keys}, nil,
			func(m *model, err error) tea.Cmd {
				if err != nil {
					m.feed.errNote("/cancel: " + err.Error())
				} else {
					m.feed.note(fmt.Sprintf("dropped %d queued message(s)", len(keys)), false)
				}
				return nil
			})
	}))
	return nil
}

// cutRunes is s cut to n runes, the last an ellipsis.
func cutRunes(s string, n int) string {
	if r := []rune(s); len(r) > n {
		return string(r[:n-1]) + "…"
	}
	return s
}

// palettePicker is ctrl+p: every command and alias; picking puts it in the prompt.
func (m *model) palettePicker() {
	var rows []pickRow
	for _, e := range cmdEntries() {
		rows = append(rows, pickRow{key: e.name, label: "/" + e.name, desc: e.cat + " · " + e.desc})
	}
	p := newPicker("Commands", rows, true, false, func(m *model, keys []string) tea.Cmd {
		m.ta.SetValue("/" + keys[0] + " ")
		m.histPos = len(m.hist)
		m.resetPrompt()
		return nil
	})
	p.palette = true
	m.picker = p
}
