package tui

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"syscall"
	"time"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/ewtodd/son-of-anton/daemon/internal/client"
)

func ctxTimeout(d time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), d)
}

// call runs one RPC in the background and applies its outcome on the
// program's goroutine. do must not touch the model.
func call(c *client.Client, timeout time.Duration, method string, params map[string]any, result any, then func(m *model, err error) tea.Cmd) tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := ctxTimeout(timeout)
		defer cancel()
		err := c.Call(ctx, method, params, result)
		return applyMsg{func(m *model) tea.Cmd { return then(m, err) }}
	}
}

// rpcCode is the JSON-RPC code of err, or 0.
func rpcCode(err error) int {
	var rpc *client.Error
	if errors.As(err, &rpc) {
		return rpc.Code
	}
	return 0
}

// interrupt asks the daemon to stop the running turn.
func (m *model) interrupt() tea.Cmd {
	return call(m.cl, 5*time.Second, "turn.interrupt", map[string]any{"session": m.sess.ID}, nil,
		func(m *model, err error) tea.Cmd {
			if err != nil {
				m.feed.errNote("could not interrupt: " + err.Error())
			}
			return nil
		})
}

// setMode switches the permission mode with shift+tab.
func (m *model) setMode(mode string) tea.Cmd {
	var res struct {
		Session map[string]any `json:"session"`
	}
	return call(m.cl, 10*time.Second, "session.set", map[string]any{"session": m.sess.ID, "mode": mode}, &res,
		func(m *model, err error) tea.Cmd {
			if err != nil {
				m.feed.errNote("could not switch permission mode: " + err.Error())
				return nil
			}
			m.sess = parseSession(res.Session, m.sess)
			return nil
		})
}

// submit sends what the prompt holds; mode is turn.send's for a plain message.
func (m *model) submit(mode string) tea.Cmd {
	text := strings.TrimSpace(m.ta.Value())
	if text == "" {
		return nil
	}
	m.record(m.ta.Value())
	m.ta.Reset()
	m.resetPrompt()
	return m.dispatch(text, mode)
}

func (m *model) dispatch(text, mode string) tea.Cmd {
	switch {
	case text == ":q":
		return m.leave()
	case strings.HasPrefix(text, "!"):
		return m.bang(strings.TrimSpace(text[1:]), text)
	case strings.HasPrefix(text, "/"):
		word, args, _ := strings.Cut(text, " ")
		if !strings.Contains(word[1:], "/") {
			return m.command(word[1:], strings.TrimSpace(args), text)
		}
	}
	return m.send(text, mode)
}

// send is turn.send; the daemon starts the turn or queues the text.
func (m *model) send(text, mode string) tea.Cmd {
	return call(m.cl, 30*time.Second, "turn.send", map[string]any{"session": m.sess.ID, "text": text, "mode": mode}, nil,
		func(m *model, err error) tea.Cmd {
			if err != nil {
				m.feed.errNote("could not send: " + err.Error())
			}
			return nil
		})
}

// ---- ! lines ----

func (m *model) bang(cmd, line string) tea.Cmd {
	m.feed.note(line, true)
	if cmd == "" {
		m.feed.note("Type ! followed by a shell command to run it directly, with no model turn, for example !git status", false)
		return nil
	}
	cwd, send := m.sess.Cwd, m.app.send
	go runBang(cwd, cmd, send)
	return nil
}

// runBang runs the line in cwd and streams its output into the feed as notes.
func runBang(cwd, line string, send func(tea.Msg)) {
	note := func(text string, isMuted bool) {
		send(applyMsg{func(m *model) tea.Cmd { m.feed.note(text, isMuted); return nil }})
	}
	ctx, cancel := ctxTimeout(120 * time.Second)
	defer cancel()
	c := exec.CommandContext(ctx, "bash", "-c", line)
	c.Dir = cwd
	c.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	c.Cancel = func() error { return syscall.Kill(-c.Process.Pid, syscall.SIGKILL) }
	c.WaitDelay = 2 * time.Second
	out, err := c.StdoutPipe()
	if err != nil {
		note("! "+err.Error(), false)
		return
	}
	c.Stderr = c.Stdout
	if err := c.Start(); err != nil {
		note("! "+err.Error(), false)
		return
	}
	sc := bufio.NewScanner(out)
	sc.Buffer(make([]byte, 64<<10), 1<<20)
	for sc.Scan() {
		note(sc.Text(), false)
	}
	err = c.Wait()
	var ee *exec.ExitError
	switch {
	case errors.As(err, &ee):
		note(fmt.Sprintf("! exited %d", ee.ExitCode()), false)
	case err != nil:
		note("! "+err.Error(), false)
	}
}

// ---- slash commands ----

func (m *model) command(name, args, line string) tea.Cmd {
	switch name {
	case "quit", "exit", "q":
		return m.leave()
	}
	m.feed.note("⚙ "+line, true)
	c, sid := m.cl, m.sess.ID
	fail := func(m *model, err error) {
		m.feed.errNote("/" + name + ": " + err.Error())
	}
	switch name {
	case "help":
		m.feed.note(helpText(), false)
	case "model":
		if args != "" {
			return m.modelNamed(args)
		}
		return m.modelPicker()
	case "reasoning":
		if args != "" {
			effort := args
			if effort == "default" {
				effort = ""
			}
			return m.set(map[string]any{"reasoning_effort": effort}, "reasoning: "+args+" (from the next turn)", name, nil)
		}
		return m.reasoningPicker()
	case "perm":
		if args != "" {
			return m.set(map[string]any{"mode": args}, "permission mode: "+args, name, nil)
		}
		m.feed.note("permission mode: "+m.sess.Mode+"  ("+strings.Join(modes, " | ")+")", false)
	case "new", "reset", "clear":
		cwd := m.sess.Cwd
		return m.switchSession(func(ctx context.Context, c *client.Client) (string, error) {
			var created struct {
				Session map[string]any `json:"session"`
			}
			if err := c.Call(ctx, "session.create", map[string]any{"cwd": cwd}, &created); err != nil {
				return "", err
			}
			return str(created.Session, "id"), nil
		}, false, name)
	case "resume", "sessions":
		if args != "" {
			return m.switchSession(func(ctx context.Context, c *client.Client) (string, error) {
				var res struct {
					Sessions []map[string]any `json:"sessions"`
				}
				if err := c.Call(ctx, "session.list", map[string]any{"limit": 500}, &res); err != nil {
					return "", err
				}
				for _, s := range res.Sessions {
					if strings.EqualFold(str(s, "title"), args) {
						return str(s, "id"), nil
					}
				}
				return args, nil // an id, of any kind of session
			}, true, name)
		}
		return m.sessionPicker(name)
	case "handoff":
		return call(c, 15*time.Second, "session.handoff", map[string]any{"session": sid}, nil,
			func(m *model, err error) tea.Cmd {
				if err != nil {
					fail(m, err)
					return nil
				}
				m.exitNote = handoffNote(sid)
				return m.quit(false)
			})
	case "commit":
		return call(c, 30*time.Second, "command.run", map[string]any{"session": sid, "name": "commit", "args": args}, nil,
			func(m *model, err error) tea.Cmd {
				if err != nil {
					fail(m, err)
				}
				return nil
			})
	case "stop":
		var res struct {
			Killed int `json:"killed"`
		}
		return call(c, 15*time.Second, "command.run", map[string]any{"session": sid, "name": "stop", "args": ""}, &res,
			func(m *model, err error) tea.Cmd {
				if err != nil {
					fail(m, err)
				} else {
					m.feed.note(fmt.Sprintf("killed %d background process(es)", res.Killed), false)
				}
				return nil
			})
	case "compact":
		if args == "--preview" || args == "--dry-run" {
			var res struct {
				Preview struct {
					Rows       int `json:"rows"`
					ThroughSeq int `json:"through_seq"`
					Tokens     int `json:"tokens"`
				} `json:"preview"`
			}
			return call(c, 30*time.Second, "command.run", map[string]any{"session": sid, "name": "compact", "args": args}, &res,
				func(m *model, err error) tea.Cmd {
					if err != nil {
						fail(m, err)
					} else {
						p := res.Preview
						m.feed.note(fmt.Sprintf("would summarize %s rows (through seq %s, about %s tokens) and keep the rest verbatim",
							commas(p.Rows), commas(p.ThroughSeq), commas(p.Tokens)), false)
					}
					return nil
				})
		}
		m.feed.note("compacting…", true)
		return call(c, 600*time.Second, "command.run", map[string]any{"session": sid, "name": "compact", "args": args}, nil,
			func(m *model, err error) tea.Cmd {
				if err != nil {
					fail(m, err)
				}
				return nil
			})
	case "retitle":
		args = strings.TrimSpace(args)
		if args == "" {
			m.feed.note("usage: /retitle <the name this conversation has now> — it renames the session here and in /resume, the sidebar and fiona's lists", false)
			return nil
		}
		return call(c, 10*time.Second, "session.rename", map[string]any{"session": sid, "title": args}, nil,
			func(m *model, err error) tea.Cmd {
				if err != nil {
					fail(m, err)
				} else {
					m.feed.note("retitled — the sidebar and /resume will follow", false)
				}
				return nil
			})
	case "sendnow":
		if args != "" {
			return call(c, 30*time.Second, "turn.send", map[string]any{"session": sid, "text": args, "mode": "now"}, nil,
				func(m *model, err error) tea.Cmd {
					if err != nil {
						fail(m, err)
					}
					return nil
				})
		}
		fallthrough
	case "sendall":
		var res struct {
			Started bool `json:"started"`
			Steered bool `json:"steered"`
		}
		return call(c, 30*time.Second, "queue.steer", map[string]any{"session": sid, "all": name == "sendall"}, &res,
			func(m *model, err error) tea.Cmd {
				switch {
				case err != nil:
					fail(m, err)
				case !res.Started && !res.Steered:
					m.feed.note("nothing queued to send", false)
				}
				return nil
			})
	case "cancel":
		return m.cancelPicker()
	case "skin":
		m.skinCommand(args)
	case "status":
		s := m.sess
		m.feed.note(fmt.Sprintf("session %s · %s · mode %s · in %s out %s · %s calls",
			s.ID, modelName(s.Model, s.Served), s.Mode, commas(s.In), commas(s.Out), commas(s.Calls)), false)
	default:
		m.feed.note("unknown command /"+name+" (/help lists them)", false)
	}
	return nil
}

func modelOr(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

type modelList struct {
	Models []struct {
		Name    string   `json:"name"`
		Efforts []string `json:"reasoning_efforts"`
		// Status is the model's live status ("serving"/"unavailable"/"" when
		// unsaid); Serving is true unless it is unavailable.
		Status  string `json:"status"`
		Serving bool   `json:"serving"`
	} `json:"models"`
	Current  string `json:"current"`
	Statuses bool   `json:"statuses"`
}

// set is session.set with the note to show once it took; after draws what
// follows a successful set (running once the session is updated).
func (m *model) set(params map[string]any, ok, name string, after func(*model) tea.Cmd) tea.Cmd {
	params["session"] = m.sess.ID
	var res struct {
		Session map[string]any `json:"session"`
	}
	return call(m.cl, 15*time.Second, "session.set", params, &res, func(m *model, err error) tea.Cmd {
		if err != nil {
			m.feed.note("/"+name+": "+err.Error(), false)
			return nil
		}
		m.sess = parseSession(res.Session, m.sess)
		m.feed.note(ok, false)
		if after != nil {
			return after(m)
		}
		return nil
	})
}

type errNote string

func (e errNote) Error() string { return string(e) }

// switchSession makes another session the current one: pick names it (and may
// create it), then it is attached as driver and the old one detached.
func (m *model) switchSession(pick func(context.Context, *client.Client) (string, error), resumed bool, name string) tea.Cmd {
	c, old := m.cl, m.sess.ID
	return func() tea.Msg {
		ctx, cancel := ctxTimeout(30 * time.Second)
		defer cancel()
		fail := func(err error) tea.Msg {
			return applyMsg{func(m *model) tea.Cmd {
				var n errNote
				switch {
				case errors.As(err, &n):
					m.feed.note(string(n), false)
				case rpcCode(err) == codeNoSession:
					m.feed.note(err.Error(), false)
				default:
					m.feed.note("/"+name+": "+err.Error(), false)
				}
				return nil
			}}
		}
		id, err := pick(ctx, c)
		if err != nil {
			return fail(err)
		}
		var res attachResult
		if err := attachSession(ctx, c, map[string]any{"session": id}, &res); err != nil {
			if rpcCode(err) == codeNoSession {
				return fail(errNote("session " + id + " is not in soa-d"))
			}
			return fail(err)
		}
		loadKids(ctx, c, id, &res)
		if id != old {
			_ = c.Call(ctx, "session.detach", map[string]any{"session": old}, nil)
		}
		return applyMsg{func(m *model) tea.Cmd {
			if m.cl != c {
				return nil
			}
			return tea.Batch(m.adopt(res, resumed && len(res.Messages) > 0), m.afterAttach())
		}}
	}
}

// skinCommand is /skin: list the skins, or draw everything in one at once. It
// is not saved (config.toml is Nix-managed).
func (m *model) skinCommand(name string) {
	skins := loadSkins(m.o.Home)
	if name == "" {
		lines := []string{"skin: " + m.th.name}
		for _, sk := range skins {
			mark := "    "
			if sk.name == m.th.name {
				mark = "  ● "
			}
			lines = append(lines, mark+sk.name+" — "+sk.desc)
		}
		m.feed.note(strings.Join(lines, "\n"), false)
		return
	}
	for _, sk := range skins {
		if sk.name == name {
			m.setTheme(newTheme(m.th.r, sk, m.th.pal))
			m.feed.note("skin: "+name, false)
			return
		}
	}
	m.feed.note("unknown skin: "+name, false)
}

// attachSession attaches to id as its driver. A session nobody may drive (a
// physics run's: its controller does) is attached as a viewer instead, so the
// run can be watched.
func attachSession(ctx context.Context, c *client.Client, params map[string]any, res *attachResult) error {
	params["as"] = "driver"
	err := c.Call(ctx, "session.attach", params, res)
	if rpcCode(err) == codeNotDriver {
		params["as"] = "viewer"
		err = c.Call(ctx, "session.attach", params, res)
	}
	return err
}
