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
				m.feed.note("could not interrupt: "+err.Error(), false)
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
				m.feed.note("could not switch permission mode: "+err.Error(), false)
				return nil
			}
			m.sess = parseSession(res.Session, m.sess)
			return nil
		})
}

// submit sends what the prompt holds.
func (m *model) submit() tea.Cmd {
	text := strings.TrimSpace(m.ta.Value())
	if text == "" {
		return nil
	}
	m.record(m.ta.Value())
	m.ta.Reset()
	return m.dispatch(text)
}

func (m *model) dispatch(text string) tea.Cmd {
	switch {
	case text == ":q":
		return m.quit(false)
	case strings.HasPrefix(text, "!"):
		return m.bang(strings.TrimSpace(text[1:]), text)
	case strings.HasPrefix(text, "/"):
		word, args, _ := strings.Cut(text, " ")
		if !strings.Contains(word[1:], "/") {
			return m.command(word[1:], strings.TrimSpace(args), text)
		}
	}
	return m.send(text, "queue")
}

// send is turn.send; the daemon starts the turn or queues the text.
func (m *model) send(text, mode string) tea.Cmd {
	return call(m.cl, 30*time.Second, "turn.send", map[string]any{"session": m.sess.ID, "text": text, "mode": mode}, nil,
		func(m *model, err error) tea.Cmd {
			if err != nil {
				m.feed.note("could not send: "+err.Error(), false)
			}
			return nil
		})
}

// ---- ! lines ----

func (m *model) bang(cmd, line string) tea.Cmd {
	m.feed.note(line, true)
	if cmd == "" {
		m.feed.note("Usage: !<command> — run a shell command without spending a model turn (e.g. !git status)", false)
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

const helpText = `commands:
  /help — show this list
  /model [name] — show the models or switch to one
  /reasoning [level] — show or set the reasoning effort
  /perm [mode] — show or set the permission mode
  /new — start a new session (/reset, /clear)
  /resume [id] — list sessions or switch to one (/sessions)
  /pickup ID — take over a worker's session
  /commit [args] — commit the work with a model turn
  /stop — kill background processes
  /compact [focus | --preview] — summarize older rows
  /sendnow [text] — steer the running turn now
  /sendall — send everything queued now
  /cancel — drop the queued messages
  /status — session, model, mode and usage
  /quit — leave (/exit, /q)
  !cmd runs a shell command here · :q quits`

func (m *model) command(name, args, line string) tea.Cmd {
	switch name {
	case "quit", "exit", "q":
		return m.quit(false)
	}
	m.feed.note("⚙ "+line, true)
	c, sid := m.cl, m.sess.ID
	fail := func(m *model, err error) {
		m.feed.note("/"+name+": "+err.Error(), false)
	}
	switch name {
	case "help":
		m.feed.note(helpText, false)
	case "model":
		if args != "" {
			return m.set(map[string]any{"model": args}, "model: "+args+" (from the next turn)", name)
		}
		var res modelList
		return call(c, 15*time.Second, "model.list", map[string]any{"session": sid}, &res, func(m *model, err error) tea.Cmd {
			if err != nil {
				fail(m, err)
				return nil
			}
			lines := []string{"model: " + res.Current}
			for _, mod := range res.Models {
				if mod.Name == res.Current {
					lines = append(lines, "  ● "+mod.Name)
				} else {
					lines = append(lines, "    "+mod.Name)
				}
			}
			m.feed.note(strings.Join(lines, "\n"), false)
			return nil
		})
	case "reasoning":
		if args != "" {
			effort := args
			if effort == "default" {
				effort = ""
			}
			return m.set(map[string]any{"reasoning_effort": effort}, "reasoning: "+args+" (from the next turn)", name)
		}
		cur, mdl := m.sess.Effort, m.sess.Model
		var res modelList
		return call(c, 15*time.Second, "model.list", map[string]any{"session": sid}, &res, func(m *model, err error) tea.Cmd {
			if err != nil {
				fail(m, err)
				return nil
			}
			if cur == "" {
				cur = "default"
			}
			var efforts []string
			for _, mod := range res.Models {
				if mod.Name == mdl || (mdl == "" && mod.Name == res.Current) {
					efforts = mod.Efforts
				}
			}
			if len(efforts) == 0 {
				m.feed.note(modelOr(mdl, res.Current)+" has no configured reasoning levels", false)
			} else {
				m.feed.note("reasoning: "+cur+"  ("+strings.Join(efforts, " | ")+")", false)
			}
			return nil
		})
	case "perm":
		if args != "" {
			return m.set(map[string]any{"mode": args}, "permission mode: "+args, name)
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
			return m.switchSession(func(context.Context, *client.Client) (string, error) { return args, nil }, true, name)
		}
		var res struct {
			Sessions []map[string]any `json:"sessions"`
		}
		return call(c, 15*time.Second, "session.list", map[string]any{"limit": 10}, &res, func(m *model, err error) tea.Cmd {
			if err != nil {
				fail(m, err)
				return nil
			}
			if len(res.Sessions) == 0 {
				m.feed.note("no sessions to resume", false)
				return nil
			}
			var lines []string
			for _, s := range res.Sessions {
				title := str(s, "title")
				if title == "" {
					title = "untitled"
				}
				lines = append(lines, "  "+str(s, "id")+"  "+title+"  ·  "+str(s, "cwd"))
			}
			m.feed.note(strings.Join(lines, "\n"), false)
			return nil
		})
	case "pickup":
		return m.switchSession(func(ctx context.Context, c *client.Client) (string, error) {
			var res struct {
				Sessions []map[string]any `json:"sessions"`
			}
			if err := c.Call(ctx, "session.list", map[string]any{"kinds": []string{"worker"}}, &res); err != nil {
				return "", err
			}
			for _, s := range res.Sessions {
				if strings.EqualFold(str(s, "id"), args) || strings.EqualFold(str(s, "title"), args) {
					return str(s, "id"), nil
				}
			}
			return "", errNote("no worker " + args)
		}, true, name)
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
		dropped := len(m.queue)
		return call(c, 15*time.Second, "queue.clear", map[string]any{"session": sid}, nil,
			func(m *model, err error) tea.Cmd {
				if err != nil {
					fail(m, err)
				} else {
					m.feed.note(fmt.Sprintf("dropped %d queued message(s)", dropped), false)
				}
				return nil
			})
	case "status":
		s := m.sess
		m.feed.note(fmt.Sprintf("session %s · %s · mode %s · in %s out %s · %s calls",
			s.ID, s.Model, s.Mode, commas(s.In), commas(s.Out), commas(s.Calls)), false)
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
	} `json:"models"`
	Current string `json:"current"`
}

// set is session.set with the note to show once it took.
func (m *model) set(params map[string]any, ok, name string) tea.Cmd {
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
		if err := c.Call(ctx, "session.attach", map[string]any{"session": id, "as": "driver"}, &res); err != nil {
			if rpcCode(err) == codeNoSession {
				return fail(errNote("session " + id + " is not in soa-d"))
			}
			return fail(err)
		}
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
