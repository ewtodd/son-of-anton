package prompt

import (
	"os"
	"path/filepath"
	"strings"
	"time"
)

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
