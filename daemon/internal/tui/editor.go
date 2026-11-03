package tui

import (
	"errors"
	"os"
	"os/exec"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
)

const editorHeader = "#! Compose your prompt below. Lines starting with '#!' are ignored.\n" +
	"#! Save and quit to send; leave empty to cancel.\n\n"

// editor is ctrl+g: the draft goes through $VISUAL, else $EDITOR, else nano,
// with the program suspended and the prompt empty; what the file holds is
// sent when the editor ends.
func (m *model) editor() tea.Cmd {
	draft := m.ta.Value()
	m.ta.Reset()
	m.histPos = len(m.hist)
	m.resetPrompt()
	f, err := os.CreateTemp("", "son_of_anton_prompt_*.md")
	if err == nil {
		_, err = f.WriteString(editorHeader + draft + "\n")
		if cerr := f.Close(); err == nil {
			err = cerr
		}
	}
	if err != nil {
		if f != nil {
			_ = os.Remove(f.Name())
		}
		m.ta.SetValue(draft)
		m.feed.errNote("could not start the editor: " + err.Error())
		return nil
	}
	path := f.Name()
	editor := os.Getenv("VISUAL")
	if editor == "" {
		editor = os.Getenv("EDITOR")
	}
	if editor == "" {
		editor = "nano"
	}
	cmd := exec.Command("sh", "-c", editor+` "$1"`, "sh", path)
	done := func(runErr error) tea.Msg {
		raw, readErr := os.ReadFile(path)
		_ = os.Remove(path)
		return applyMsg{func(m *model) tea.Cmd { return m.edited(draft, string(raw), runErr, readErr) }}
	}
	m.editing = true
	if !m.app.interactive { // no terminal to hand over (the tests)
		return func() tea.Msg { return done(cmd.Run()) }
	}
	return tea.ExecProcess(cmd, done)
}

// edited sends what the editor left, or gives the draft back when it left
// nothing.
func (m *model) edited(draft, raw string, runErr, readErr error) tea.Cmd {
	m.editing = false
	var exit *exec.ExitError
	switch {
	case readErr != nil:
		m.feed.errNote("could not read the editor's file: " + readErr.Error())
	case runErr != nil && !errors.As(runErr, &exit): // an editor that exits non-zero still saved
		m.feed.errNote("could not run the editor: " + runErr.Error())
		raw = ""
	}
	var keep []string
	for _, l := range strings.Split(raw, "\n") {
		if !strings.HasPrefix(l, "#!") {
			keep = append(keep, l)
		}
	}
	text := strings.TrimSpace(strings.Join(keep, "\n"))
	if text == "" {
		if m.ta.Value() == "" {
			m.ta.SetValue(draft)
		}
		return nil
	}
	m.record(text)
	return m.dispatch(text)
}
