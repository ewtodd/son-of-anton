package tui

import (
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/BurntSushi/toml"
	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/x/term"
	"github.com/muesli/termenv"
	"golang.org/x/sys/unix"

	"github.com/ewtodd/son-of-anton/daemon/internal/config"
)

// skinKeys are the colour keys of a skin (son_of_anton_cli/skin_engine.py).
var skinKeys = []string{"ui_accent", "ui_thinking", "ui_tool", "ui_ok", "ui_warn", "ui_error"}

type skinDef struct {
	name, desc string
	colors     map[string]string
}

// builtinSkins are the built-in skins, in the order /skin lists them. A key a
// skin leaves out comes from the first, "default".
var builtinSkins = []skinDef{
	{"default", "Son of Anton — obsidian and gold", map[string]string{
		"ui_accent": "yellow", "ui_thinking": "cyan", "ui_tool": "yellow", "ui_ok": "green", "ui_warn": "yellow", "ui_error": "red"}},
	{"ares", "War-god theme — crimson and bronze", map[string]string{"ui_accent": "#DD4A3A"}},
	{"mono", "Monochrome — clean grayscale", map[string]string{
		"ui_accent": "#aaaaaa", "ui_ok": "#888888", "ui_error": "#cccccc", "ui_warn": "#999999"}},
	{"slate", "Cool blue — developer-focused", map[string]string{
		"ui_accent": "#7eb8f6", "ui_ok": "#63D0A6", "ui_error": "#F7A072", "ui_warn": "#e6a855"}},
	{"daylight", "Light theme for bright terminals with dark text and cool blue accents", map[string]string{
		"ui_accent": "#2563EB", "ui_ok": "#15803D", "ui_error": "#B91C1C", "ui_warn": "#B45309"}},
	{"warm-lightmode", "Warm light mode — dark brown/gold text for light terminal backgrounds", map[string]string{
		"ui_accent": "#8B4513", "ui_warn": "#E65100"}},
	{"poseidon", "Ocean-god theme — deep blue and seafoam", map[string]string{"ui_accent": "#5DB8F5"}},
	{"sisyphus", "Sisyphean theme — austere grayscale with persistence", map[string]string{
		"ui_accent": "#E7E7E7", "ui_ok": "#919191", "ui_error": "#E7E7E7", "ui_warn": "#B7B7B7"}},
	{"charizard", "Volcanic theme — burnt orange and ember", map[string]string{"ui_accent": "#F29C38"}},
}

// loadSkins is the built-ins, then the user skins in <home>/skins/*.toml by
// name. A user skin named like a built-in replaces it in place.
func loadSkins(home string) []skinDef {
	skins := append([]skinDef(nil), builtinSkins...)
	files, _ := filepath.Glob(filepath.Join(home, "skins", "*.toml"))
	sort.Strings(files)
	user := map[string]skinDef{}
	for _, f := range files {
		var data struct {
			Name        string         `toml:"name"`
			Description string         `toml:"description"`
			Colors      map[string]any `toml:"colors"`
		}
		b, err := os.ReadFile(f)
		if err != nil || toml.Unmarshal(b, &data) != nil {
			continue
		}
		if data.Name == "" {
			data.Name = strings.TrimSuffix(filepath.Base(f), ".toml")
		}
		sk := skinDef{name: data.Name, desc: data.Description, colors: map[string]string{}}
		for k, v := range data.Colors {
			if s, ok := v.(string); ok {
				sk.colors[k] = s
			}
		}
		user[sk.name] = sk
	}
	var extra []string
	for name, sk := range user {
		replaced := false
		for i := range skins {
			if skins[i].name == name {
				skins[i], replaced = sk, true
			}
		}
		if !replaced {
			extra = append(extra, name)
		}
	}
	sort.Strings(extra)
	for _, name := range extra {
		skins = append(skins, user[name])
	}
	return skins
}

var ansiNames = map[string]string{
	"black": "0", "red": "1", "green": "2", "yellow": "3", "blue": "4", "magenta": "5", "cyan": "6", "white": "7",
	"gray": "8", "grey": "8", // bright black
}

// parseSkinColor reads a skin colour value (tui.py _to_ansi): its first colour
// word is that ANSI colour, "default" is the terminal's own, "#rgb" and
// "#rrggbb" are as given; anything else is not a colour.
func parseSkinColor(v string) (lipgloss.TerminalColor, bool) {
	v = strings.ToLower(strings.TrimSpace(v))
	for _, w := range strings.Fields(strings.ReplaceAll(v, ",", " ")) {
		if c, ok := ansiNames[w]; ok {
			return lipgloss.Color(c), true
		}
		if w == "default" {
			return lipgloss.NoColor{}, true
		}
	}
	if strings.HasPrefix(v, "#") && (len(v) == 4 || len(v) == 7) {
		h := v[1:]
		if len(h) == 3 {
			h = string([]byte{h[0], h[0], h[1], h[1], h[2], h[2]})
		}
		for _, r := range h {
			if !strings.ContainsRune("0123456789abcdef", r) {
				return nil, false
			}
		}
		return lipgloss.Color("#" + h), true
	}
	return nil, false
}

// skinColors resolves the six colours of a skin.
func skinColors(sk skinDef) map[string]lipgloss.TerminalColor {
	out := map[string]lipgloss.TerminalColor{}
	for _, k := range skinKeys {
		def, _ := parseSkinColor(builtinSkins[0].colors[k])
		c := def
		if v, ok := sk.colors[k]; ok {
			if p, ok := parseSkinColor(v); ok {
				c = p
			}
		}
		out[k] = c
	}
	return out
}

// theme is what the screen is drawn with: a skin's colours, the surfaces, and
// the renderer they are drawn through. A new skin is a new theme, so cached
// rows of the old one are never reused.
type theme struct {
	r    *lipgloss.Renderer
	name string

	pal   map[string]string // Palette of the terminal's background, or nil
	exact *strings.Replacer // corrects termenv's hex colours; nil when none needs it

	accent, thinking, tool, okC, warn, errC lipgloss.TerminalColor
	panel                                   lipgloss.TerminalColor
	surface                                 lipgloss.TerminalColor // nil: no generated surface

	acc, think, toolS, okS, warnS, errS, mut, bld, plain lipgloss.Style
}

// newTheme builds the theme of skin sk. pal is Palette's answer for the
// terminal's background, or nil when it is not known.
func newTheme(r *lipgloss.Renderer, sk skinDef, pal map[string]string) *theme {
	cols := skinColors(sk)
	th := &theme{r: r, name: sk.name, pal: pal,
		accent: cols["ui_accent"], thinking: cols["ui_thinking"], tool: cols["ui_tool"],
		okC: cols["ui_ok"], warn: cols["ui_warn"], errC: cols["ui_error"]}
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
	}
	var hexes []string
	for _, c := range []lipgloss.TerminalColor{th.accent, th.thinking, th.tool, th.okC, th.warn, th.errC} {
		if s, ok := c.(lipgloss.Color); ok && strings.HasPrefix(string(s), "#") {
			hexes = append(hexes, string(s))
		}
	}
	if pal != nil {
		hexes = append(hexes, pal["panel"], pal["surface"], pal["text-muted"])
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

// markdownTheme is the theme Markdown draws with: the default skin, no palette.
func markdownTheme() *theme {
	mdThemeOnce.Do(func() {
		r := lipgloss.NewRenderer(io.Discard)
		r.SetColorProfile(termenv.TrueColor)
		r.SetHasDarkBackground(true)
		mdTheme = newTheme(r, builtinSkins[0], nil)
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

// startTheme is the theme the app starts in: the skin of config.toml's
// [display] skin (default when unset or unknown), on the surfaces of the
// terminal's background.
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
	want := ""
	if cfg, err := config.Load(o.Home); err == nil {
		want = cfg.Display.Skin
	}
	skin := builtinSkins[0]
	for _, sk := range loadSkins(o.Home) {
		if sk.name == want {
			skin = sk
		}
	}
	return newTheme(newRenderer(out, dark), skin, pal)
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
