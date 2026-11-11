package server

import (
	"testing"
	"time"
)

func TestLastResetIsTheLatestResetHourAtOrBeforeNow(t *testing.T) {
	zone, err := time.LoadLocation("America/Chicago")
	if err != nil {
		t.Skip(err)
	}
	at := func(mo time.Month, d, h, m int) time.Time { return time.Date(2026, mo, d, h, m, 0, 0, zone) }
	for _, c := range []struct {
		now  time.Time
		hour int
		want time.Time
	}{
		{at(11, 10, 5, 0), 4, at(11, 10, 4, 0)},
		{at(11, 10, 4, 0), 4, at(11, 10, 4, 0)},
		{at(11, 10, 3, 59), 4, at(11, 9, 4, 0)},
		{at(11, 10, 23, 30), 4, at(11, 10, 4, 0)},
		{at(11, 1, 0, 30), 0, at(11, 1, 0, 0)},
		{at(12, 1, 2, 0), 4, at(11, 30, 4, 0)},
		{at(11, 2, 3, 0), 4, at(11, 1, 4, 0)}, // across the end of daylight time
	} {
		if got := lastReset(c.now, c.hour); !got.Equal(c.want) {
			t.Errorf("lastReset(%v, %d) = %v, want %v", c.now, c.hour, got, c.want)
		}
	}
}
