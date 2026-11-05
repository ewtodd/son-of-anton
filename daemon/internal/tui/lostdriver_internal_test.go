package tui

import (
	"strings"
	"testing"
)

func TestTheNoteSaysWhoTookTheSession(t *testing.T) {
	got := lostDriverNote("chat", "s1")
	if !strings.Contains(got, "fiona took over session s1") || !strings.Contains(got, "son-of-anton -r s1") {
		t.Fatalf("the chat's note = %q", got)
	}
	if got := lostDriverNote("", "s1"); !strings.HasPrefix(got, "Another terminal took over session s1") {
		t.Fatalf("a terminal's note = %q", got)
	}
}
