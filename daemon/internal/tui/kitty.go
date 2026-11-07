package tui

import (
	"os"
	"reflect"
	"regexp"
	"strconv"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
)

// A terminal left alone sends the same byte for enter and ctrl+enter, so the
// key for /sendnow needs the kitty keyboard protocol
// (sw.kovidgoyal.net/kitty/keyboard-protocol). Its disambiguate flag tells
// them apart, and also reports every other ctrl and alt key, esc, shift+tab
// and the keypad as CSI number ; modifiers u. bubbletea v1 does not read those:
// kittyFilter turns each back into the key it is without the protocol.

const (
	altScreenOn  = "\x1b[?1049h"
	altScreenOff = "\x1b[?1049l"
	kittyPush    = "\x1b[>1u" // the disambiguate flag
	kittyQuery   = "\x1b[?u"  // a terminal with the protocol answers CSI ? flags u
	kittyPop     = "\x1b[<u"
)

const (
	kittyShift = 1 << iota
	kittyAlt
	kittyCtrl
	kittySuper
	kittyHyper
	kittyMeta
)

const kittyKeypadEnter = 57414

// kittyKeypad is the keypad with num lock off.
var kittyKeypad = map[int]tea.KeyType{
	57417: tea.KeyLeft, 57418: tea.KeyRight, 57419: tea.KeyUp, 57420: tea.KeyDown,
	57421: tea.KeyPgUp, 57422: tea.KeyPgDown, 57423: tea.KeyHome, 57424: tea.KeyEnd,
	57425: tea.KeyInsert, 57426: tea.KeyDelete,
}

// ctrlEnterMsg is ctrl+enter, which has no tea.KeyType.
type ctrlEnterMsg struct{}

// kittyKeysMsg says the terminal has the protocol, so ctrl+enter works.
type kittyKeysMsg struct{}

// kittyOutput is the terminal bubbletea draws on, with the protocol on exactly
// while the alternate screen is. Each screen keeps its own flags, so it has to
// go off before the screen is left: whatever runs next there, like the editor,
// would get keys it cannot read. Embedding the file keeps it a terminal to
// bubbletea, but never to os/exec, so an editor is given the file itself.
type kittyOutput struct{ *os.File }

func (o kittyOutput) Write(b []byte) (int, error) {
	if _, err := o.WriteString(string(b)); err != nil {
		return 0, err
	}
	return len(b), nil
}

func (o kittyOutput) WriteString(s string) (int, error) {
	seq := strings.ReplaceAll(s, altScreenOff, kittyPop+altScreenOff)
	seq = strings.ReplaceAll(seq, altScreenOn, altScreenOn+kittyPush+kittyQuery)
	if _, err := o.File.WriteString(seq); err != nil {
		return 0, err
	}
	return len(s), nil
}

// kittySeqRE is one CSI sequence, as far as the keyboard protocol's go: the
// parameters, the intermediates, the final byte.
var kittySeqRE = regexp.MustCompile(`^\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]`)

// kittyFilter is the program's filter. bubbletea hands over a sequence it does
// not know as an unexported []byte type. A read chunk can hold more than the
// sequence — a key typed microseconds after it — so the filter decodes the
// prefix and sends the rest on, or the tail would vanish inside the unknown
// message.
func kittyFilter(m tea.Model, msg tea.Msg) tea.Msg {
	v := reflect.ValueOf(msg)
	if v.Kind() != reflect.Slice || v.Type().Name() != "unknownCSISequenceMsg" {
		return msg
	}
	seq := string(v.Bytes())
	loc := kittySeqRE.FindStringIndex(seq)
	if loc == nil {
		return msg
	}
	if decoded := kittyMsg(seq[:loc[1]]); decoded != nil {
		if rest := seq[loc[1]:]; rest != "" {
			if mm, ok := m.(*model); ok && mm.p != nil {
				r := reflect.MakeSlice(v.Type(), len(rest), len(rest))
				reflect.Copy(r, reflect.ValueOf([]byte(rest)))
				mm.p.Send(r.Interface())
			}
		}
		return decoded
	}
	return msg
}

// kittyMsg decodes CSI number ; modifiers u, or the answer to the query; nil
// for anything else, and for keys that mean nothing without the protocol.
func kittyMsg(seq string) tea.Msg {
	body, ok := strings.CutPrefix(seq, "\x1b[")
	if !ok {
		return nil
	}
	if body, ok = strings.CutSuffix(body, "u"); !ok {
		return nil
	}
	if flags, ok := strings.CutPrefix(body, "?"); ok {
		if n, err := strconv.Atoi(flags); err == nil && n&1 != 0 {
			return kittyKeysMsg{}
		}
		return nil
	}
	number, modifiers, _ := strings.Cut(body, ";")
	code, err := strconv.Atoi(number)
	if err != nil {
		return nil
	}
	mods := 0
	if modifiers != "" {
		n, err := strconv.Atoi(modifiers)
		if err != nil {
			return nil
		}
		mods = n - 1 // caps lock and num lock are the bits above these, and change nothing here
	}
	if mods&(kittySuper|kittyHyper|kittyMeta) != 0 {
		return nil
	}
	shift, alt, ctrl := mods&kittyShift != 0, mods&kittyAlt != 0, mods&kittyCtrl != 0
	key := func(t tea.KeyType) tea.Msg { return tea.KeyMsg{Type: t, Alt: alt} }
	switch code {
	case '\r', kittyKeypadEnter:
		if ctrl && !alt && !shift {
			return ctrlEnterMsg{}
		}
		// shift+enter is a newline in the draft, the same KeyMsg alt+enter
		// says without the protocol. Without the protocol the terminal
		// sends a bare enter for it and it cannot be told apart.
		if shift {
			return tea.KeyMsg{Type: tea.KeyEnter, Alt: true}
		}
		return key(tea.KeyEnter)
	case '\t':
		if shift {
			return key(tea.KeyShiftTab)
		}
		return key(tea.KeyTab)
	case 0x1b:
		return key(tea.KeyEsc)
	case 0x7f:
		if ctrl {
			return key(tea.KeyCtrlH)
		}
		return key(tea.KeyBackspace)
	case ' ':
		if ctrl {
			return key(tea.KeyCtrlAt)
		}
		return tea.KeyMsg{Type: tea.KeySpace, Runes: []rune{' '}, Alt: alt}
	}
	if t, ok := kittyKeypad[code]; ok {
		return key(t)
	}
	switch {
	case ctrl && !shift && code >= '@' && code <= '~':
		return key(tea.KeyType(code & 0x1f))
	case alt && !ctrl && !shift && code > ' ' && code <= '~':
		return tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{rune(code)}, Alt: true}
	}
	return nil
}
