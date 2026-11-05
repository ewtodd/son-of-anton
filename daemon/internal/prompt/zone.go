package prompt

import (
	"os"
	"path/filepath"
	"strings"
	"time"
)

// FormatSentAt renders a user message's send time as
// "[Sat 2026-10-24 14:32 CDT]" in the host's zone (prompt.LocalZone).
func FormatSentAt(t time.Time) string {
	return "[" + t.In(LocalZone()).Format("Mon 2006-01-02 15:04 MST") + "]"
}

// LocalZone is time.Local with its zone name, which time.Local alone prints
// as "Local": TZ, else the zoneinfo path /etc/localtime points at. Without
// either, time.Local.
func LocalZone() *time.Location {
	name := strings.TrimPrefix(os.Getenv("TZ"), ":")
	if name == "" {
		if p, err := filepath.EvalSymlinks("/etc/localtime"); err == nil {
			if i := strings.LastIndex(p, "zoneinfo/"); i >= 0 {
				name = p[i+len("zoneinfo/"):]
			}
		}
	}
	if name != "" {
		if loc, err := time.LoadLocation(name); err == nil {
			return loc
		}
	}
	return time.Local
}
