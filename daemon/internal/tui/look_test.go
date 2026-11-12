package tui_test

// Phase 9b: the sidebar, the wordmark, markdown and surfaces
// (daemon/phases/09b-tui-look.md).

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/tui"
)

var ansiSeq = regexp.MustCompile(`\x1b\[[0-9;:?]*[ -/]*[@-~]`)

// sidebar is the screen's right 41 columns, after the border.
func sidebar(screen string) string {
	var out []string
	for _, l := range strings.Split(screen, "\n") {
		if r := []rune(l); len(r) > 119 {
			out = append(out, string(r[119:]))
		}
	}
	return strings.Join(out, "\n")
}

func TestMarkdownMatchesTheTextualFeed(t *testing.T) {
	b, err := os.ReadFile("testdata/markdown.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases map[string]map[string]json.RawMessage
	if err := json.Unmarshal(b, &cases); err != nil {
		t.Fatal(err)
	}
	for name, c := range cases {
		var src string
		if err := json.Unmarshal(c["source"], &src); err != nil {
			t.Fatal(err)
		}
		for _, w := range []string{"80", "40"} {
			var want []string
			if err := json.Unmarshal(c[w], &want); err != nil {
				t.Fatal(err)
			}
			width, _ := strconv.Atoi(w)
			var got []string
			for _, l := range tui.Markdown(src, width) {
				got = append(got, strings.TrimRight(ansiSeq.ReplaceAllString(l, ""), " "))
			}
			if strings.Join(got, "\n") != strings.Join(want, "\n") {
				t.Errorf("%s at %d:\ngot\n%s\nwant\n%s", name, width, strings.Join(got, "\n"), strings.Join(want, "\n"))
			}
		}
	}
}

func TestPaletteMatchesTheTextualTheme(t *testing.T) {
	b, err := os.ReadFile("testdata/palette.json")
	if err != nil {
		t.Fatal(err)
	}
	var want map[string]map[string]string
	if err := json.Unmarshal(b, &want); err != nil {
		t.Fatal(err)
	}
	for bg, w := range want {
		got := tui.Palette(bg)
		for k, v := range w {
			if !strings.EqualFold(got[k], v) {
				t.Errorf("Palette(%s)[%s] = %q, want %q", bg, k, got[k], v)
			}
		}
	}
}

const (
	wideFirst  = "███████╗  ██████╗  ███╗   ██╗      ██████╗  ███████╗       █████╗   ███╗   ██╗ ████████╗  ██████╗  ███╗   ██╗"
	antonFirst = " █████╗   ███╗   ██╗ ████████╗  ██████╗  ███╗   ██╗" // the stacked form's second word
)

func TestTheWordmarkFitsTheRoom(t *testing.T) {
	d := newDaemon(t, fakellm.New(t))
	a := open(t, d, tui.Options{})
	for _, c := range []struct {
		w    int
		form string
		col  int // the screen column it starts at: the 2-column inset plus the pad
	}{
		{120, wideFirst, 2 + (116-109)/2},
		{113, wideFirst, 2},
		{112, antonFirst, 2 + (108-52)/2},
		{100, antonFirst, 2 + (96-52)/2},
		{160, wideFirst, 2 + (114-109)/2}, // the sidebar takes 42
		{150, antonFirst, 2 + (104-52)/2},
		{55, "SON OF ANTON", 2 + (51-12)/2},
	} {
		a.Resize(c.w, 40)
		s := a.Screen()
		checkFrame(t, s, c.w, 40)
		line := lineWith(s, c.form)
		if line == "" {
			t.Fatalf("at %d columns the wordmark is not %q:\n%s", c.w, c.form, s)
		}
		if c.form != wideFirst && strings.Contains(s, wideFirst) {
			t.Fatalf("at %d columns the wide wordmark is drawn", c.w)
		}
		if col := len([]rune(line[:strings.Index(line, c.form)])); col != c.col {
			t.Fatalf("at %d columns the wordmark starts at column %d, want %d", c.w, col, c.col)
		}
		if !strings.Contains(strings.Join(strings.Split(s, "\n")[:3], "\n"), strings.TrimSpace(c.form)[:3]) {
			t.Fatalf("at %d columns a short feed does not start at the top:\n%s", c.w, s)
		}
	}
}

func TestTheSidebar(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "done", PromptTokens: 250, CompletionTokens: 50},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{{ID: "b1", Name: "Bash", Arguments: `{"command":"sleep 60","run_in_background":true}`}}},
		fakellm.Turn{Content: "started"},
	)
	d := newDaemon(t, fake)
	restartWith(t, d, "context_length = 1000\n")
	a := open(t, d, tui.Options{Model: "other-model", Yolo: true})
	a.Resize(160, 40)
	send(a, "fix the parser please")
	waitScreen(t, a, "done")
	idle(t, a)
	sess, _ := peek(t, d, a.Session())
	title, _ := sess["title"].(string)
	if title == "" {
		t.Fatal("the session has no title")
	}
	s := waitScreen(t, a, "▰▰▰▱▱▱▱▱▱▱  30%", "300 / 1.0K", "in 250 · out 50 · 1 calls")
	checkFrame(t, s, 160, 40)
	for i, l := range strings.Split(s, "\n") {
		if r := []rune(l); len(r) < 119 || r[118] != '│' {
			t.Fatalf("row %d has no sidebar border at column 118: %q", i, l)
		}
	}
	side := sidebar(s)
	for _, want := range []string{title, a.Session(), "mode", "yolo", "reasoning", "context", "session usage"} {
		if !strings.Contains(side, want) {
			t.Fatalf("the sidebar lacks %q:\n%s", want, side)
		}
	}
	if !regexp.MustCompile(`session \d+s`).MatchString(side) || strings.Contains(side, "background") {
		t.Fatalf("the sidebar's elapsed time, or a background row with nothing running:\n%s", side)
	}
	lines := strings.Split(side, "\n")
	if !strings.Contains(strings.Join(lines[len(lines)-3:], "\n"), "• Son of Anton test") {
		t.Fatalf("the footer is not at the bottom:\n%s", side)
	}

	send(a, "start a server")
	waitScreen(t, a, "started", "1 processes")
	if !strings.Contains(sidebar(a.Screen()), "background") {
		t.Fatal("no background label")
	}
	send(a, "/stop")
	waitScreen(t, a, "killed 1 background process(es)")
	waitGone(t, a, "1 processes")

	a.Resize(100, 30)
	waitGone(t, a, "session usage")
	a.Press("ctrl+b")
	s = waitScreen(t, a, "session usage")
	checkFrame(t, s, 100, 30)
	for i, l := range strings.Split(s, "\n") {
		if r := []rune(l); len(r) < 59 || r[58] != '│' {
			t.Fatalf("row %d: the toggled sidebar is not over the right 42 columns: %q", i, l)
		}
	}
	a.Press("ctrl+b")
	waitGone(t, a, "session usage")
	a.Resize(170, 40)
	if strings.Contains(a.Screen(), "session usage") {
		t.Fatal("a resize showed the sidebar after ctrl+b hid it")
	}
}

func TestNoContextLengthYet(t *testing.T) {
	d := newDaemon(t, fakellm.New(t))
	a := open(t, d, tui.Options{})
	a.Resize(160, 40)
	waitScreen(t, a, "▱▱▱▱▱▱▱▱▱▱  —", "no turns yet")
}

func TestAnswersRenderAsMarkdown(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "## Plan\n\n- **one**\n- two\n\n```\ncode here\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", ChunkRunes: 7})
	d := newDaemon(t, fake)
	a := open(t, d, tui.Options{})
	send(a, "plan it")
	s := waitScreen(t, a, "• one", "▎code here", "| a   | b   |", "| 1   | 2   |") // MT pads columns to 3
	if strings.Contains(s, "**one**") || strings.Contains(s, "## Plan") || !hasLine(s, "Plan") {
		t.Fatalf("markdown was drawn as source:\n%s", s)
	}
}

func TestSurfacesFollowTheTerminal(t *testing.T) {
	d := newDaemon(t, fakellm.New(t, fakellm.Turn{Content: "hello"}))
	a := open(t, d, tui.Options{Background: "#1e1e2e"})
	send(a, "hi")
	waitScreen(t, a, "hello")
	idle(t, a)
	st := a.Styled()
	if !strings.Contains(st, "48;2;44;44;67") || !strings.Contains(st, "48;2;51;51;78") {
		t.Fatal("the user row is not on the panel colour (#2c2c43) or the prompt on the surface (#33334e)")
	}
}

func TestAModalDimCoversTheWholeFeed(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{Content: "the earlier answer stays in the transcript above the dialog", ToolCalls: []fakellm.ToolCall{terminal("c1", "chmod 777 f.txt")}},
		fakellm.Turn{Content: "changed it"},
	)
	d := newDaemon(t, fake)
	if err := os.WriteFile(filepath.Join(d.cwd, "f.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	a := open(t, d, tui.Options{Background: "#1e1e2e"})
	a.Resize(100, 60)
	send(a, "make it writable")
	s := waitScreen(t, a, "The agent asks to run a command", "Allow once", "the earlier answer stays")
	checkFrame(t, s, 100, 60)
	// Textual's modal screen is `$background 60%`: everything behind the dialog
	// is dimmed, and the dialog is a card on `$panel`. The card is the dialog's
	// own width, with dim columns on either side.
	const dimSeq = "\x1b[48;2;37;37;56m"      // #252538 backdrop
	const cardSeq = "\x1b[48;2;44;44;67m"     // #2c2c43 panel
	const mutedSeq = "\x1b[38;2;169;169;169m" // #a9a9a9 text-muted
	st := a.Styled()
	if !strings.Contains(st, dimSeq) || !strings.Contains(st, cardSeq) {
		t.Fatal("no backdrop or dialog card in the styled frame")
	}
	// The earlier reply is dimmed but still readable, muted on the backdrop.
	row := lineWith(st, "the earlier answer stays")
	if row == "" || !strings.HasPrefix(row, dimSeq+mutedSeq) {
		t.Fatalf("the earlier reply is not muted on the backdrop: %q", row)
	}
	if !strings.Contains(s, "the earlier answer stays") {
		t.Fatal("dimming removed the earlier reply from the screen")
	}
	// On a dialog row: 12 dim columns, then the 76-column card, then 12 dim
	// columns (100-column screen, 76-wide box).
	box := lineWith(st, "The agent asks to run a command")
	if box == "" {
		t.Fatal("no dialog row")
	}
	edge := dimSeq + strings.Repeat(" ", 12) + "\x1b[0m"
	if !strings.HasPrefix(box, edge+cardSeq) {
		t.Fatalf("the dialog row's left edge is not 12 dim columns: %q", box)
	}
	if !strings.HasSuffix(box, edge) {
		t.Fatalf("the dialog row's right edge is not 12 dim columns: %q", box)
	}
	a.Press("1")
	waitScreen(t, a, "changed it")
}

func TestNarrowScreensKeepTheDialog(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{terminal("c1", "chmod 777 f.txt")}},
		fakellm.Turn{Content: "changed it"},
	)
	d := newDaemon(t, fake)
	if err := os.WriteFile(filepath.Join(d.cwd, "f.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	a := open(t, d, tui.Options{})
	send(a, "make it writable")
	waitScreen(t, a, "The agent asks to run a command")
	for w := 1; w <= 6; w++ {
		a.Resize(w, 30)
		scr := a.Screen()
		if strings.TrimSpace(scr) == "" || len(strings.Split(scr, "\n")) != 30 {
			t.Fatalf("at %d columns the screen is empty or not 30 rows:\n%q", w, scr)
		}
		sty := a.Styled()
		if strings.TrimSpace(ansiSeq.ReplaceAllString(sty, "")) == "" {
			t.Fatalf("at %d columns the styled frame is empty", w)
		}
	}
	a.Resize(100, 30)
	waitScreen(t, a, "The agent asks to run a command", "Allow once")
	a.Press("1")
	waitScreen(t, a, "changed it")

	b := open(t, d, tui.Options{})
	send(b, "/model")
	waitScreen(t, b, "Switch model  ·  now test-model")
	for w := 1; w <= 6; w++ {
		b.Resize(w, 30)
		scr := b.Screen()
		if strings.TrimSpace(scr) == "" || len(strings.Split(scr, "\n")) != 30 {
			t.Fatalf("picker at %d columns: the screen is empty or not 30 rows:\n%q", w, scr)
		}
		sty := b.Styled()
		if strings.TrimSpace(ansiSeq.ReplaceAllString(sty, "")) == "" {
			t.Fatalf("picker at %d columns: the styled frame is empty", w)
		}
	}
}

func TestARoutedModelReportsTheModelThatAnswered(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "routed", ServedModel: "radiance/small-model", PromptTokens: 300, CompletionTokens: 50})
	fake.SetModels("auto", "radiance/small-model")
	fake.SetModelMeta("auto", map[string]any{"context_length": 500000}) // the largest on offer
	fake.SetModelMeta("radiance/small-model", map[string]any{"context_length": 1000})
	d := newDaemon(t, fake)
	restartWith(t, d, "\n[custom_providers.custom.models.\"auto\"]\n")
	a := open(t, d, tui.Options{Model: "auto"})
	a.Resize(160, 40)
	send(a, "hi")
	waitScreen(t, a, "routed")
	idle(t, a)
	// The window, the bar and the counts are the answering model's, not auto's listing.
	waitScreen(t, a, "auto → small-model", "▰▰▰▰▱▱▱▱▱▱  35%", "350 / 1.0K")
	send(a, "/status")
	waitScreen(t, a, "· auto → radiance/small-model · mode")
}
