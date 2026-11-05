package tui_test

import (
	"bytes"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/signalfake"
	"github.com/ewtodd/son-of-anton/daemon/internal/tui"
)

func TestHandoffClosesTheTUIAndSaysHowToComeBack(t *testing.T) {
	const account, group = "+15550000000", "gW"
	sig := signalfake.New(t)
	t.Setenv("SIGNAL_HTTP_URL", sig.URL)
	t.Setenv("SIGNAL_ACCOUNT", account)
	t.Setenv("SIGNAL_ALLOWED_USERS", account)
	t.Setenv("SIGNAL_GROUP_ALLOWED_USERS", group)
	t.Setenv("SIGNAL_DM_MODE", "ignore")
	t.Setenv("SIGNAL_REQUIRE_MENTION", "")
	t.Setenv("SON_OF_ANTON_GATEWAY_LOCK_DIR", t.TempDir())
	fake := fakellm.New(t,
		fakellm.Turn{When: "You never run commands", Content: "Hello."},
		fakellm.Turn{When: "start at the desk", Content: "desk work done"},
		fakellm.Turn{When: "[worker ", Content: "It is with me now."},
	)
	d := newDaemon(t, fake)
	restartWith(t, d, "\n[daemon]\nsignal = true\n\n[gateway]\nsingle_user = true\n")
	sig.WaitConnected(t)
	sig.Push(signalfake.OwnerInGroup(account, group, "hello", 1_700_000_000_001))
	said := func(text string) func(signalfake.Sent) bool {
		return func(m signalfake.Sent) bool { return m.Text == text }
	}
	sig.WaitSend(t, "the greeting", said("Hello."))

	var out bytes.Buffer
	a := open(t, d, tui.Options{Stderr: &out})
	send(a, "start at the desk")
	waitScreen(t, a, "desk work done")
	idle(t, a)
	id := a.Session()
	send(a, "/handoff")
	waitExit(t, a)
	if got := out.String(); !strings.Contains(got, "fiona") || !strings.Contains(got, "son-of-anton -r "+id) {
		t.Fatalf("the terminal says %q", got)
	}
	sig.WaitSend(t, "fiona's word", said("It is with me now."))
}

func TestHandoffSaysWhyItCannot(t *testing.T) {
	fake := fakellm.New(t)
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "/handoff")
	waitScreen(t, a, "/handoff: ", "Signal")
	idle(t, a) // still here
}
