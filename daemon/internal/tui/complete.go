package tui

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	tea "github.com/charmbracelet/bubbletea"
)

const maxCompRows = 9

// cand is one row of the completion list.
type cand struct {
	insert  string // replaces the word being completed
	display string
	desc    string
}

// completion is the open list above the prompt.
type completion struct {
	items []cand
	start int // byte offset of the word in the draft
	sel   int
	top   int
	path  bool // opened for a path: typing keeps it open
}

// wordStart is the byte offset where the last word of v begins.
func wordStart(v string) int {
	i := strings.LastIndexFunc(v, unicode.IsSpace)
	if i < 0 {
		return 0
	}
	_, n := utf8.DecodeRuneInString(v[i:])
	return i + n
}

// atEnd reports whether the cursor is at the end of the draft.
func (m *model) atEnd() bool {
	if m.ta.Line() != m.ta.LineCount()-1 {
		return false
	}
	li := m.ta.LineInfo()
	lines := strings.Split(m.ta.Value(), "\n")
	return li.StartColumn+li.ColumnOffset >= len([]rune(lines[len(lines)-1]))
}

// resetPrompt forgets what the list and the fetched models were for.
func (m *model) resetPrompt() {
	m.comp = nil
	m.models = nil
	m.modelsGen++
	m.modelsPending = false
	m.tabPending = false
}

// complete finds the candidates for the word at the end of the draft.
// explicit is a tab; paths allows file names. need is true when the models
// are wanted and not yet fetched.
func (m *model) complete(explicit, paths bool) (c *completion, need bool) {
	if !m.atEnd() {
		return nil, false
	}
	v := m.ta.Value()
	start := wordStart(v)
	word := v[start:]
	switch {
	case strings.HasPrefix(v, "/") && start == 0:
		var items []cand
		sel := 0
		for _, e := range cmdEntries() {
			if strings.HasPrefix("/"+e.name, strings.ToLower(word)) {
				if "/"+e.name == word {
					sel = len(items)
				}
				items = append(items, cand{"/" + e.name + " ", "/" + e.name, e.desc})
			}
		}
		if len(items) == 1 && items[0].display == word {
			items = nil
		}
		if len(items) > 0 {
			return &completion{items: items, start: 0, sel: sel}, false
		}
	case strings.HasPrefix(v, "/") && !strings.Contains(v, "\n"):
		c, need = m.completeArg(v, start, word, explicit)
		if c != nil || need {
			return c, need
		}
	}
	if paths && isPathWord(word) && !(start == 0 && isCommandWord(word)) {
		if items := m.completePath(word); len(items) > 0 {
			return &completion{items: items, start: start, path: true}, false
		}
	}
	return nil, false
}

func isPathWord(w string) bool {
	if w == "" || strings.Contains(w, "://") {
		return false
	}
	return strings.HasPrefix(w, "./") || strings.HasPrefix(w, "../") || strings.HasPrefix(w, "~/") ||
		strings.HasPrefix(w, "/") || strings.Contains(w, "/")
}

func isCommandWord(w string) bool {
	for _, e := range cmdEntries() {
		if strings.HasPrefix("/"+e.name, strings.ToLower(w)) {
			return true
		}
	}
	return false
}

// completeArg completes the argument of a one-argument command.
func (m *model) completeArg(v string, start int, word string, explicit bool) (*completion, bool) {
	base, rest, _ := strings.Cut(v, " ")
	cmd := canonicalCmd(strings.TrimPrefix(base, "/"))
	if cmd == "" || start == 0 || strings.TrimSpace(rest[:max(start-len(base)-1, 0)]) != "" {
		return nil, false // not a command, or it already has its argument
	}
	if word == "" && !explicit {
		return nil, false
	}
	var names []string
	switch cmd {
	case "perm":
		names = modes
	case "skin":
		for _, sk := range loadSkins(m.o.Home) {
			names = append(names, sk.name)
		}
	case "model", "reasoning":
		if m.models == nil {
			return nil, true
		}
		if cmd == "model" {
			for _, mod := range m.models.Models {
				if m.models.Statuses && !mod.Serving {
					continue // the picker offers only what is being served
				}
				names = append(names, mod.Name)
			}
			break
		}
		names = []string{"default"}
		seen := map[string]bool{"default": true}
		add := func(es []string) {
			for _, e := range es {
				if !seen[e] {
					seen[e] = true
					names = append(names, e)
				}
			}
		}
		mdl := m.sess.Model
		for _, mod := range m.models.Models {
			if mod.Name == mdl || (mdl == "" && mod.Name == m.models.Current) {
				add(mod.Efforts)
			}
		}
	default:
		return nil, false
	}
	var items []cand
	sel := 0
	for _, n := range names {
		if strings.HasPrefix(strings.ToLower(n), strings.ToLower(word)) {
			if n == word {
				sel = len(items)
			}
			items = append(items, cand{n + " ", n, ""})
		}
	}
	if len(items) == 0 || (len(items) == 1 && items[0].display == word) {
		return nil, false
	}
	return &completion{items: items, start: start, sel: sel}, false
}

// completePath lists the entries of the directory word names that start with
// the name after its last slash: directories first, then files.
func (m *model) completePath(word string) []cand {
	dirPart, name := word[:strings.LastIndex(word, "/")+1], word[strings.LastIndex(word, "/")+1:]
	dir := dirPart
	switch {
	case strings.HasPrefix(dir, "~/"):
		h, err := os.UserHomeDir()
		if err != nil {
			return nil
		}
		dir = filepath.Join(h, dir[2:])
	case !filepath.IsAbs(dir):
		dir = filepath.Join(m.sess.Cwd, dir)
	}
	ents, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var dirs, files []cand
	for _, e := range ents {
		n := e.Name()
		if !strings.HasPrefix(n, name) || (strings.HasPrefix(n, ".") && !strings.HasPrefix(name, ".")) {
			continue
		}
		isDir := e.IsDir()
		if !isDir && e.Type()&os.ModeSymlink != 0 {
			if fi, err := os.Stat(filepath.Join(dir, n)); err == nil {
				isDir = fi.IsDir()
			}
		}
		if isDir {
			dirs = append(dirs, cand{dirPart + n + "/", n + "/", ""})
		} else {
			files = append(files, cand{dirPart + n + " ", n, ""})
		}
	}
	byName := func(s []cand) { sort.Slice(s, func(i, j int) bool { return s[i].display < s[j].display }) }
	byName(dirs)
	byName(files)
	return append(dirs, files...)
}

// refreshComp updates the list after the draft changed by typing.
func (m *model) refreshComp() tea.Cmd {
	c, need := m.complete(false, m.comp != nil && m.comp.path)
	m.comp = c
	if need {
		return m.fetchComp()
	}
	return nil
}

// fetchComp fetches model.list once for this prompt.
func (m *model) fetchComp() tea.Cmd {
	if m.modelsPending {
		return nil
	}
	m.modelsPending = true
	gen := m.modelsGen
	var res modelList
	return call(m.cl, 15*time.Second, "model.list", map[string]any{"session": m.sess.ID}, &res, func(m *model, err error) tea.Cmd {
		if gen != m.modelsGen {
			return nil
		}
		m.modelsPending = false
		if err != nil {
			m.tabPending = false
			return nil
		}
		m.models = &res
		if m.tabPending {
			m.tabPending = false
			return m.tab()
		}
		return m.refreshComp()
	})
}

// compWord is the word the list completes.
func (m *model) compWord() string { return m.ta.Value()[m.comp.start:] }

// accept replaces the word by the highlighted row.
func (m *model) accept() tea.Cmd {
	c := m.comp
	m.applyInsert(c.start, c.items[c.sel].insert)
	return nil
}

// applyInsert replaces the draft from start by ins and looks at what is left
// to complete: after a directory, its entries.
func (m *model) applyInsert(start int, ins string) {
	m.ta.SetHeight(m.taCap())
	m.ta.SetValue(m.ta.Value()[:start] + ins)
	m.histPos = len(m.hist)
	m.comp = nil
	if strings.HasSuffix(ins, "/") {
		m.comp, _ = m.complete(true, true)
		return
	}
	m.comp, _ = m.complete(false, false)
}

// tab is the tab key.
func (m *model) tab() tea.Cmd {
	if m.comp != nil {
		return m.accept()
	}
	c, need := m.complete(true, true)
	if need {
		m.tabPending = true
		return m.fetchComp()
	}
	if c == nil {
		return nil
	}
	if len(c.items) == 1 {
		m.applyInsert(c.start, c.items[0].insert)
		return nil
	}
	word := m.ta.Value()[c.start:]
	var ins []string
	for _, it := range c.items {
		ins = append(ins, it.insert)
	}
	if p := commonPrefix(ins); len(p) > len(word) {
		m.ta.SetValue(m.ta.Value()[:c.start] + p)
		return nil
	}
	m.comp = c
	return nil
}

func commonPrefix(s []string) string {
	p := s[0]
	for _, x := range s[1:] {
		for !strings.HasPrefix(x, p) {
			p = p[:len(p)-1]
		}
	}
	return p
}

// compKey handles the keys an open list takes; ok is false for the rest.
func (m *model) compKey(s string) (cmd tea.Cmd, ok bool) {
	c := m.comp
	switch s {
	case "esc":
		m.comp = nil
		return nil, true
	case "up":
		c.sel = max(c.sel-1, 0)
		return nil, true
	case "down":
		c.sel = min(c.sel+1, len(c.items)-1)
		return nil, true
	case "tab":
		return m.accept(), true
	case "enter":
		it := c.items[c.sel]
		if strings.TrimRight(it.insert, " ") == m.compWord() {
			m.comp = nil
			return nil, false // the row is what was typed: send it
		}
		return m.accept(), true
	}
	return nil, false
}

// lines draws the visible rows of the list, cw columns wide.
func (m *model) compLines(cw int) []string {
	c := m.comp
	if c == nil {
		return nil
	}
	th := m.th
	n := m.compRows()
	c.sel = min(max(c.sel, 0), len(c.items)-1)
	if c.sel < c.top {
		c.top = c.sel
	}
	if c.sel >= c.top+n {
		c.top = c.sel - n + 1
	}
	longest := 0
	for _, it := range c.items {
		longest = max(longest, textWidth(it.display))
	}
	colw := min(longest+2, 48)
	var out []string
	for i := c.top; i < c.top+n; i++ {
		it := c.items[i]
		room := max(cw-4, 1)
		var line string
		if i == c.sel {
			text := pad(ellipsize(it.display, colw), colw) + it.desc
			line = th.bld.Render(th.acc.Render(ellipsize(strings.TrimRight(text, " "), room)))
		} else {
			line = renderSegs([]seg{{pad(ellipsize(it.display, colw), colw), th.bld}, {it.desc, th.mut}}, room)
		}
		line = "   " + line
		if th.surface != nil {
			line = th.paint(line, th.surface, cw)
		}
		out = append(out, line)
	}
	return out
}

// compRows is how many rows of the list show: at most nine, and never so many
// that fewer than three feed rows remain.
func (m *model) compRows() int {
	if m.comp == nil {
		return 0
	}
	return min(len(m.comp.items), maxCompRows, max(m.h-8-minFeedRows-m.queueRows()-m.taRows(), 0))
}

// suggestion is the rest of the newest history entry the draft starts: shown
// after the cursor while the draft is one line, the cursor at its end and no
// list open.
func (m *model) suggestion() string {
	v := m.ta.Value()
	if v == "" || m.comp != nil || strings.Contains(v, "\n") || !m.atEnd() {
		return ""
	}
	for i := len(m.hist) - 1; i >= 0; i-- {
		h := m.hist[i]
		if len(h) > len(v) && strings.HasPrefix(h, v) && !strings.Contains(h, "\n") {
			return h[len(v):]
		}
	}
	return ""
}
