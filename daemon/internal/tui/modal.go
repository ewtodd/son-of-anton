package tui

import (
	"fmt"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

const dismissedAnswer = "The user dismissed the question without answering; use your best judgement."

type choice struct{ value, label, desc string }

type modal struct {
	question bool
	id       string

	// approval
	title, subject string
	reasons        []string
	choices        []choice

	// question
	text     string
	options  []string // the question's choices, then "Other…"
	multi    bool
	picked   map[int]bool
	typing   bool // typing an answer (Other, or no choices)
	input    string
	hasOther bool

	sel int
}

var approvalChoices = map[string]choice{
	"once":    {"once", "Allow once", "run this now"},
	"session": {"session", "Allow for this session", "no more prompts for this until exit"},
	"always":  {"always", "Always allow", "add to the allowlist"},
	"deny":    {"deny", "Deny", "the agent is told it was refused"},
}

func newModal(kind string, p map[string]any) *modal {
	if kind == "question.request" {
		md := &modal{question: true, id: str(p, "question_id"), text: str(p, "text"), picked: map[int]bool{}}
		md.multi, _ = p["multi_select"].(bool)
		if raw, ok := p["choices"].([]any); ok {
			for _, c := range raw {
				if s, ok := c.(string); ok {
					md.options = append(md.options, s)
				}
			}
		}
		if len(md.options) == 0 {
			md.typing = true
		} else {
			md.options = append(md.options, "Other…")
			md.hasOther = true
		}
		return md
	}
	md := &modal{id: str(p, "approval_id"), subject: str(p, "subject"), title: "The agent wants to run a command"}
	memory := str(p, "approval_kind") == "memory"
	if memory {
		md.title = "Save to memory?"
	}
	if raw, ok := p["reasons"].([]any); ok {
		for _, r := range raw {
			if s, ok := r.(string); ok {
				md.reasons = append(md.reasons, s)
			}
		}
	}
	if raw, ok := p["choices"].([]any); ok {
		for _, r := range raw {
			s, _ := r.(string)
			c, ok := approvalChoices[s]
			if !ok {
				c = choice{s, s, ""}
			}
			if memory {
				switch s {
				case "once":
					c = choice{s, "Save", "write it to memory"}
				case "deny":
					c = choice{s, "Don't save", "the agent is told it was refused"}
				}
			}
			md.choices = append(md.choices, c)
		}
	}
	return md
}

func (m *model) openModal(md *modal) {
	for _, o := range m.modals {
		if o.id == md.id && o.question == md.question {
			return
		}
	}
	m.modals = append(m.modals, md)
}

func (m *model) closeModal(id string) {
	for i, o := range m.modals {
		if o.id == id {
			m.modals = append(m.modals[:i], m.modals[i+1:]...)
			return
		}
	}
}

// modalKey sends keys to the oldest open modal.
func (m *model) modalKey(k tea.KeyMsg) tea.Cmd {
	md := m.modals[0]
	if md.question {
		return m.questionKey(md, k)
	}
	s := k.String()
	switch s {
	case "esc":
		return m.answerApproval(md, "deny")
	case "up":
		md.sel = max(md.sel-1, 0)
	case "down":
		md.sel = min(md.sel+1, len(md.choices)-1)
	case "enter":
		if md.sel < len(md.choices) {
			return m.answerApproval(md, md.choices[md.sel].value)
		}
	default:
		if len(s) == 1 && s[0] >= '1' && s[0] <= '9' {
			if i := int(s[0] - '1'); i < len(md.choices) {
				return m.answerApproval(md, md.choices[i].value)
			}
		}
	}
	return nil
}

func (m *model) answerApproval(md *modal, choice string) tea.Cmd {
	m.closeModal(md.id)
	return call(m.cl, 10*time.Second, "approval.answer",
		map[string]any{"session": m.sess.ID, "approval_id": md.id, "choice": choice}, nil,
		func(m *model, err error) tea.Cmd {
			if c := rpcCode(err); err != nil && c != codeNoSession && c != -32012 {
				m.feed.note("could not answer the approval: "+err.Error(), false)
			}
			return nil
		})
}

func (m *model) answerQuestion(md *modal, answers []string) tea.Cmd {
	m.closeModal(md.id)
	return call(m.cl, 10*time.Second, "question.answer",
		map[string]any{"session": m.sess.ID, "question_id": md.id, "answers": answers}, nil,
		func(m *model, err error) tea.Cmd {
			if c := rpcCode(err); err != nil && c != codeNoSession && c != -32012 {
				m.feed.note("could not answer the question: "+err.Error(), false)
			}
			return nil
		})
}

func (m *model) questionKey(md *modal, k tea.KeyMsg) tea.Cmd {
	s := k.String()
	if s == "esc" {
		return m.answerQuestion(md, []string{dismissedAnswer})
	}
	if md.typing {
		switch k.Type {
		case tea.KeyEnter:
			text := strings.TrimSpace(md.input)
			var answers []string
			if md.multi {
				for i, o := range md.options {
					if md.picked[i] && !(md.hasOther && i == len(md.options)-1) {
						answers = append(answers, o)
					}
				}
			}
			if text != "" {
				answers = append(answers, text)
			}
			if len(answers) == 0 {
				answers = []string{dismissedAnswer}
			}
			return m.answerQuestion(md, answers)
		case tea.KeyBackspace:
			if r := []rune(md.input); len(r) > 0 {
				md.input = string(r[:len(r)-1])
			}
		case tea.KeyRunes, tea.KeySpace:
			md.input += clean(string(k.Runes))
		}
		return nil
	}
	other := len(md.options) - 1
	switch {
	case s == "up":
		md.sel = max(md.sel-1, 0)
	case s == "down":
		md.sel = min(md.sel+1, len(md.options)-1)
	case s == " " && md.multi:
		md.picked[md.sel] = !md.picked[md.sel]
	case s == "enter":
		return m.pickOption(md, md.sel, other)
	case len(s) == 1 && s[0] >= '1' && s[0] <= '9' && int(s[0]-'1') < len(md.options):
		md.sel = int(s[0] - '1')
		if md.multi {
			md.picked[md.sel] = !md.picked[md.sel]
		} else {
			return m.pickOption(md, md.sel, other)
		}
	}
	return nil
}

func (m *model) pickOption(md *modal, i, other int) tea.Cmd {
	if md.multi {
		var answers []string
		for j, o := range md.options {
			if md.picked[j] && j != other {
				answers = append(answers, o)
			}
		}
		if md.picked[other] {
			md.typing = true
			return nil
		}
		if len(answers) == 0 {
			answers = []string{dismissedAnswer}
		}
		return m.answerQuestion(md, answers)
	}
	if i == other {
		md.typing = true
		return nil
	}
	return m.answerQuestion(md, []string{md.options[i]})
}

// view draws the modal as a bordered box no wider than w.
func (md *modal) view(w int) []string {
	bw := min(76, w)
	inner := max(bw-4, 1)
	border := accent
	var body []string
	add := func(s string) { body = append(body, s) }
	row := func(i, sel int, text, desc string, mark string) {
		label := fmt.Sprintf("%s%d. %s", mark, i+1, text)
		line := label
		if desc != "" {
			line += "  " + desc
		}
		line = ellipsize(line, inner)
		if i == sel {
			add(bold.Render(accent.Render(line)))
		} else {
			add(line)
		}
	}
	if md.question {
		add(bold.Render("Son of Anton needs your input"))
		add("")
		for _, l := range wrapText(clean(md.text), inner) {
			add(l)
		}
		add("")
		if md.typing {
			add(muted.Render("your answer"))
			add(accent.Render("> ") + ellipsize(md.input, inner-3) + "▏")
		} else {
			for i, o := range md.options {
				mark := ""
				if md.multi {
					mark = "[ ] "
					if md.picked[i] {
						mark = "[x] "
					}
				}
				row(i, md.sel, o, "", mark)
			}
		}
		add("")
		hint := "1-9 pick · ↑↓ move · enter pick · esc dismiss"
		if md.multi {
			hint = "space toggle · ↑↓ move · enter send · esc dismiss"
		}
		if md.typing {
			hint = "enter send · esc dismiss"
		}
		add(muted.Render(ellipsize(hint, inner)))
	} else {
		border = lipgloss.NewStyle().Foreground(lipgloss.Color("214"))
		add(bold.Render(md.title))
		add("")
		for _, l := range wrapText(clean(md.subject), inner) {
			add(bold.Render(l))
		}
		if len(md.reasons) > 0 {
			for _, l := range wrapText(clean(strings.Join(md.reasons, "; ")), inner) {
				add(muted.Render(l))
			}
		}
		add("")
		for i, c := range md.choices {
			row(i, md.sel, c.label, c.desc, "")
		}
		add("")
		add(muted.Render(ellipsize("1-9 pick · ↑↓ move · enter pick · esc cancel", inner)))
	}
	box := lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(border.GetForeground()).
		Padding(0, 1).Width(bw - 2).Render(strings.Join(body, "\n"))
	return strings.Split(box, "\n")
}
