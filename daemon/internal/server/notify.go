// Desktop nudges: an approval or a clarify question that waits unanswered
// pokes the user with notify-send, so a turn blocked in another window or
// on another surface is not forgotten.
package server

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/exec"
	"strings"
	"time"
)

const (
	nudgeTitle = "Son of Anton is waiting for your answer"
	nudgeMax   = 200 // characters of body, as the Python's nudge kept
)

// runNotify is the desktop nudge: notify-send, best effort. A host without
// the binary or without a notification daemon stays quiet.
func runNotify(title, body string) {
	if _, err := exec.LookPath("notify-send"); err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "notify-send", "--app-name=Son of Anton", title, body)
	cmd.Env = dbusEnv(os.Environ(), os.Getuid())
	if out, err := cmd.CombinedOutput(); err != nil {
		log.Printf("notify-send: %v: %s", err, out)
	}
}

// dbusEnv fills in the user's session bus: a systemd system unit runs with
// no DBUS_SESSION_BUS_ADDRESS, and desktop notifications only exist there.
// A bus the instance cannot reach stays a quiet no-op — the nudge is best
// effort.
func dbusEnv(env []string, uid int) []string {
	for _, kv := range env {
		if strings.HasPrefix(kv, "DBUS_SESSION_BUS_ADDRESS=") {
			return env
		}
	}
	return append(env, fmt.Sprintf("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%d/bus", uid))
}

// armNudge starts the desktop nudge for a pending approval or question: if
// it still waits after agent.prompt_notify_after, notify-send taps the
// user's shoulder (best effort; 0 or less never nudges). mu held.
func (l *live) armNudge(id, text string) {
	secs := l.srv.cfg.PromptNotifyAfter()
	if secs <= 0 {
		return
	}
	body := nudgeBody(text)
	l.nudges[id] = time.AfterFunc(time.Duration(secs)*time.Second, func() {
		l.mu.Lock()
		_, pending := l.nudges[id]
		delete(l.nudges, id)
		l.mu.Unlock()
		if pending {
			l.srv.notify(nudgeTitle, body)
		}
	})
}

// nudgeBody is a body notify-send can show: whitespace squashed, cut short.
func nudgeBody(s string) string {
	out, n := make([]rune, 0, len(s)), 0
	space := false
	for _, r := range s {
		if r == ' ' || r == '\t' || r == '\n' || r == '\r' {
			space = true
			continue
		}
		if space && len(out) > 0 {
			out, n = append(out, ' '), n+1
		}
		space = false
		out, n = append(out, r), n+1
		if n >= nudgeMax-1 {
			out = append(out, '…')
			break
		}
	}
	return string(out)
}
