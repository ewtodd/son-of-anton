package prompt_test

import (
	"strings"
	"testing"
	"time"
	_ "time/tzdata" // the zone database, so the test does not depend on the host's

	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
)

// The date line names the zone, its abbreviation and its UTC offset, so the
// model never has to guess between standard and daylight time.
func TestDateLineNamesTheZoneAndOffset(t *testing.T) {
	chicago, err := time.LoadLocation("America/Chicago")
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		now  time.Time
		want string
	}{
		{time.Date(2026, 11, 2, 9, 30, 0, 0, chicago), "Conversation started: Monday, November 2, 2026 (America/Chicago, CST, UTC-06:00)"},
		{time.Date(2026, 7, 6, 9, 30, 0, 0, chicago), "Conversation started: Monday, July 6, 2026 (America/Chicago, CDT, UTC-05:00)"},
		{time.Date(2026, 11, 2, 9, 30, 0, 0, time.UTC), "Conversation started: Monday, November 2, 2026 (UTC, UTC+00:00)"},
		{time.Date(2026, 11, 2, 9, 30, 0, 0, time.FixedZone("Local", -6*3600)), "Conversation started: Monday, November 2, 2026 (UTC-06:00)"},
	} {
		res, err := prompt.Build(prompt.Options{Home: t.TempDir(), Cwd: t.TempDir(), Now: c.now})
		if err != nil {
			t.Fatal(err)
		}
		if !strings.HasSuffix(res.Text, c.want) {
			t.Errorf("prompt ends %q, want %q", res.Text[strings.LastIndex(res.Text, "\n")+1:], c.want)
		}
	}
}
