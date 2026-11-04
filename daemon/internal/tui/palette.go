package tui

import (
	"fmt"
	"strconv"
	"strings"
)

// Palette is the surfaces generated from a terminal background given as
// #rrggbb (son_of_anton_tui/palette.py build_palette, after opencode's
// "system" theme): "panel" is the user rows' and the sidebar's background,
// "surface" the prompt frame's, "text-muted" muted text's colour, and
// "backdrop" the dim the screen takes behind an open dialog. A background that
// is not #rrggbb gives nil.
func Palette(bg string) map[string]string {
	r, g, b, ok := parseHex(bg)
	if !ok {
		return nil
	}
	lum := luminance(r, g, b)
	dark := lum <= 127.5 // polarity
	grays := grayScale(r, g, b, dark)
	return map[string]string{
		"backdrop":        grays[1],
		"panel":           grays[2],
		"surface":         grays[3],
		"boost":           grays[3],
		"panel-lighten-1": grays[4],
		"border-blurred":  grays[6],
		"border":          grays[7],
		"text-muted":      mutedText(lum, dark),
	}
}

func parseHex(s string) (r, g, b int, ok bool) {
	s = strings.TrimSpace(s)
	if len(s) != 7 || s[0] != '#' {
		return
	}
	v, err := strconv.ParseUint(s[1:], 16, 32)
	if err != nil {
		return
	}
	return int(v >> 16), int(v >> 8 & 0xff), int(v & 0xff), true
}

func luminance(r, g, b int) float64 {
	return float64(float64(0.299*float64(r))+float64(0.587*float64(g))) + float64(0.114*float64(b))
}

func hexOf(r, g, b int) string { return fmt.Sprintf("#%02x%02x%02x", r, g, b) }

// grayScale is opencode's generateGrayScale: twelve steps, each moving the
// background 40% (scaled by the step) toward white on a dark terminal, black on
// a light one. Index 0 is unused.
func grayScale(r, g, b int, dark bool) [13]string {
	var out [13]string
	lum := luminance(r, g, b)
	for i := 1; i <= 12; i++ {
		factor := float64(i) / 12.0
		var fr, fg, fb float64
		switch {
		case dark && lum < 10:
			v := float64(int(float64(float64(factor*0.4) * 255)))
			fr, fg, fb = v, v, v
		case dark:
			ratio := float64(lum+float64(float64(float64(255-lum)*factor)*0.4)) / lum
			fr = min(float64(float64(r)*ratio), 255)
			fg = min(float64(float64(g)*ratio), 255)
			fb = min(float64(float64(b)*ratio), 255)
		case lum > 245:
			v := float64(int(255 - float64(float64(factor*0.4)*255)))
			fr, fg, fb = v, v, v
		default:
			ratio := float64(lum*float64(1-float64(factor*0.4))) / lum
			fr = max(float64(float64(r)*ratio), 0)
			fg = max(float64(float64(g)*ratio), 0)
			fb = max(float64(float64(b)*ratio), 0)
		}
		out[i] = hexOf(int(fr), int(fg), int(fb))
	}
	return out
}

// mutedText is opencode's generateMutedTextColor.
func mutedText(lum float64, dark bool) string {
	var v int
	switch {
	case dark && lum < 10:
		v = 180
	case dark:
		v = min(int(160+float64(lum*0.3)), 200)
	case lum > 245:
		v = 75
	default:
		v = max(int(100-float64(float64(255-lum)*0.2)), 60)
	}
	return hexOf(v, v, v)
}
