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

// cronjobTool is ../tools/cronjob_tools.py.
type cronjobTool struct{ home string }

// CronJob is the cronjob tool for the instance at home.
func CronJob(home string) Tool { return cronjobTool{home: home} }

// displayHome is the home as the Python prints it: under the user's home as
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
    "action": {"type": "string", "description": "create, list, update, pause, resume, remove or run."},
    "job_id": {"type": "string", "description": "Required for update, pause, resume, remove and run."},
    "prompt": {"type": "string", "description": "create: the job's self-contained prompt. run: extra context for that one run."},
    "schedule": {"type": "string", "description": "Required for create. A delay ('30m' = once, 30 minutes from now), an interval ('every 2h', repeats), a cron expression ('0 9 * * *', repeats) or a timestamp ('2026-06-01T09:00:00', once). A timestamp without a UTC offset is local time in your system prompt's timezone: write the time the user said, don't convert it to UTC. Use a repeating form only when the user asks for a repeat."},
    "name": {"type": "string", "description": "Short label."},
    "repeat": {"type": "integer", "description": "Number of runs. Default: once for one-time schedules, forever for repeating ones."},
    "deliver": {"type": "string", "description": "Where results go. Omit to reply in this chat, which is almost always right. 'local' saves without sending, 'all' sends to every connected home channel, 'signal:<chat_id>' to a specific chat; combine with commas ('origin,all')."},
    "script": {"type": "string", "description": ` + quote("Script run before each run: its stdout is added to the prompt, or with no_agent it is the message. Relative paths are under "+displayHome(t.home)+"/scripts/; .sh/.bash run with bash, others with Python. Empty string clears it on update.") + `},
    "no_agent": {"type": "boolean", "default": false, "description": "Run only the script, no model: non-empty stdout is sent as the message, empty stdout sends nothing, a failure sends an alert. Needs script; prompt is ignored. For watchdogs whose script prints the message; anything that needs judgement needs the agent."},
    "workdir": {"type": "string", "description": "Existing absolute path the job runs in: its AGENTS.md is loaded and the terminal and file tools start there. Such jobs run one at a time. Empty string clears it."}
  },
  "required": ["action"]
}`
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "cronjob",
		Description: "Schedule and manage jobs that run a prompt later or on a schedule. Actions: create (needs schedule and prompt), list, update, pause, resume, remove, run.\n\n" +
			"A job runs in a fresh session with no chat context and no one to answer questions: its prompt must be self-contained, and its final response is what gets delivered. " +
			"To change or stop a job, list first to get its job_id (never guess one); prefer updating a job over creating a near-duplicate. " +
			"run fires a job now in the background and its result arrives later as a new message, so don't wait or poll for it.",
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
	Action          string   `json:"action"`
	JobID           string   `json:"job_id"`
	Prompt          *string  `json:"prompt"`
	Schedule        *string  `json:"schedule"`
	Name            *string  `json:"name"`
	Repeat          *float64 `json:"repeat"`
	Deliver         *string  `json:"deliver"`
	Script          *string  `json:"script"`
	NoAgent         *bool    `json:"no_agent"`
	Workdir         *string  `json:"workdir"`
	Reason          *string  `json:"reason"`
	IncludeDisabled *bool    `json:"include_disabled"`
}

// indented is the Python's json.dumps(indent=2, ensure_ascii=False).
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
		out := []any{}
		for _, j := range jobs {
			n := cron.Normalize(j)
			if a.IncludeDisabled != nil && !*a.IncludeDisabled && !cron.Enabled(n) {
				continue
			}
			out = append(out, formatJob(n))
		}
		return answer{"success": true, "count": len(out), "jobs": out}, nil
	}
	if a.JobID == "" {
		return nil, fmt.Errorf("job_id is required for action '%s'", action)
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
			"error": fmt.Sprintf("Job name '%s' is ambiguous — matches %d jobs: %s. Use the job ID instead.", a.JobID, len(matches), strings.Join(ids, ", "))}, nil
	}
	if job == nil {
		return answer{"success": false, "error": fmt.Sprintf("Job with ID or name '%s' not found. Use cronjob(action='list') to inspect jobs.", a.JobID)}, nil
	}
	job = cron.Normalize(job)
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
			return nil, fmt.Errorf("Cannot resume: one-shot time %s is in the past (grace window: 120s) and will never fire.", cron.ScheduleOf(job).RunAt)
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
	return nil, fmt.Errorf("Unknown cron action '%s'", a.Action)
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
		return nil, fmt.Errorf("Job with ID or name '%s' not found. Use cronjob(action='list') to inspect jobs.", id)
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
		return nil, fmt.Errorf("Failed to remove job '%s'", id)
	}
	if id != "" && !strings.ContainsAny(id, "/\\") && id != "." && id != ".." {
		_ = os.RemoveAll(filepath.Join(cron.Dir(h.c.Home), "output", id))
	}
	return answer{"success": true, "message": fmt.Sprintf("Cron job '%s' removed.", job["name"]),
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
		f["execution_skipped"] = "Already running; not run again."
		return answer{"success": true, "job": f}, nil
	}
	f["executed"] = true
	f["execution_mode"] = "background"
	return answer{"success": true, "job": f, "note": "The job is running in the background. You and the user can keep working; " +
		"its outcome is delivered as the job's deliver setting says and recorded in its last_status. " +
		"Do not wait or poll, just continue."}, nil
}

func (h *cronHandler) create(a cronArgs) (any, error) {
	if a.Schedule == nil || *a.Schedule == "" {
		return nil, errors.New("schedule is required for create")
	}
	prompt, script := deref(a.Prompt), strings.TrimSpace(deref(a.Script))
	noAgent := a.NoAgent != nil && *a.NoAgent
	if noAgent {
		if script == "" {
			return nil, errors.New("create with no_agent=True requires a script — the script is the job.")
		}
	} else if prompt == "" {
		return nil, errors.New("create requires a prompt")
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
		return nil, fmt.Errorf("Requested one-shot time %s is more than 120s in the past and cannot be scheduled.", sched.RunAt)
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
	message := fmt.Sprintf("Cron job '%s' created.", name)
	if strings.ToLower(userDeliver) != "local" {
		if targets, _ := cron.ResolveTargets(job, h.c.HomeChannel); len(targets) == 0 {
			message += " This is a local-only cron job: its output is saved (view it with cronjob(action='list')) " +
				"but will NOT be delivered back into this session. To be notified when it runs, recreate or update " +
				"the job with deliver set to a Signal chat, e.g. deliver='signal' or deliver='signal:<chat_id>'."
		}
	}
	return answer{"success": true, "job_id": job["id"], "name": name, "schedule": sched.Display, "repeat": repeatDisplay(job),
		"deliver": deliver, "next_run_at": job["next_run_at"], "job": formatJob(cron.Normalize(job)), "message": message}, nil
}

func (h *cronHandler) update(id string, job cron.Job, a cronArgs) (any, error) {
	if a.Prompt == nil && a.Name == nil && a.Deliver == nil && a.Script == nil && a.Workdir == nil &&
		a.NoAgent == nil && a.Repeat == nil && a.Schedule == nil {
		return nil, errors.New("No updates provided.")
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
		return nil, errors.New("Cannot set no_agent=True on a job without a script. Set `script` in the same update, or on the job first.")
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
					return fmt.Errorf("Requested one-shot time %s is more than 120s in the past and cannot be scheduled.", sched.RunAt)
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
		return "", fmt.Errorf("Cron workdir must be an absolute path (got %q). Cron jobs run detached from any shell cwd, so relative paths are ambiguous.", raw)
	}
	resolved, err := filepath.EvalSymlinks(raw)
	if err != nil {
		return "", fmt.Errorf("Cron workdir does not exist: %s", raw)
	}
	fi, err := os.Stat(resolved)
	if err != nil || !fi.IsDir() {
		return "", fmt.Errorf("Cron workdir is not a directory: %s", resolved)
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
		return fmt.Errorf("Script path must be relative to %s/scripts/. Got absolute or home-relative path: %q. Place scripts in %s/scripts/ and use just the filename.",
			displayHome(h.c.Home), script, displayHome(h.c.Home))
	}
	if _, err := cron.ScriptPath(h.c.Home, script); err != nil {
		return fmt.Errorf("Script path escapes the scripts directory via traversal: %q", script)
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

// ---- the prompt scan (CJT:_scan_cron_prompt) ----

const secretVar = `\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)\w*\}?`

var threatPatterns = []struct {
	re *regexp.Regexp
	id string
}{
	{regexp.MustCompile(`(?i)ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions`), "prompt_injection"},
	{regexp.MustCompile(`(?i)do\s+not\s+tell\s+the\s+user`), "deception_hide"},
	{regexp.MustCompile(`(?i)system\s+prompt\s+override`), "sys_prompt_override"},
	{regexp.MustCompile(`(?i)disregard\s+(your|all|any)\s+(instructions|rules|guidelines)`), "disregard_rules"},
	{regexp.MustCompile(`(?i)cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|id_rsa|id_ed25519|id_ecdsa)`), "read_secrets"},
	{regexp.MustCompile(`(?i)authorized_keys`), "ssh_backdoor"},
	{regexp.MustCompile(`(?i)/etc/sudoers|visudo`), "sudoers_mod"},
	{regexp.MustCompile(`(?i)rm\s+-rf\s+/`), "destructive_root_rm"},
	{regexp.MustCompile("(?i)curl\\s+[^\\n]*https?://[^\\s\"'`]*" + secretVar), "exfil_curl_url"},
	{regexp.MustCompile("(?i)wget\\s+[^\\n]*https?://[^\\s\"'`]*" + secretVar), "exfil_wget_url"},
	{regexp.MustCompile(`(?i)curl\s+[^\n]*(?:--data(?:-raw|-binary|-urlencode)?|-d|--form|-F)\s+[^\n]*` + secretVar), "exfil_curl_data"},
	{regexp.MustCompile(`(?i)wget\s+[^\n]*--post-(?:data|file)=[^\n]*` + secretVar), "exfil_wget_post"},
	{regexp.MustCompile(`(?i)curl\s+[^\n]*(?:-H|--header)\s+["']Authorization:\s*(?:Bearer|token)\s+` + secretVar + `["']`), "exfil_curl_auth_header"},
}

// githubAuth is the one authenticated call the exfil rules let through.
var githubAuth = regexp.MustCompile("(?i)curl\\s+[^\\n;&|$`]*(?:-H|--header)\\s+[\"']Authorization:\\s*token\\s+" + secretVar +
	"[\"']\\s+[\"']?https://api\\.github\\.com(?::\\d+)?(?:/|\\s|$|[\"'])[^\\s;&|$`]*")

var invisibleChars = string([]rune{0x200b, 0x200c, 0x200d, 0x2060, 0x2062, 0x2063, 0x2064, 0xfeff, 0x202a, 0x202b, 0x202c, 0x202d, 0x202e, 0x2066, 0x2067, 0x2068, 0x2069})

func isEmojiRune(r rune) bool {
	return (r >= 0x1F000 && r <= 0x1FFFF) || (r >= 0x2600 && r <= 0x27BF) || (r >= 0x2300 && r <= 0x23FF) || r == 0x20E3
}

// zwjInEmoji says whether the joiner at runes[i] sits between emoji.
func zwjInEmoji(runes []rune, i int) bool {
	l, r := i-1, i+1
	for l >= 0 && runes[l] == 0xFE0F {
		l--
	}
	for r < len(runes) && runes[r] == 0xFE0F {
		r++
	}
	return l >= 0 && r < len(runes) && isEmojiRune(runes[l]) && isEmojiRune(runes[r])
}

// scanPrompt returns why a job prompt is refused, "" when it is fine.
func scanPrompt(prompt string) string {
	prompt = githubAuth.ReplaceAllString(prompt, "curl https://api.github.com/user")
	runes := []rune(prompt)
	for i, r := range runes {
		if strings.ContainsRune(invisibleChars, r) && !(r == 0x200d && zwjInEmoji(runes, i)) {
			return fmt.Sprintf("Blocked: prompt contains invisible unicode U+%04X (possible injection).", r)
		}
	}
	for _, p := range threatPatterns {
		if p.re.MatchString(prompt) {
			return fmt.Sprintf("Blocked: prompt matches threat pattern '%s'. Cron prompts must not contain injection or exfiltration payloads.", p.id)
		}
	}
	return ""
}
