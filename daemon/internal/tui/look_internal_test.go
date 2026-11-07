package tui

// Phase 9b helpers that need no daemon: what the terminal's reply and a
// session's age are read as.

import (
	"testing"
	"time"
)

func TestOSCReplies(t *testing.T) {
	for reply, want := range map[string]string{
		"\x1b]11;rgb:1e1e/1e1e/2e2e\x1b\\":     "#1e1e2e",
		"\x1b]11;rgb:ff/00/80\x07":             "#ff0080",
		"\x1b]11;rgba:ffff/ffff/ffff/ffff\x07": "#ffffff",
		"\x1b]11;#282c34\x07":                  "#282c34",
		"":                                     "",
		"\x1b]11;nonsense\x07":                 "",
	} {
		if got := parseOSCColor(reply); got != want {
			t.Errorf("parseOSCColor(%q) = %q, want %q", reply, got, want)
		}
	}
}

func TestSessionAges(t *testing.T) {
	for d, want := range map[time.Duration]string{
		45 * time.Second: "45s", 12 * time.Minute: "12m", 3*time.Hour + 5*time.Minute: "3h 5m", 52 * time.Hour: "2d 4h",
	} {
		if got := fmtDuration(d); got != want {
			t.Errorf("fmtDuration(%v) = %q, want %q", d, got, want)
		}
	}
}
