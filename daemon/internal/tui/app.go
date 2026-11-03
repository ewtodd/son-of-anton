// Package tui is the Go terminal client of the daemon (daemon/phases/09a-tui-core.md).
package tui

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"time"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/ewtodd/son-of-anton/daemon/internal/client"
)

// Options configure Start.
type Options struct {
	Home       string    // the daemon's home
	Cwd        string    // a new session's directory, and -c's workspace (default: the working directory)
	Continue   bool      // -c
	Resume     string    // a session id, or "latest"
	Model      string    // -m
	Reasoning  string    // --reasoning
	Yolo       bool      // --yolo
	Version    string    // shown in the intro
	Autostart  bool      // reconnect with client.Connect rather than client.Dial
	Stderr     io.Writer // -c's "nothing to continue" line; nil means os.Stderr
	Background string    // the terminal's background as #rrggbb; empty: ask the terminal
}

// App is a running TUI.
type App struct {
	p    *tea.Program
	done chan struct{}

	mu          sync.Mutex
	err         error
	lastScreen  string
	lastStyled  string
	lastSession string
	interactive bool // the program owns a terminal (so an editor can take it over)
	out         io.Writer
	copied      string
}

func (a *App) send(msg tea.Msg) { a.p.Send(msg) }

// Start picks the session, attaches as driver, applies the options and
// starts the program on in and out. Every failure before that is returned.
func Start(c *client.Client, o Options, in io.Reader, out io.Writer) (*App, error) {
	if o.Stderr == nil {
		o.Stderr = os.Stderr
	}
	if o.Cwd == "" {
		o.Cwd, _ = os.Getwd()
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	res, resumed, err := attachFirst(ctx, c, o)
	if err != nil {
		return nil, err
	}
	sid := str(res.Session, "id")
	set := map[string]any{}
	if o.Model != "" {
		set["model"] = o.Model
	}
	if o.Reasoning != "" {
		set["reasoning_effort"] = o.Reasoning
	}
	if o.Yolo {
		set["mode"] = "yolo"
	}
	if len(set) > 0 {
		set["session"] = sid
		var sr struct {
			Session map[string]any `json:"session"`
		}
		if err := c.Call(ctx, "session.set", set, &sr); err != nil {
			return nil, err
		}
		res.Session = sr.Session
	}

	a := &App{done: make(chan struct{}), interactive: in != nil, out: out}
	m := newModel(a, c, o, res, resumed, startTheme(o, out))
	opts := []tea.ProgramOption{tea.WithOutput(out)}
	if in == nil {
		opts = append(opts, tea.WithInput(nil), tea.WithoutSignalHandler())
	} else {
		opts = append(opts, tea.WithInput(in), tea.WithAltScreen(), tea.WithMouseCellMotion())
	}
	a.p = tea.NewProgram(m, opts...)
	go func() {
		final, err := a.p.Run()
		a.mu.Lock()
		a.err = err
		if fm, ok := final.(*model); ok {
			styled := strings.Join(fm.render(), "\n")
			a.lastScreen, a.lastStyled = stripANSI(styled), styled
			a.lastSession = fm.sess.ID
		}
		a.mu.Unlock()
		close(a.done)
	}()
	go a.pump(c)
	return a, nil
}

// attachFirst picks and attaches the first session.
func attachFirst(ctx context.Context, c *client.Client, o Options) (res attachResult, resumed bool, err error) {
	id := o.Resume
	switch {
	case o.Resume == "latest":
		var l struct {
			Sessions []map[string]any `json:"sessions"`
		}
		if err = c.Call(ctx, "session.list", map[string]any{}, &l); err != nil {
			return
		}
		if len(l.Sessions) == 0 {
			return res, false, errors.New("no session to resume")
		}
		id = str(l.Sessions[0], "id")
	case o.Continue:
		var l struct {
			Sessions []map[string]any `json:"sessions"`
		}
		if err = c.Call(ctx, "session.list", map[string]any{"cwd": o.Cwd}, &l); err != nil {
			return
		}
		if len(l.Sessions) > 0 {
			id = str(l.Sessions[0], "id")
		} else {
			fmt.Fprintf(o.Stderr, "soa-d: no session to continue in %s; starting a new one\n", o.Cwd)
		}
	}
	if id == "" {
		var created struct {
			Session map[string]any `json:"session"`
		}
		if err = c.Call(ctx, "session.create", map[string]any{"cwd": o.Cwd}, &created); err != nil {
			return
		}
		id = str(created.Session, "id")
	} else {
		resumed = true
	}
	if err = attachSession(ctx, c, map[string]any{"session": id}, &res); err != nil {
		if rpcCode(err) == codeNoSession {
			err = errors.New("session " + id + " is not in soa-d")
		}
		return
	}
	loadKids(ctx, c, id, &res)
	return res, resumed && len(res.Messages) > 0, nil
}

// pump delivers a connection's events to the program, then its loss.
func (a *App) pump(c *client.Client) {
	for ev := range c.Events() {
		a.p.Send(evMsg{ev: ev, c: c})
	}
	a.p.Send(lostMsg{c})
}

// Wait returns when the program ends, and again at once on a later call.
func (a *App) Wait() error {
	<-a.done
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.err
}

// ask runs fn on the program's goroutine; once the program has ended, last.
func (a *App) ask(fn func(*model) string, last func() string) string {
	reply := make(chan string, 1)
	a.p.Send(queryMsg{fn, reply})
	select {
	case s := <-reply:
		return s
	case <-a.done:
		a.mu.Lock()
		defer a.mu.Unlock()
		return last()
	}
}

// Screen is what the next frame paints, ANSI stripped.
func (a *App) Screen() string {
	return a.ask(func(m *model) string { return stripANSI(strings.Join(m.render(), "\n")) },
		func() string { return a.lastScreen })
}

// Styled is the frame as painted: Screen with its styling.
func (a *App) Styled() string {
	return a.ask(func(m *model) string { return strings.Join(m.render(), "\n") },
		func() string { return a.lastStyled })
}

// Session is the id of the current session.
func (a *App) Session() string {
	return a.ask(func(m *model) string { return m.sess.ID }, func() string { return a.lastSession })
}

// Type sends text to the program, one key per rune.
func (a *App) Type(s string) {
	for _, r := range s {
		if r == ' ' {
			a.p.Send(tea.KeyMsg{Type: tea.KeySpace, Runes: []rune{' '}})
		} else {
			a.p.Send(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{r}})
		}
	}
}

var namedKeys = map[string]tea.KeyMsg{
	"enter":     {Type: tea.KeyEnter},
	"esc":       {Type: tea.KeyEsc},
	"tab":       {Type: tea.KeyTab},
	"shift+tab": {Type: tea.KeyShiftTab},
	"up":        {Type: tea.KeyUp},
	"down":      {Type: tea.KeyDown},
	"left":      {Type: tea.KeyLeft},
	"right":     {Type: tea.KeyRight},
	"pgup":      {Type: tea.KeyPgUp},
	"pgdown":    {Type: tea.KeyPgDown},
	"home":      {Type: tea.KeyHome},
	"end":       {Type: tea.KeyEnd},
	"backspace": {Type: tea.KeyBackspace},
	"space":     {Type: tea.KeySpace, Runes: []rune{' '}},
	"alt+enter": {Type: tea.KeyEnter, Alt: true},
}

func keyMsg(name string) tea.KeyMsg {
	if k, ok := namedKeys[name]; ok {
		return k
	}
	if l, ok := strings.CutPrefix(name, "ctrl+"); ok && len(l) == 1 && l[0] >= 'a' && l[0] <= 'z' {
		return tea.KeyMsg{Type: tea.KeyType(l[0] - 'a' + 1)}
	}
	return tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(name)}
}

// Press sends named keys to the program, in order.
func (a *App) Press(keys ...string) {
	for _, k := range keys {
		a.p.Send(keyMsg(k))
	}
}

// Resize tells the program the terminal's size.
func (a *App) Resize(w, h int) { a.p.Send(tea.WindowSizeMsg{Width: w, Height: h}) }

// Click is a left press and release at a cell (0-based screen column and row).
func (a *App) Click(x, y int) {
	a.p.Send(tea.MouseMsg{X: x, Y: y, Button: tea.MouseButtonLeft, Action: tea.MouseActionPress, Type: tea.MouseLeft})
	a.p.Send(tea.MouseMsg{X: x, Y: y, Button: tea.MouseButtonLeft, Action: tea.MouseActionRelease, Type: tea.MouseRelease})
}

// Drag is a left press, motion and release.
func (a *App) Drag(x0, y0, x1, y1 int) {
	a.p.Send(tea.MouseMsg{X: x0, Y: y0, Button: tea.MouseButtonLeft, Action: tea.MouseActionPress, Type: tea.MouseLeft})
	a.p.Send(tea.MouseMsg{X: x1, Y: y1, Button: tea.MouseButtonLeft, Action: tea.MouseActionMotion, Type: tea.MouseMotion})
	a.p.Send(tea.MouseMsg{X: x1, Y: y1, Button: tea.MouseButtonLeft, Action: tea.MouseActionRelease, Type: tea.MouseRelease})
}

// Wheel turns the wheel at a cell: notches > 0 scroll down, < 0 up.
func (a *App) Wheel(x, y, notches int) {
	b, ty := tea.MouseButtonWheelDown, tea.MouseWheelDown
	if notches < 0 {
		b, ty, notches = tea.MouseButtonWheelUp, tea.MouseWheelUp, -notches
	}
	for range notches {
		a.p.Send(tea.MouseMsg{X: x, Y: y, Button: b, Action: tea.MouseActionPress, Type: ty})
	}
}

// Copied is the text of the last copy, "" before one.
func (a *App) Copied() string {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.copied
}

// copy puts text on the clipboard: OSC 52 to the terminal.
func (a *App) copy(text string) {
	a.mu.Lock()
	a.copied = text
	a.mu.Unlock()
	if a.interactive && a.out != nil {
		_, _ = io.WriteString(a.out, "\x1b]52;c;"+base64.StdEncoding.EncodeToString([]byte(text))+"\x07")
	}
}
