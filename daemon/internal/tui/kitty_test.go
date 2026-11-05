package tui_test

// ctrl+enter as a terminal delivers it: the TUI is wired as cmd/soa-d wires it,
// keys arrive as bytes and what it writes goes to a file. The escape sequences
// are what kitty sends with the keyboard protocol's disambiguate flag on (see
// kitty_internal_test.go).

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/client"
	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/tui"
)

const (
	ctrlEnter    = "\x1b[13;5u"
	protocolOn   = "\x1b[?1049h\x1b[>1u\x1b[?u"
	protocolOff  = "\x1b[<u\x1b[?1049l"
	protocolSays = "\x1b[?1u" // the terminal's answer to the query
)

type fakeTTY struct {
	*tui.App
	keys   *os.File
	stdout *os.File

	mu      sync.Mutex
	written bytes.Buffer
}

func (t *fakeTTY) Write(b []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.written.Write(b)
}

func (t *fakeTTY) output() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.written.String()
}

// press sends the bytes a terminal sends for a key.
func (t *fakeTTY) press(seq string) {
	_, _ = t.keys.WriteString(seq)
}

func openTerminal(t *testing.T, d *daemon) *fakeTTY {
	t.Helper()
	c, err := client.Dial(d.home, "tui")
	if err != nil {
		t.Fatal(err)
	}
	// Files, as a terminal is: the editor inherits them.
	in, keys, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	term := &fakeTTY{keys: keys, stdout: w}
	copied := make(chan struct{})
	go func() {
		defer close(copied)
		_, _ = io.Copy(term, r)
	}()
	a, err := tui.Start(c, tui.Options{Home: d.home, Cwd: d.cwd, Version: "test"}, in, w)
	if err != nil {
		t.Fatal(err)
	}
	term.App = a
	a.Resize(100, 30)
	t.Cleanup(func() {
		a.Press("ctrl+q", "3") // the 3 answers the quit question, if a turn runs
		waitExit(t, a)
		_ = keys.Close()
		_ = in.Close()
		_ = w.Close()
		<-copied
		_ = r.Close()
		c.Close()
	})
	return term
}

func TestCtrlEnterSendsTheDraftNow(t *testing.T) {
	rel := make(chan struct{})
	t.Cleanup(func() { close(rel) })
	fake := fakellm.New(t,
		fakellm.Turn{Content: "first answer", ChunkRunes: 6, Release: rel},
		fakellm.Turn{Content: "second answer"},
	)
	term := openTerminal(t, newDaemon(t, fake))
	send(term.App, "one")
	waitScreen(t, term.App, "first", "ctrl+c interrupt")
	term.Type("two")
	term.press(ctrlEnter)
	waitScreen(t, term.App, "second answer")
	if fake.Count() != 2 {
		t.Fatalf("%d requests", fake.Count())
	}
	if !bytes.Contains(fake.Bodies()[1], []byte(`"two"`)) {
		t.Fatalf("the draft is not in the next request: %s", fake.Bodies()[1])
	}
}

func TestCtrlEnterWithNoDraftSendsTheOldestQueued(t *testing.T) {
	rel := make(chan struct{})
	t.Cleanup(func() { close(rel) })
	fake := fakellm.New(t,
		fakellm.Turn{Content: "first answer", ChunkRunes: 6, Release: rel},
		fakellm.Turn{Content: "second answer"},
		fakellm.Turn{Content: "third answer"},
	)
	term := openTerminal(t, newDaemon(t, fake))
	send(term.App, "one")
	waitScreen(t, term.App, "first", "ctrl+c interrupt")
	send(term.App, "two")
	send(term.App, "three")
	waitScreen(t, term.App, "↳ two", "↳ three", "2 queued")
	term.press(ctrlEnter)
	waitScreen(t, term.App, "second answer", "third answer")
	if fake.Count() != 3 {
		t.Fatalf("%d requests", fake.Count())
	}
	if b := fake.Bodies()[1]; !bytes.Contains(b, []byte(`"two"`)) || bytes.Contains(b, []byte(`"three"`)) {
		t.Fatalf("the oldest alone goes first: %s", b)
	}
}

func TestCtrlEnterOverACompletionListIsEnter(t *testing.T) {
	term := openTerminal(t, newDaemon(t, fakellm.New(t)))
	term.Type("/help")
	waitScreen(t, term.App, "/help")
	term.press(ctrlEnter)
	waitScreen(t, term.App, "/quit — Leave the TUI")
}

func TestKeysStillWorkUnderTheProtocol(t *testing.T) {
	for name, key := range map[string]string{"esc": "\x1b[27u", "ctrl+c": "\x1b[99;5u"} {
		t.Run(name, func(t *testing.T) {
			rel := make(chan struct{})
			t.Cleanup(func() { close(rel) })
			fake := fakellm.New(t, fakellm.Turn{Content: "slow answer", ChunkRunes: 5, Release: rel})
			term := openTerminal(t, newDaemon(t, fake))
			send(term.App, "one")
			waitScreen(t, term.App, "slow", "ctrl+c interrupt")
			term.press(key)
			waitGone(t, term.App, "ctrl+c interrupt")
		})
	}
}

// A terminal without the protocol sends enter for ctrl+enter, so the hint
// offers the key only once the terminal has said it has the protocol.
func TestTheQueueHintOffersCtrlEnterWhenTheTerminalHasIt(t *testing.T) {
	rel := make(chan struct{})
	t.Cleanup(func() { close(rel) })
	fake := fakellm.New(t,
		fakellm.Turn{Content: "first answer", ChunkRunes: 6, Release: rel},
	)
	term := openTerminal(t, newDaemon(t, fake))
	send(term.App, "one")
	waitScreen(t, term.App, "first", "ctrl+c interrupt")
	send(term.App, "two")
	waitScreen(t, term.App, "/sendnow sends this now")
	term.press(protocolSays)
	waitScreen(t, term.App, "ctrl+enter or /sendnow to send now")
	send(term.App, "three")
	waitScreen(t, term.App, "ctrl+enter or /sendnow to send the oldest · /sendall to send all")
}

func TestTheProtocolIsOffWhileTheEditorRuns(t *testing.T) {
	dir := t.TempDir()
	stdoutWas := filepath.Join(dir, "stdout")
	editor := filepath.Join(dir, "editor.sh")
	script := "#!/bin/sh\nterm=$(readlink /proc/$$/fd/1)\necho \"$term\" > " + stdoutWas + ".tmp\nmv " + stdoutWas + ".tmp " + stdoutWas + "\n: > \"$1\"\n"
	if err := os.WriteFile(editor, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("VISUAL", editor)
	term := openTerminal(t, newDaemon(t, fakellm.New(t)))
	waitScreen(t, term.App, "/help")
	term.Press("ctrl+g")
	deadline := time.Now().Add(waitFor)
	var got []byte
	for {
		var err error
		if got, err = os.ReadFile(stdoutWas); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the editor did not run")
		}
		time.Sleep(10 * time.Millisecond)
	}
	want, err := os.Readlink(fmt.Sprintf("/proc/self/fd/%d", term.stdout.Fd()))
	if err != nil {
		t.Fatal(err)
	}
	if strings.TrimSpace(string(got)) != want {
		t.Fatalf("the editor's stdout is %s, not the terminal (%s)", got, want)
	}
	term.Press("ctrl+q")
	waitExit(t, term.App)

	out := term.output()
	if n := strings.Count(out, "\x1b[?1049h"); n != 2 || strings.Count(out, protocolOn) != n {
		t.Fatalf("entered the alternate screen %d times, with the protocol %d times", n, strings.Count(out, protocolOn))
	}
	if n := strings.Count(out, "\x1b[?1049l"); n != 2 || strings.Count(out, protocolOff) != n {
		t.Fatalf("left the alternate screen %d times, with the protocol off %d times", n, strings.Count(out, protocolOff))
	}
	at := 0
	for _, want := range []string{protocolOn, protocolOff, protocolOn, protocolOff} {
		i := strings.Index(out[at:], want)
		if i < 0 {
			t.Fatalf("%q does not follow at %d", want, at)
		}
		at += i + len(want)
	}
}
