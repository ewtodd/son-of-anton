package cron

import (
	"fmt"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

// Paused says whether a record carries a pause marker.
func Paused(j Job) bool {
	if s, _ := j["state"].(string); strings.TrimSpace(s) == "paused" {
		return true
	}
	switch v := j["paused_at"].(type) {
	case nil:
		return false
	case string:
		return v != ""
	}
	return true
}

// Enabled is the record's enabled flag (true when absent).
func Enabled(j Job) bool {
	b, ok := j["enabled"].(bool)
	return !ok || b
}

// Due returns the ids of the jobs to fire at now: enabled, unpaused, with a
// next_run_at that is not after now. A recurring job later than its Grace
// still fires once, and its next_run_at moves to the next slot after now (a
// backlog is one run); a job without a next_run_at gets one. It changes
// those fields in jobs.
func Due(jobs []Job, now time.Time) []string {
	var due []string
	for _, j := range jobs {
		id, _ := j["id"].(string)
		if id == "" || !Enabled(j) || Paused(j) {
			continue
		}
		s := ScheduleOf(j)
		next, ok := time.Time{}, false
		if text, _ := j["next_run_at"].(string); text != "" {
			if t, err := ParseISO(text, now.Location()); err == nil {
				next, ok = t, true
			}
		}
		if !ok {
			var last *time.Time
			if text, _ := j["last_run_at"].(string); text != "" {
				if t, err := ParseISO(text, now.Location()); err == nil {
					last = &t
				}
			}
			if s.Kind != "once" {
				last = &now
			}
			if next, ok = NextRun(s, last, now); !ok {
				continue
			}
			j["next_run_at"] = ISO(next)
		}
		if next.After(now) {
			continue
		}
		if (s.Kind == "cron" || s.Kind == "interval") && now.Sub(next) > Grace(s) {
			if skip, ok := NextRun(s, &now, now); ok {
				j["next_run_at"] = ISO(skip)
			}
		}
		due = append(due, id)
	}
	return due
}

// Silent says whether a cron response asks for no delivery: one of the
// tokens as the whole response, or as its first or last non-empty line, or a
// response starting with [SILENT].
func Silent(response string) bool {
	text := strings.TrimSpace(response)
	if text == "" {
		return false
	}
	token := func(s string) bool {
		switch strings.Join(strings.Fields(strings.ToUpper(s)), " ") {
		case "[SILENT]", "SILENT", "NO_REPLY", "NO REPLY":
			return true
		}
		return false
	}
	if token(text) {
		return true
	}
	var lines []string
	for _, l := range strings.Split(text, "\n") {
		if strings.TrimSpace(l) != "" {
			lines = append(lines, l)
		}
	}
	if len(lines) > 0 && (token(lines[0]) || token(lines[len(lines)-1])) {
		return true
	}
	return strings.HasPrefix(strings.ToUpper(text), "[SILENT]")
}

// Frame is a delivery: the content under a header naming the job, and a
// footer saying how to manage it.
func Frame(name, id, content string) string {
	return "Cronjob Response: " + name + "\n(job_id: " + id + ")\n-------------\n\n" + content +
		"\n\nTo stop or manage this job, send me a new message (e.g. \"stop reminder " + name + "\")."
}

var (
	wrapperRE = regexp.MustCompile(`^(RuntimeError|Exception|ValueError|HTTPStatusError):\s*`)
	spaceRE   = regexp.MustCompile(`\s+`)
)

// FailureText is the one-line notice a failed run delivers.
func FailureText(name, errMsg string) string {
	text := strings.TrimSpace(errMsg)
	if text == "" {
		text = "unknown error"
	}
	if r := []rune(text); len(r) > 2000 {
		text = string(r[:2000])
	}
	text = strings.TrimSpace(spaceRE.ReplaceAllString(wrapperRE.ReplaceAllString(text, ""), " "))
	if utf8.RuneCountInString(text) > 180 {
		text = strings.TrimRight(string([]rune(text)[:177]), " \t\n") + "..."
	}
	return fmt.Sprintf("⚠️ Cron '%s' failed: %s", name, text)
}

// OriginChat is the Signal chat a job was created in, "" for none.
func OriginChat(j Job) string {
	o, _ := j["origin"].(map[string]any)
	if p, _ := o["platform"].(string); strings.ToLower(p) != "signal" {
		return ""
	}
	id, _ := o["chat_id"].(string)
	return strings.TrimSpace(id)
}

// ResolveTargets turns a job's deliver setting into Signal recipients
// ("group:<id>" or a number). problem is non-empty when part of the setting
// could not be resolved. A job set to origin that has no chat and no home
// channel is local, not a problem.
func ResolveTargets(j Job, homeChannel string) (targets []string, problem string) {
	deliver, _ := j["deliver"].(string)
	deliver = strings.TrimSpace(deliver)
	if deliver == "" {
		deliver = "local"
	}
	origin := OriginChat(j)
	seen := map[string]bool{}
	add := func(t string) {
		if t != "" && !seen[t] {
			seen[t] = true
			targets = append(targets, t)
		}
	}
	var bad []string
	for _, part := range strings.Split(deliver, ",") {
		part = strings.TrimSpace(part)
		switch lower := strings.ToLower(part); {
		case part == "" || lower == "local":
		case lower == "origin":
			if origin != "" {
				add(origin)
			} else {
				add(homeChannel)
			}
		case lower == "signal":
			if homeChannel != "" {
				add(homeChannel)
			} else {
				add(origin)
			}
		case lower == "all":
			add(homeChannel)
		case strings.HasPrefix(lower, "signal:") && strings.TrimSpace(part[7:]) != "":
			add(strings.TrimSpace(part[7:]))
		default:
			bad = append(bad, part)
		}
	}
	switch {
	case len(bad) > 0:
		problem = "unsupported delivery target " + strings.Join(bad, ", ") + " (Signal is the only platform)"
	case len(targets) == 0 && strings.ToLower(deliver) != "local" && strings.ToLower(deliver) != "origin":
		problem = "no delivery target resolved for deliver=" + deliver
	}
	return targets, problem
}

// effectiveState is the state a record shows: enabled is what the scheduler
// honours, so an enabled job never reads as paused.
func effectiveState(j Job) string {
	stored, _ := j["state"].(string)
	stored = strings.TrimSpace(stored)
	if stored == "completed" || stored == "error" {
		return stored
	}
	if !Enabled(j) {
		if Paused(j) || stored == "paused" {
			return "paused"
		}
		if stored == "" {
			return "paused"
		}
		return stored
	}
	if stored == "paused" || Paused(j) {
		return "scheduled"
	}
	if stored == "" {
		return "scheduled"
	}
	return stored
}

func text(v any) string {
	if v == nil {
		return ""
	}
	return fmt.Sprint(v)
}

// Normalize is a read-safe copy of a record: an id, a prompt, a name and a
// schedule_display even when the stored ones are missing, and the derived
// state. The stored record is not changed.
func Normalize(j Job) Job {
	n := Job{}
	for k, v := range j {
		n[k] = v
	}
	id := text(j["id"])
	if id == "" {
		id = "unknown"
	}
	prompt := text(j["prompt"])
	n["id"], n["prompt"] = id, prompt
	name := strings.TrimSpace(text(j["name"]))
	if name == "" {
		label := prompt
		if label == "" {
			label = strings.TrimSpace(text(j["script"]))
		}
		if label == "" {
			label = id
		}
		name = strings.TrimSpace(string([]rune(label)[:min(50, len([]rune(label)))]))
		if name == "" {
			name = "cron job"
		}
	}
	n["name"] = name
	display := strings.TrimSpace(text(j["schedule_display"]))
	if display == "" {
		if m, ok := j["schedule"].(map[string]any); ok {
			for _, k := range []string{"display", "value", "expr", "run_at"} {
				if display = strings.TrimSpace(text(m[k])); display != "" {
					break
				}
			}
		}
	}
	if display == "" {
		display = "?"
	}
	n["schedule_display"] = display
	n["state"] = effectiveState(n)
	return n
}
