package tui

import (
	"fmt"
	"io"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

// kittySends is what kitty 0.49.2 sends with the disambiguate flag on, taken
// from its own encoder (kitty.fast_data_types.encode_key_for_tty with
// key_encoding_flags=1), and the key each must arrive as: what the same press
// is without the protocol.
var kittySends = []struct{ seq, want string }{
	{"\x1b[13;5u", "ctrl+enter"},
	{"\x1b[13;133u", "ctrl+enter"}, // num lock on
	{"\x1b[13;69u", "ctrl+enter"},  // caps lock on
	{"\x1b[57414;5u", "ctrl+enter"},
	{"\x1b[13;2u", "enter"},
	{"\x1b[13;6u", "enter"},
	{"\x1b[13;3u", "alt+enter"},
	{"\x1b[57414u", "enter"},
	{"\x1b[9;2u", "shift+tab"},
	{"\x1b[9;5u", "tab"},
	{"\x1b[27u", "esc"},
	{"\x1b[27;2u", "esc"},
	{"\x1b[27;3u", "alt+esc"},
	{"\x1b[91;5u", "esc"}, // ctrl+[
	{"\x1b[127;5u", "ctrl+h"},
	{"\x1b[127;2u", "backspace"},
	{"\x1b[127;3u", "alt+backspace"},
	{"\x1b[32;5u", "ctrl+@"},
	{"\x1b[32;3u", "alt+ "},
	{"\x1b[97;5u", "ctrl+a"},
	{"\x1b[97;133u", "ctrl+a"},
	{"\x1b[97;69u", "ctrl+a"},
	{"\x1b[98;5u", "ctrl+b"},
	{"\x1b[99;5u", "ctrl+c"},
	{"\x1b[100;5u", "ctrl+d"},
	{"\x1b[103;5u", "ctrl+g"},
	{"\x1b[106;5u", "ctrl+j"},
	{"\x1b[108;5u", "ctrl+l"},
	{"\x1b[112;5u", "ctrl+p"},
	{"\x1b[113;5u", "ctrl+q"},
	{"\x1b[120;5u", "ctrl+x"},
	{"\x1b[98;7u", "alt+ctrl+b"},
	{"\x1b[98;3u", "alt+b"},
	{"\x1b[93;5u", "ctrl+]"},
	{"\x1b[92;5u", "ctrl+\\"},
	{"\x1b[57417u", "left"},
	{"\x1b[57418u", "right"},
	{"\x1b[57419u", "up"},
	{"\x1b[57420u", "down"},
	{"\x1b[57421u", "pgup"},
	{"\x1b[57422u", "pgdown"},
	{"\x1b[57423u", "home"},
	{"\x1b[57424u", "end"},
	{"\x1b[57425u", "insert"},
	{"\x1b[57426u", "delete"},
}

type keyProbe struct{ got chan string }

func (p keyProbe) Init() tea.Cmd { return nil }
func (p keyProbe) View() string  { return "" }
func (p keyProbe) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.KeyMsg:
		p.got <- msg.String()
	case ctrlEnterMsg:
		p.got <- "ctrl+enter"
	case kittyKeysMsg:
		p.got <- "the protocol"
	}
	return p, nil
}

// The bytes go through bubbletea's own reader, so a bubbletea that stops
// handing over sequences it does not know fails here.
func TestWhatKittySendsArrivesAsTheKeyItIs(t *testing.T) {
	in, keys := io.Pipe()
	defer keys.Close()
	probe := keyProbe{make(chan string)}
	p := tea.NewProgram(probe, tea.WithInput(in), tea.WithOutput(io.Discard), tea.WithoutRenderer(),
		tea.WithoutSignalHandler(), tea.WithFilter(kittyFilter))
	done := make(chan error, 1)
	go func() {
		_, err := p.Run()
		done <- err
	}()
	defer func() {
		p.Quit()
		<-done
	}()
	check := func(seq, want string) {
		t.Helper()
		go func() { _, _ = io.WriteString(keys, seq) }()
		select {
		case got := <-probe.got:
			if got != want {
				t.Errorf("%q arrived as %q, want %q", seq, got, want)
			}
		case <-time.After(5 * time.Second):
			t.Fatalf("%q never arrived, want %q", seq, want)
		}
	}
	for _, k := range kittySends {
		check(k.seq, k.want)
	}
	check("\x1b[?1u", "the protocol")
}

func TestWhatMeansNothingWithoutTheProtocolStaysNothing(t *testing.T) {
	for _, seq := range []string{
		"\x1b[99;6u",  // ctrl+shift+c
		"\x1b[98;4u",  // alt+shift+b
		"\x1b[99;9u",  // super+c
		"\x1b[?0u",    // a terminal with the protocol, not on
		"\x1b[1;5D",   // not this form
		"\x1b[x;5u",   // not a number
		"\x1b[99;xu",  // not a number
		"\x1b[99999u", // a key beyond the table
	} {
		if got := kittyMsg(seq); got != nil {
			t.Errorf("kittyMsg(%q) = %s", seq, fmt.Sprint(got))
		}
	}
}
