package tui

import (
	"fmt"
	"io"
	"math"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/x/term"
	"github.com/muesli/termenv"
	"golang.org/x/sys/unix"
)

// themeColors are the colour roles of the one theme: the ANSI colours the
// screen has always defaulted to, on whatever the terminal's own background
// yields as surfaces.
var themeColors = map[string]lipgloss.TerminalColor{
	"ui_accent": lipgloss.Color("3"), "ui_thinking": lipgloss.Color("6"), "ui_tool": lipgloss.Color("3"),
	"ui_ok": lipgloss.Color("2"), "ui_warn": lipgloss.Color("3"), "ui_error": lipgloss.Color("1"),
}

// theme is what the screen is drawn with: the theme's colours, the surfaces,
// and the renderer they are drawn through.
type theme struct {
	r *lipgloss.Renderer

	pal   map[string]string // Palette of the terminal's background, or nil
	exact *strings.Replacer // corrects termenv's hex colours; nil when none needs it

	accent, thinking, tool, okC, warn, errC lipgloss.TerminalColor
	panel                                   lipgloss.TerminalColor
	surface                                 lipgloss.TerminalColor // nil: no generated surface
	backdrop                                lipgloss.TerminalColor // the dim card behind a dialog
	card                                    lipgloss.TerminalColor // a dialog's own surface

	acc, think, toolS, okS, warnS, errS, mut, bld, plain lipgloss.Style
}

// newTheme builds the one theme. pal is Palette's answer for the terminal's
// background, or nil when it is not known.
func newTheme(r *lipgloss.Renderer, pal map[string]string) *theme {
	th := &theme{r: r, pal: pal,
		accent: themeColors["ui_accent"], thinking: themeColors["ui_thinking"], tool: themeColors["ui_tool"],
		okC: themeColors["ui_ok"], warn: themeColors["ui_warn"], errC: themeColors["ui_error"]}
	fg := func(c lipgloss.TerminalColor) lipgloss.Style { return r.NewStyle().Foreground(c) }
	th.acc, th.think, th.toolS = fg(th.accent), fg(th.thinking), fg(th.tool)
	th.okS, th.warnS, th.errS = fg(th.okC), fg(th.warn), fg(th.errC)
	th.bld, th.plain = r.NewStyle().Bold(true), r.NewStyle()
	th.panel = lipgloss.AdaptiveColor{Light: "254", Dark: "236"}
	th.mut = r.NewStyle().Faint(true)
	if pal != nil {
		th.panel = lipgloss.Color(pal["panel"])
		th.surface = lipgloss.Color(pal["surface"])
		th.mut = fg(lipgloss.Color(pal["text-muted"]))
		th.backdrop, th.card = lipgloss.Color(pal["backdrop"]), lipgloss.Color(pal["panel"])
	} else {
		th.backdrop = lipgloss.AdaptiveColor{Light: "255", Dark: "233"}
		th.card = th.panel
	}
	var hexes []string
	for _, c := range []lipgloss.TerminalColor{th.accent, th.thinking, th.tool, th.okC, th.warn, th.errC, th.panel, th.card, th.backdrop} {
		if s, ok := c.(lipgloss.Color); ok && strings.HasPrefix(string(s), "#") {
			hexes = append(hexes, string(s))
		}
	}
	if pal != nil {
		hexes = append(hexes, pal["panel"], pal["surface"], pal["text-muted"], pal["backdrop"])
	}
	if r.ColorProfile() == termenv.TrueColor {
		if pairs := exactSeqs(hexes); len(pairs) > 0 {
			th.exact = strings.NewReplacer(pairs...)
		}
	}
	return th
}

// bgSeq is the escape that starts background c, or "" when nothing is coloured.
func (th *theme) bgSeq(c lipgloss.TerminalColor) string {
	s := th.r.NewStyle().Background(c).Render("x")
	if i := strings.Index(s, "x"); i > 0 {
		return s[:i]
	}
	return ""
}

// paint draws line on background c, filling it to w columns: the background
// is started again after every reset inside the line.
func (th *theme) paint(line string, c lipgloss.TerminalColor, w int) string {
	seq := th.bgSeq(c)
	gap := strings.Repeat(" ", max(w-lipgloss.Width(line), 0))
	if seq == "" {
		return line + gap
	}
	return seq + strings.ReplaceAll(line, "\x1b[0m", "\x1b[0m"+seq) + gap + "\x1b[0m"
}

var (
	mdThemeOnce sync.Once
	mdTheme     *theme
)

// markdownTheme is the theme Markdown draws with: no palette.
func markdownTheme() *theme {
	mdThemeOnce.Do(func() {
		r := lipgloss.NewRenderer(io.Discard)
		r.SetColorProfile(termenv.TrueColor)
		r.SetHasDarkBackground(true)
		mdTheme = newTheme(r, nil)
	})
	return mdTheme
}

// isTerminal reports whether w is a terminal.
func isTerminal(w io.Writer) bool {
	f, ok := w.(*os.File)
	return ok && term.IsTerminal(f.Fd())
}

// newRenderer is the renderer of out: its own colour profile when out is a
// terminal, else true colour, so colours read the same everywhere else.
func newRenderer(out io.Writer, dark bool) *lipgloss.Renderer {
	r := lipgloss.NewRenderer(out)
	if !isTerminal(out) {
		r.SetColorProfile(termenv.TrueColor)
	}
	r.SetHasDarkBackground(dark)
	return r
}

// queryBackground asks the terminal for its background as #rrggbb (OSC 11,
// 120ms): "" when out is not a terminal, the query is turned off, or the
// terminal does not answer with a colour.
func queryBackground(out io.Writer) string {
	if !isTerminal(out) {
		return ""
	}
	switch strings.ToLower(strings.TrimSpace(os.Getenv("SON_OF_ANTON_TUI_NO_COLOR_QUERY"))) {
	case "1", "true", "yes", "on":
		return ""
	}
	f := out.(*os.File)
	state, err := term.MakeRaw(f.Fd())
	if err != nil {
		return ""
	}
	defer func() { _ = term.Restore(f.Fd(), state) }()
	return parseOSCColor(askTerminal(f, "\x1b]11;?\x1b\\", 120*time.Millisecond))
}

// askTerminal sends query to the terminal f and returns its reply: what
// arrives within timeout, up to the BEL or ST that ends it.
func askTerminal(f *os.File, query string, timeout time.Duration) string {
	if _, err := f.WriteString(query); err != nil {
		return ""
	}
	fd := int(f.Fd())
	deadline := time.Now().Add(timeout)
	var reply []byte
	buf := make([]byte, 64)
	for {
		left := time.Until(deadline)
		if left <= 0 {
			break
		}
		var set unix.FdSet
		set.Set(fd)
		tv := unix.NsecToTimeval(left.Nanoseconds())
		n, err := unix.Select(fd+1, &set, nil, nil, &tv)
		if err == unix.EINTR {
			continue
		}
		if err != nil || n == 0 {
			break
		}
		n, err = unix.Read(fd, buf)
		if err != nil || n <= 0 {
			break
		}
		reply = append(reply, buf[:n]...)
		if s := string(reply); strings.Contains(s, "\x07") || strings.Contains(s, "\x1b\\") {
			break
		}
	}
	return string(reply)
}

var (
	oscRGB = regexp.MustCompile(`rgba?:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})`)
	oscHex = regexp.MustCompile(`#([0-9a-fA-F]{6})`)
)

// parseOSCColor reads an OSC 10/11 reply into #rrggbb: "rgb:RRRR/GGGG/BBBB"
// with 1 to 4 digits a channel, or "#rrggbb". "" when it is neither.
func parseOSCColor(payload string) string {
	if m := oscRGB.FindStringSubmatch(payload); m != nil {
		var ch [3]int
		for i := range ch {
			v, _ := strconv.ParseUint(m[i+1], 16, 32)
			scale := float64(uint64(1)<<(4*len(m[i+1])) - 1)
			ch[i] = int(math.RoundToEven(float64(v) * 255 / scale))
		}
		return hexOf(ch[0], ch[1], ch[2])
	}
	if m := oscHex.FindStringSubmatch(payload); m != nil {
		return strings.ToLower("#" + m[1])
	}
	return ""
}

// startTheme is the theme the app starts in: the one theme, on the surfaces
// of the terminal's background.
func startTheme(o Options, out io.Writer) *theme {
	bg := o.Background
	if bg == "" {
		bg = queryBackground(out)
	}
	pal := Palette(bg)
	dark := lipgloss.HasDarkBackground() // bubbletea has already asked the terminal
	if r, g, b, ok := parseHex(bg); ok {
		dark = luminance(r, g, b) <= 127.5
	}
	return newTheme(newRenderer(out, dark), pal)
}

// exactSeqs lists, for each colour #rrggbb in hexes, the escape lipgloss writes
// for it and the one that is right, when they differ. termenv writes a hex
// channel as uint8(v/255*255), which is one short of v for some values (0x4a
// comes out 73), and no hex string gives v back; the screen is corrected after
// it is drawn so the terminal receives the colour that was asked for.
func exactSeqs(hexes []string) []string {
	var pairs []string
	for _, hex := range hexes {
		r, g, b, ok := parseHex(hex)
		if !ok {
			continue
		}
		for _, bg := range []bool{false, true} {
			got := termenv.RGBColor(hex).Sequence(bg)
			want := fmt.Sprintf("38;2;%d;%d;%d", r, g, b)
			if bg {
				want = fmt.Sprintf("48;2;%d;%d;%d", r, g, b)
			}
			if got != want {
				for _, end := range []string{"m", ";"} { // an escape ends, or goes on with another attribute
					pairs = append(pairs, got+end, want+end)
				}
			}
		}
	}
	return pairs
}

// fix corrects the colours of a drawn row (see exactSeqs).
func (th *theme) fix(s string) string {
	if th.exact == nil || !strings.Contains(s, "\x1b[") {
		return s
	}
	return th.exact.Replace(s)
}
