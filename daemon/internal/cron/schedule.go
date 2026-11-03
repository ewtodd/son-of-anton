package cron

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Schedule is a parsed schedule, the "schedule" object of a job.
type Schedule struct {
	Kind    string // "once" | "interval" | "cron"
	Minutes int    // interval
	Expr    string // cron
	RunAt   string // once, ISO 8601
	Display string
}

// Map is the schedule as stored in a job.
func (s Schedule) Map() map[string]any {
	m := map[string]any{"kind": s.Kind, "display": s.Display}
	switch s.Kind {
	case "interval":
		m["minutes"] = s.Minutes
	case "cron":
		m["expr"] = s.Expr
	case "once":
		m["run_at"] = s.RunAt
	}
	return m
}

// ScheduleOf reads the schedule of a job record.
func ScheduleOf(j Job) Schedule {
	m, _ := j["schedule"].(map[string]any)
	s := Schedule{}
	s.Kind, _ = m["kind"].(string)
	s.Expr, _ = m["expr"].(string)
	s.RunAt, _ = m["run_at"].(string)
	s.Display, _ = m["display"].(string)
	if n, ok := m["minutes"].(float64); ok {
		s.Minutes = int(n)
	} else if n, ok := m["minutes"].(int); ok {
		s.Minutes = n
	}
	return s
}

const (
	oneshotGrace = 120 * time.Second
	minGrace     = 120 * time.Second
	maxGrace     = 2 * time.Hour
)

var (
	durationRE  = regexp.MustCompile(`^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$`)
	cronFieldRE = regexp.MustCompile(`^[\d*\-,/]+$`)
	dateRE      = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}`)
)

func parseDuration(s string) (int, error) {
	s = strings.ToLower(strings.TrimSpace(s))
	m := durationRE.FindStringSubmatch(s)
	if m == nil {
		return 0, fmt.Errorf("Invalid duration: '%s'. Use format like '30m', '2h', or '1d'", s)
	}
	n, err := strconv.Atoi(m[1])
	if err != nil {
		return 0, fmt.Errorf("Invalid duration: '%s'. Use format like '30m', '2h', or '1d'", s)
	}
	switch m[2][0] {
	case 'h':
		n *= 60
	case 'd':
		n *= 1440
	}
	return n, nil
}

// ParseSchedule reads a schedule string: "every <duration>", a five-field
// cron expression, a timestamp, or a bare duration (once, from now). A
// timestamp without an offset is in now's zone.
func ParseSchedule(s string, now time.Time) (Schedule, error) {
	original := strings.TrimSpace(s)
	if strings.HasPrefix(strings.ToLower(original), "every ") {
		minutes, err := parseDuration(original[6:])
		if err != nil {
			return Schedule{}, err
		}
		return Schedule{Kind: "interval", Minutes: minutes, Display: fmt.Sprintf("every %dm", minutes)}, nil
	}
	parts := strings.Fields(original)
	if len(parts) >= 5 {
		all := true
		for _, p := range parts[:5] {
			all = all && cronFieldRE.MatchString(p)
		}
		if all {
			if len(parts) > 5 {
				return Schedule{}, fmt.Errorf("Invalid cron expression '%s': expected 5 fields, got %d", original, len(parts))
			}
			if _, err := parseCron(original); err != nil {
				return Schedule{}, fmt.Errorf("Invalid cron expression '%s': %v", original, err)
			}
			return Schedule{Kind: "cron", Expr: original, Display: original}, nil
		}
	}
	if strings.Contains(original, "T") || dateRE.MatchString(original) {
		t, err := ParseISO(original, now.Location())
		if err != nil {
			return Schedule{}, fmt.Errorf("Invalid timestamp '%s': %v", original, err)
		}
		return Schedule{Kind: "once", RunAt: ISO(t), Display: "once at " + t.Format("2006-01-02 15:04")}, nil
	}
	if minutes, err := parseDuration(original); err == nil {
		return Schedule{Kind: "once", RunAt: ISO(now.Add(time.Duration(minutes) * time.Minute)), Display: "once in " + original}, nil
	}
	return Schedule{}, fmt.Errorf("Invalid schedule '%s'. Use:\n"+
		"  - Duration: '30m', '2h', '1d' (one-shot)\n"+
		"  - Interval: 'every 30m', 'every 2h' (recurring)\n"+
		"  - Cron: '0 9 * * *' (cron expression)\n"+
		"  - Timestamp: '2026-02-03T14:00:00' (one-shot at time)", original)
}

// NextRun is when the schedule next fires: a one-shot at its time when it
// never ran and is not more than 120 s past, an interval after the last run
// (or now), a cron expression after the last run (or now).
func NextRun(s Schedule, last *time.Time, now time.Time) (time.Time, bool) {
	switch s.Kind {
	case "once":
		if last != nil || s.RunAt == "" {
			return time.Time{}, false
		}
		t, err := ParseISO(s.RunAt, now.Location())
		if err != nil || t.Before(now.Add(-oneshotGrace)) {
			return time.Time{}, false
		}
		return t, true
	case "interval":
		if s.Minutes <= 0 {
			return time.Time{}, false
		}
		base := now
		if last != nil {
			base = *last
		}
		return base.Add(time.Duration(s.Minutes) * time.Minute), true
	case "cron":
		c, err := parseCron(s.Expr)
		if err != nil {
			return time.Time{}, false
		}
		base := now
		if last != nil {
			base = *last
		}
		return c.next(base)
	}
	return time.Time{}, false
}

// Grace is how late a recurring job may be and still run for its slot: half
// its period, from 120 s to 2 h.
func Grace(s Schedule) time.Duration {
	var period time.Duration
	switch s.Kind {
	case "interval":
		period = time.Duration(s.Minutes) * time.Minute
	case "cron":
		if c, err := parseCron(s.Expr); err == nil {
			if first, ok := c.next(time.Now()); ok {
				if second, ok := c.next(first); ok {
					period = second.Sub(first)
				}
			}
		}
	}
	if period <= 0 {
		return minGrace
	}
	return max(minGrace, min(period/2, maxGrace))
}

// cronExpr is a parsed five-field expression.
type cronExpr struct {
	min, hour, dom, month, dow [64]bool
	domStar, dowStar           bool
}

// parseCron reads minute hour day-of-month month day-of-week: numbers, *,
// ranges, lists and steps. Day of week is 0-7 (0 and 7 are Sunday).
func parseCron(expr string) (*cronExpr, error) {
	f := strings.Fields(expr)
	if len(f) != 5 {
		return nil, fmt.Errorf("expected 5 fields, got %d", len(f))
	}
	c := &cronExpr{domStar: strings.HasPrefix(f[2], "*"), dowStar: strings.HasPrefix(f[4], "*")}
	for i, spec := range []struct {
		name   string
		lo, hi int
		set    *[64]bool
	}{{"minute", 0, 59, &c.min}, {"hour", 0, 23, &c.hour}, {"day of month", 1, 31, &c.dom}, {"month", 1, 12, &c.month}, {"day of week", 0, 7, &c.dow}} {
		if err := fillField(f[i], spec.lo, spec.hi, spec.set); err != nil {
			return nil, fmt.Errorf("%s: %v", spec.name, err)
		}
	}
	if c.dow[7] {
		c.dow[0] = true
	}
	return c, nil
}

func fillField(field string, lo, hi int, set *[64]bool) error {
	for _, item := range strings.Split(field, ",") {
		if item == "" {
			return fmt.Errorf("empty item in %q", field)
		}
		rng, stepText, hasStep := strings.Cut(item, "/")
		step := 1
		if hasStep {
			n, err := strconv.Atoi(stepText)
			if err != nil || n <= 0 {
				return fmt.Errorf("bad step in %q", item)
			}
			step = n
		}
		a, b := lo, hi
		switch {
		case rng == "*":
		case strings.Contains(rng, "-"):
			x, y, _ := strings.Cut(rng, "-")
			var err1, err2 error
			a, err1 = strconv.Atoi(x)
			b, err2 = strconv.Atoi(y)
			if err1 != nil || err2 != nil {
				return fmt.Errorf("bad range %q", item)
			}
		default:
			n, err := strconv.Atoi(rng)
			if err != nil {
				return fmt.Errorf("bad value %q", item)
			}
			a, b = n, n
			if hasStep {
				b = hi // "5/10" runs from 5 to the top
			}
		}
		if a < lo || b > hi || a > b {
			return fmt.Errorf("%q is out of range %d-%d", item, lo, hi)
		}
		for v := a; v <= b; v += step {
			set[v] = true
		}
	}
	return nil
}

// dayMatches follows cron: when both day fields are restricted either may
// match; otherwise both must.
func (c *cronExpr) dayMatches(d time.Time) bool {
	dom, dow := c.dom[d.Day()], c.dow[int(d.Weekday())]
	if !c.domStar && !c.dowStar {
		return dom || dow
	}
	return dom && dow
}

// next is the first match strictly after t, within the next eight years.
func (c *cronExpr) next(t time.Time) (time.Time, bool) {
	loc := t.Location()
	start := t.Truncate(time.Minute).Add(time.Minute)
	y, m, d := start.Date()
	for day := time.Date(y, m, d, 0, 0, 0, 0, loc); day.Before(start.AddDate(8, 0, 0)); day = day.AddDate(0, 0, 1) {
		if !c.month[int(day.Month())] || !c.dayMatches(day) {
			continue
		}
		for h := 0; h < 24; h++ {
			if !c.hour[h] {
				continue
			}
			for mi := 0; mi < 60; mi++ {
				if !c.min[mi] {
					continue
				}
				cand := time.Date(day.Year(), day.Month(), day.Day(), h, mi, 0, 0, loc)
				if !cand.Before(start) {
					return cand, true
				}
			}
		}
	}
	return time.Time{}, false
}
