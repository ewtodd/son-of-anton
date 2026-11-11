package tools

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/ewtodd/son-of-anton/daemon/internal/cron"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

// Cron is what the cronjob tool needs from the daemon.
type Cron struct {
	Home string
	// Chat is the calling chat's stamp (signal:dm:<sender> or
	// signal:group:<id>[:<sender>]); "" for a terminal session.
	Chat string
	// HomeChannel is the Signal chat "signal" and "all" deliver to ("" for
	// none).
	HomeChannel string
	// Run starts a job now in the background, with extra context for that run
	// alone. It reports false when the job is already running.
	Run func(id, extra string) bool
}

// cronjobTool lets the model schedule and manage the instance's jobs.
type cronjobTool struct{ home string }

// CronJob is the cronjob tool for the instance at home.
func CronJob(home string) Tool { return cronjobTool{home: home} }

// displayHome is the home as the model is shown it: under the user's home as
// ~/..., else in full.
func displayHome(home string) string {
	if u, err := os.UserHomeDir(); err == nil && u != "" {
		if rel, err := filepath.Rel(u, home); err == nil && rel != ".." && !strings.HasPrefix(rel, "../") {
			if rel == "." {
				return "~"
			}
			return "~/" + rel
		}
	}
	return home
}

func (t cronjobTool) Schema() provider.Tool {
	params := `{
  "type": "object",
  "properties": {
    "action": {"type": "string", "description": "What to do: create, list, update, pause, resume, remove or run."},
    "job_id": {"type": "string", "description": "The job to act on (its id or its name). Needed by every action except create and list."},
    "prompt": {"type": "string", "description": "For create, the instructions the job will follow each time; they must make sense with no other context. For run, extra instructions for that single run."},
    "schedule": {"type": "string", "description": "When to run; create needs it. Four forms: a delay such as '30m' (one run, that long from now); an interval such as 'every 2h' (repeating); a five-field cron expression such as '0 9 * * *' (repeating); or a timestamp such as '2026-06-01T09:00:00' (one run). A timestamp with no UTC offset is read as local time in the timezone named in your system prompt, so give the time the user stated and do not convert it. Pick a repeating form only if the user wants repetition."},
    "name": {"type": "string", "description": "A short name for the job."},
    "repeat": {"type": "integer", "description": "How many times to run before the job ends. One-time schedules run once and repeating ones never end, unless this says otherwise."},
    "deliver": {"type": "string", "description": "Where each result is sent. Leave it out to answer in the current chat, which is nearly always what is wanted. Values: 'local' (keep it on disk, send nothing), 'all' (every connected home channel), 'signal:<chat_id>' (that chat). Several can be joined with commas, as in 'origin,all'."},
    "script": {"type": "string", "description": ` + quote("A script to run before each run. Its stdout is appended to the prompt; with no_agent, its stdout is the message itself. Give a path relative to "+displayHome(t.home)+"/scripts/. Files ending in .sh or .bash are run with bash and any others with Python. On update, an empty string removes the script.") + `},
    "no_agent": {"type": "boolean", "default": false, "description": "Run the script alone, without a model. Output on stdout is sent as the message, no output sends nothing, and a failing script sends an alert. This requires script and ignores prompt. It suits checks whose script prints the text to deliver; anything that calls for judgement needs the model."},
    "workdir": {"type": "string", "description": "An absolute path of an existing directory to run the job in. The job reads that directory's AGENTS.md and its terminal and file tools start there. Jobs with a workdir run one after another, never together. An empty string removes it."},
    "include_completed": {"type": "boolean", "default": false, "description": "For list, also show the jobs that have run for the last time, such as a one-time job after its run. They are left out otherwise."}
  },
  "required": ["action"]
}`
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "cronjob",
		Description: "Set up and look after jobs that run a prompt at a later time or repeatedly: each run is a model that follows the prompt. " +
			"To remind the user of something, use the reminder tool instead. Actions: create (needs schedule and prompt), list, update, pause, resume, remove, run.\n\n" +
			"Each run is a new session. It has no memory of this chat and nobody to ask, so the prompt has to stand on its own, and whatever the run replies last is what gets delivered. " +
			"Before changing or stopping a job, list to find its job_id; do not guess one. Update an existing job rather than adding a near copy of it. " +
			"run starts a job right now in the background and its result comes back later as a new message, so do not wait for it or poll.",
		Parameters: json.RawMessage(params),
	}}
}

func quote(s string) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(s)
	return strings.TrimRight(buf.String(), "\n")
}

type cronArgs struct {
	Action           string   `json:"action"`
	JobID            string   `json:"job_id"`
	Prompt           *string  `json:"prompt"`
	Schedule         *string  `json:"schedule"`
	Name             *string  `json:"name"`
	Repeat           *float64 `json:"repeat"`
	Deliver          *string  `json:"deliver"`
	Script           *string  `json:"script"`
	NoAgent          *bool    `json:"no_agent"`
	Workdir          *string  `json:"workdir"`
	Reason           *string  `json:"reason"`
	IncludeCompleted bool     `json:"include_completed"`
}

// indented renders v as JSON with two-space indentation and no escaping of
// non-ASCII text.
func indented(v any) (string, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		return "", err
	}
	return strings.TrimRight(buf.String(), "\n"), nil
}

func toolError(msg string) (string, error) {
	return jsonResult(map[string]any{"error": msg, "success": false})
}

func (t cronjobTool) Run(ctx context.Context, env *Env, raw json.RawMessage) (string, error) {
	if env.Cron == nil {
		return errorResult("cron is not available in this session"), nil
	}
	var a cronArgs
	if err := json.Unmarshal(raw, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	h := &cronHandler{c: env.Cron, zone: cron.Zone(env.Cron.Home)}
	res, err := h.run(a)
	if err != nil {
		return toolError(err.Error())
	}
	return indented(res)
}

type cronHandler struct {
	c    *Cron
	zone *time.Location
}

// answer is a tool result that is not an error but has success false.
type answer map[string]any

func (h *cronHandler) now() time.Time { return time.Now().In(h.zone) }

func (h *cronHandler) run(a cronArgs) (any, error) {
	action := strings.ToLower(strings.TrimSpace(a.Action))
	switch action {
	case "create":
		return h.create(a)
	case "list":
		jobs, err := cron.Load(h.c.Home)
		if err != nil {
			return nil, err
		}
		out, completed := []any{}, 0
		for _, j := range jobs {
			n := cron.Normalize(j)
			if IsReminder(n) {
				continue
			}
			if n["state"] == "completed" && !a.IncludeCompleted {
				completed++
				continue
			}
			out = append(out, formatJob(n))
		}
		res := answer{"success": true, "count": len(out), "jobs": out}
		if completed > 0 {
			res["completed_left_out"] = completed
		}
		return res, nil
	}
	if a.JobID == "" {
		return nil, fmt.Errorf("the %s action needs a job_id", action)
	}
	jobs, err := cron.Load(h.c.Home)
	if err != nil {
		return nil, err
	}
	job, matches := resolveJob(jobs, a.JobID)
	if len(matches) > 1 {
		ids := make([]string, len(matches))
		list := make([]any, len(matches))
		for i, m := range matches {
			n := cron.Normalize(m)
			ids[i] = fmt.Sprint(n["id"])
			list[i] = map[string]any{"id": n["id"], "name": n["name"], "schedule": n["schedule_display"], "next_run_at": n["next_run_at"]}
		}
		return answer{"success": false, "matches": list,
			"error": fmt.Sprintf("'%s' is the name of %d jobs (%s); pass the job id to pick one.", a.JobID, len(matches), strings.Join(ids, ", "))}, nil
	}
	if job == nil {
		return answer{"success": false, "error": fmt.Sprintf("No job has the id or name '%s'. List the jobs to see what exists.", a.JobID)}, nil
	}
	job = cron.Normalize(job)
	if IsReminder(job) {
		return answer{"success": false, "error": fmt.Sprintf("'%s' is a reminder: change or cancel it with the reminder tool.", job["name"])}, nil
	}
	id := job["id"].(string)
	switch action {
	case "remove":
		return h.remove(id, job)
	case "pause":
		reason := any(nil)
		if a.Reason != nil {
			reason = *a.Reason
		}
		updated, err := h.change(id, func(j cron.Job) error {
			j["enabled"], j["state"], j["paused_at"], j["paused_reason"] = false, "paused", cron.ISO(h.now()), reason
			return nil
		})
		if err != nil {
			return nil, err
		}
		return answer{"success": true, "job": formatJob(updated)}, nil
	case "resume":
		next, ok := cron.NextRun(cron.ScheduleOf(job), nil, h.now())
		if !ok && cron.ScheduleOf(job).Kind == "once" {
			return nil, fmt.Errorf("This job cannot be resumed: its single run time, %s, has passed by more than the 120 second allowance, so it would never run.", cron.ScheduleOf(job).RunAt)
		}
		updated, err := h.change(id, func(j cron.Job) error {
			j["enabled"], j["state"], j["paused_at"], j["paused_reason"] = true, "scheduled", nil, nil
			j["next_run_at"] = nil
			if ok {
				j["next_run_at"] = cron.ISO(next)
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
		return answer{"success": true, "job": formatJob(updated)}, nil
	case "run", "run_now", "trigger":
		return h.runNow(id, job, a)
	case "update":
		return h.update(id, job, a)
	}
	return nil, fmt.Errorf("'%s' is not a cronjob action", a.Action)
}

// resolveJob finds a job by id, else by case-insensitive name. More than one
// name match returns them all and no job.
func resolveJob(jobs []cron.Job, ref string) (cron.Job, []cron.Job) {
	for _, j := range jobs {
		if j["id"] == ref {
			return j, nil
		}
	}
	var matches []cron.Job
	for _, j := range jobs {
		if name, _ := j["name"].(string); strings.EqualFold(name, ref) {
			matches = append(matches, j)
		}
	}
	if len(matches) == 1 {
		return matches[0], nil
	}
	return nil, matches
}

// change edits one stored job under the lock and returns it as read back.
func (h *cronHandler) change(id string, f func(cron.Job) error) (cron.Job, error) {
	var out cron.Job
	err := cron.Update(h.c.Home, func(jobs []cron.Job) ([]cron.Job, error) {
		for _, j := range jobs {
			if j["id"] == id {
				if err := f(j); err != nil {
					return nil, err
				}
				out = cron.Normalize(j)
				return jobs, nil
			}
		}
		return nil, fmt.Errorf("No job has the id or name '%s'. List the jobs to see what exists.", id)
	})
	return out, err
}

func (h *cronHandler) remove(id string, job cron.Job) (any, error) {
	removed := false
	err := cron.Update(h.c.Home, func(jobs []cron.Job) ([]cron.Job, error) {
		kept := jobs[:0:0]
		for _, j := range jobs {
			if j["id"] != id {
				kept = append(kept, j)
			}
		}
		removed = len(kept) < len(jobs)
		return kept, nil
	})
	if err != nil {
		return nil, err
	}
	if !removed {
		return nil, fmt.Errorf("Job '%s' could not be removed", id)
	}
	if id != "" && !strings.ContainsAny(id, "/\\") && id != "." && id != ".." {
		_ = os.RemoveAll(filepath.Join(cron.Dir(h.c.Home), "output", id))
	}
	return answer{"success": true, "message": fmt.Sprintf("Removed the job '%s'.", job["name"]),
		"removed_job": map[string]any{"id": id, "name": job["name"], "schedule": job["schedule_display"]}}, nil
}

func (h *cronHandler) runNow(id string, job cron.Job, a cronArgs) (any, error) {
	extra := ""
	if a.Prompt != nil && *a.Prompt != "" {
		if msg := scanPrompt(*a.Prompt); msg != "" {
			return nil, errors.New(msg)
		}
		extra = *a.Prompt
	}
	started := h.c.Run != nil && h.c.Run(id, extra)
	f := formatJob(job)
	if !started {
		f["executed"] = false
		f["execution_skipped"] = "It is already running, so no second run was started."
		return answer{"success": true, "job": f}, nil
	}
	f["executed"] = true
	f["execution_mode"] = "background"
	return answer{"success": true, "job": f, "note": "The job has been started in the background. Carry on with other work. " +
		"Its result goes wherever the job's deliver setting points, and its last_status is updated. " +
		"There is nothing to wait for."}, nil
}

func (h *cronHandler) create(a cronArgs) (any, error) {
	if a.Schedule == nil || *a.Schedule == "" {
		return nil, errors.New("create needs a schedule")
	}
	prompt, script := deref(a.Prompt), strings.TrimSpace(deref(a.Script))
	noAgent := a.NoAgent != nil && *a.NoAgent
	if noAgent {
		if script == "" {
			return nil, errors.New("A job with no_agent is just its script, so create needs a script.")
		}
	} else if prompt == "" {
		return nil, errors.New("create needs a prompt")
	}
	if prompt != "" {
		if msg := scanPrompt(prompt); msg != "" {
			return nil, errors.New(msg)
		}
	}
	if err := h.checkScript(script); err != nil {
		return nil, err
	}
	now := h.now()
	sched, err := cron.ParseSchedule(*a.Schedule, now)
	if err != nil {
		return nil, err
	}
	var repeat any
	if a.Repeat != nil && *a.Repeat > 0 {
		repeat = int(*a.Repeat)
	}
	if sched.Kind == "once" && repeat == nil {
		repeat = 1
	}
	origin := h.origin()
	deliver := strings.TrimSpace(deref(a.Deliver))
	userDeliver := deliver
	if deliver == "" {
		deliver = "local"
		if origin != nil {
			deliver = "origin"
		}
	}
	workdir, err := normalizeWorkdir(deref(a.Workdir))
	if err != nil {
		return nil, err
	}
	next, ok := cron.NextRun(sched, nil, now)
	if sched.Kind == "once" && !ok {
		return nil, fmt.Errorf("The requested time, %s, is more than 120 seconds ago, so the job cannot be scheduled.", sched.RunAt)
	}
	label := prompt
	if label == "" && noAgent {
		label = script
	}
	if label == "" {
		label = "cron job"
	}
	name := strings.TrimSpace(deref(a.Name))
	if name == "" {
		name = strings.TrimSpace(string([]rune(label)[:min(50, utf8.RuneCountInString(label))]))
	}
	var b [6]byte
	if _, err := rand.Read(b[:]); err != nil {
		return nil, err
	}
	job := cron.Job{
		"id": hex.EncodeToString(b[:]), "name": name, "prompt": prompt,
		"model": nil, "provider": nil, "provider_snapshot": nil, "model_snapshot": nil, "base_url": nil,
		"script": nilIfEmpty(script), "no_agent": noAgent, "monitor_script": nil, "monitor_url": nil, "monitor_state": nil,
		"context_from": nil, "schedule": sched.Map(), "schedule_display": sched.Display,
		"repeat":  map[string]any{"times": repeat, "completed": 0},
		"enabled": true, "state": "scheduled", "paused_at": nil, "paused_reason": nil,
		"created_at": cron.ISO(now), "next_run_at": cron.ISO(next),
		"last_run_at": nil, "last_status": nil, "last_error": nil, "last_delivery_error": nil, "failure_streak": 0,
		"deliver": deliver, "origin": origin, "enabled_toolsets": nil, "workdir": nilIfEmpty(workdir),
	}
	if err := cron.Update(h.c.Home, func(jobs []cron.Job) ([]cron.Job, error) { return append(jobs, job), nil }); err != nil {
		return nil, err
	}
	message := fmt.Sprintf("Created the job '%s'.", name)
	if strings.ToLower(userDeliver) != "local" {
		if targets, _ := cron.ResolveTargets(job, h.c.HomeChannel); len(targets) == 0 {
			message += " Nothing will be sent for this job: results are only stored on disk and do not come back into this session. " +
				"To be told when it runs, update the job so deliver names a Signal chat, for example 'signal' or 'signal:<chat_id>'."
		}
	}
	return answer{"success": true, "job_id": job["id"], "name": name, "schedule": sched.Display, "repeat": repeatDisplay(job),
		"deliver": deliver, "next_run_at": job["next_run_at"], "job": formatJob(cron.Normalize(job)), "message": message}, nil
}

func (h *cronHandler) update(id string, job cron.Job, a cronArgs) (any, error) {
	if a.Prompt == nil && a.Name == nil && a.Deliver == nil && a.Script == nil && a.Workdir == nil &&
		a.NoAgent == nil && a.Repeat == nil && a.Schedule == nil {
		return nil, errors.New("Nothing to update: no fields were given.")
	}
	if a.Prompt != nil {
		if msg := scanPrompt(*a.Prompt); msg != "" {
			return nil, errors.New(msg)
		}
	}
	script := job["script"]
	if a.Script != nil {
		script = nilIfEmpty(strings.TrimSpace(*a.Script))
		if err := h.checkScript(strings.TrimSpace(*a.Script)); err != nil {
			return nil, err
		}
	}
	if a.NoAgent != nil && *a.NoAgent && (script == nil || script == "") {
		return nil, errors.New("no_agent needs a script. Set script in the same update, or give the job one first.")
	}
	var workdir any
	if a.Workdir != nil {
		wd, err := normalizeWorkdir(*a.Workdir)
		if err != nil {
			return nil, err
		}
		workdir = nilIfEmpty(wd)
	}
	var sched cron.Schedule
	var next time.Time
	if a.Schedule != nil {
		var err error
		if sched, err = cron.ParseSchedule(*a.Schedule, h.now()); err != nil {
			return nil, err
		}
	}
	updated, err := h.change(id, func(j cron.Job) error {
		if a.Prompt != nil {
			j["prompt"] = *a.Prompt
			delete(j, "skills")
			delete(j, "skill")
		}
		if a.Name != nil {
			j["name"] = *a.Name
		}
		if a.Deliver != nil {
			j["deliver"] = strings.TrimSpace(*a.Deliver)
			if j["deliver"] == "" {
				j["deliver"] = "local"
			}
		}
		if a.Script != nil {
			j["script"] = script
		}
		if a.Workdir != nil {
			j["workdir"] = workdir
		}
		if a.NoAgent != nil {
			j["no_agent"] = *a.NoAgent
		}
		if a.Repeat != nil {
			rep, _ := j["repeat"].(map[string]any)
			rep2 := map[string]any{}
			for k, v := range rep {
				rep2[k] = v
			}
			rep2["times"] = nil
			if *a.Repeat > 0 {
				rep2["times"] = int(*a.Repeat)
			}
			j["repeat"] = rep2
		}
		if a.Schedule != nil {
			j["schedule"], j["schedule_display"] = sched.Map(), sched.Display
			if state, _ := j["state"].(string); state != "paused" {
				j["state"], j["enabled"] = "scheduled", true
				var ok bool
				if next, ok = cron.NextRun(sched, nil, h.now()); !ok && sched.Kind == "once" {
					return fmt.Errorf("The requested time, %s, is more than 120 seconds ago, so the job cannot be scheduled.", sched.RunAt)
				}
				j["next_run_at"] = nil
				if ok {
					j["next_run_at"] = cron.ISO(next)
				}
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return answer{"success": true, "job": formatJob(updated)}, nil
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func nilIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// normalizeWorkdir checks a job's working directory: absolute, existing, a
// directory. "" is no workdir.
func normalizeWorkdir(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", nil
	}
	if strings.HasPrefix(raw, "~") {
		if u, err := os.UserHomeDir(); err == nil {
			raw = filepath.Join(u, strings.TrimPrefix(raw, "~"))
		}
	}
	if !filepath.IsAbs(raw) {
		return "", fmt.Errorf("workdir must be an absolute path, not %q: a job does not start from any particular directory.", raw)
	}
	resolved, err := filepath.EvalSymlinks(raw)
	if err != nil {
		return "", fmt.Errorf("workdir %s does not exist", raw)
	}
	fi, err := os.Stat(resolved)
	if err != nil || !fi.IsDir() {
		return "", fmt.Errorf("workdir %s is not a directory", resolved)
	}
	return resolved, nil
}

// checkScript refuses a script path that is absolute, home-relative or
// leaves <home>/scripts.
func (h *cronHandler) checkScript(script string) error {
	if script == "" {
		return nil
	}
	if strings.HasPrefix(script, "/") || strings.HasPrefix(script, "~") {
		return fmt.Errorf("script must be a path inside %s/scripts/, but %q starts at the root or the home directory. Put the file in %s/scripts/ and name it relative to that.",
			displayHome(h.c.Home), script, displayHome(h.c.Home))
	}
	if _, err := cron.ScriptPath(h.c.Home, script); err != nil {
		return fmt.Errorf("script %q leaves the scripts directory", script)
	}
	return nil
}

// origin is where the calling chat is, for a job to deliver back to.
func (h *cronHandler) origin() any {
	rest, ok := strings.CutPrefix(h.c.Chat, "signal:")
	if !ok {
		return nil
	}
	var chat, user string
	if group, ok := strings.CutPrefix(rest, "group:"); ok {
		id, sender, _ := strings.Cut(group, ":")
		chat, user = "group:"+id, sender
	} else if dm, ok := strings.CutPrefix(rest, "dm:"); ok {
		chat, user = dm, dm
	} else {
		return nil
	}
	return map[string]any{"platform": "signal", "chat_id": chat, "chat_name": nil, "thread_id": nil, "user_id": nilIfEmpty(user)}
}

func repeatDisplay(j cron.Job) string {
	rep, _ := j["repeat"].(map[string]any)
	completed := 0
	switch v := rep["completed"].(type) {
	case float64:
		completed = int(v)
	case int:
		completed = v
	}
	var times int
	switch v := rep["times"].(type) {
	case nil:
		return "forever"
	case float64:
		times = int(v)
	case int:
		times = v
	}
	switch {
	case times == 1 && completed == 0:
		return "once"
	case times == 1:
		return "1/1"
	case completed > 0:
		return fmt.Sprintf("%d/%d", completed, times)
	}
	return fmt.Sprintf("%d times", times)
}

func formatJob(j cron.Job) map[string]any {
	prompt := fmt.Sprint(orEmpty(j["prompt"]))
	preview := prompt
	if r := []rune(prompt); len(r) > 100 {
		preview = string(r[:100]) + "..."
	}
	deliver := j["deliver"]
	if _, ok := j["deliver"]; !ok {
		deliver = "local"
	}
	enabled := true
	if b, ok := j["enabled"].(bool); ok {
		enabled = b
	}
	out := map[string]any{
		"job_id": j["id"], "name": j["name"], "prompt_preview": preview,
		"model": j["model"], "provider": j["provider"], "base_url": j["base_url"],
		"schedule": j["schedule_display"], "repeat": repeatDisplay(j), "deliver": deliver,
		"next_run_at": j["next_run_at"], "last_run_at": j["last_run_at"], "last_status": j["last_status"],
		"last_delivery_error": j["last_delivery_error"], "last_fire_error": j["last_fire_error"],
		"enabled": enabled, "state": j["state"], "paused_at": j["paused_at"], "paused_reason": j["paused_reason"],
	}
	for _, k := range []string{"script", "reasoning_effort", "workdir"} {
		if v, _ := j[k].(string); v != "" {
			out[k] = v
		}
	}
	if b, _ := j["no_agent"].(bool); b {
		out["no_agent"] = true
	}
	return out
}

func orEmpty(v any) any {
	if v == nil {
		return ""
	}
	return v
}

// ---- screening job prompts ----

// A job prompt is stored and later run with no one watching, so a prompt that
// tries to steer the model against its user, or to move secrets off the
// machine, is refused when the job is created or changed.

// secretRef matches a shell reference to a variable whose name suggests a
// secret, such as $API_KEY or ${GITHUB_TOKEN}.
const secretRef = `\$\{?[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)[A-Za-z0-9_]*\}?`

var promptThreats = []struct {
	name string
	re   *regexp.Regexp
}{
	{"override-instructions", regexp.MustCompile(`(?i)\b(?:ignore|disregard|forget)\b[^.\n]{0,60}\b(?:previous|prior|above|earlier|all|any|your)\b[^.\n]{0,40}\b(?:instructions|rules|guidelines)\b`)},
	{"conceal-from-user", regexp.MustCompile(`(?i)\b(?:do\s+not|don'?t|never)\s+(?:tell|inform|notify)\s+the\s+user\b`)},
	{"prompt-override", regexp.MustCompile(`(?i)system\s+prompt\s+override`)},
	{"read-secret-file", regexp.MustCompile(`(?i)\b(?:cat|less|more|head|tail|bat)\s+[^\n]*(?:\.env\b|credentials|\.netrc|\.pgpass|id_(?:rsa|ed25519|ecdsa))`)},
	{"ssh-key-install", regexp.MustCompile(`(?i)authorized_keys`)},
	{"sudo-config", regexp.MustCompile(`(?i)/etc/sudoers|\bvisudo\b`)},
	{"wipe-root", regexp.MustCompile(`(?i)\brm\s+-[a-z]*r[a-z]*\s+(?:-[a-z]+\s+)*/`)},
	{"secret-upload", regexp.MustCompile(`(?i)\b(?:curl|wget)\b[^\n]*` + secretRef)},
}

// githubTokenCall is the one authenticated request that may name a secret:
// a curl to api.github.com whose only secret is the token in its
// Authorization header.
var githubTokenCall = regexp.MustCompile("(?i)\\bcurl\\s+[^\\n;&|$`]*(?:-H|--header)\\s+[\"']Authorization:\\s*token\\s+" + secretRef +
	"[\"']\\s+[\"']?https://api\\.github\\.com(?::\\d+)?(?:/[^\\s;&|$`]*)?")

// hiddenRunes are code points that render as nothing (zero-width and
// direction-control characters), which can hide text from a reader.
var hiddenRunes = map[rune]bool{
	0x200b: true, 0x200c: true, 0x200d: true, 0x2060: true, 0x2062: true, 0x2063: true, 0x2064: true, 0xfeff: true,
	0x202a: true, 0x202b: true, 0x202c: true, 0x202d: true, 0x202e: true,
	0x2066: true, 0x2067: true, 0x2068: true, 0x2069: true,
}

func isEmojiRune(r rune) bool {
	return (r >= 0x1F000 && r <= 0x1FFFF) || (r >= 0x2600 && r <= 0x27BF) || (r >= 0x2300 && r <= 0x23FF) || r == 0x20E3
}

// joinsEmoji says whether the zero-width joiner at runes[i] ties two emoji
// together, which is legitimate (variation selectors may sit in between).
func joinsEmoji(runes []rune, i int) bool {
	l, r := i-1, i+1
	for l >= 0 && runes[l] == 0xFE0F {
		l--
	}
	for r < len(runes) && runes[r] == 0xFE0F {
		r++
	}
	return l >= 0 && r < len(runes) && isEmojiRune(runes[l]) && isEmojiRune(runes[r])
}

// scanPrompt returns the reason a job prompt is refused, "" when it is fine.
func scanPrompt(prompt string) string {
	runes := []rune(prompt)
	for i, r := range runes {
		if hiddenRunes[r] && !(r == 0x200d && joinsEmoji(runes, i)) {
			return fmt.Sprintf("Refused: the prompt contains the invisible character U+%04X, which can hide instructions.", r)
		}
	}
	cleaned := githubTokenCall.ReplaceAllString(prompt, "curl https://api.github.com/user")
	for _, p := range promptThreats {
		if p.re.MatchString(cleaned) {
			return fmt.Sprintf("Refused: the prompt matches the '%s' check. A scheduled prompt may not try to override instructions, hide things from the user, or send secrets out.", p.name)
		}
	}
	return ""
}
