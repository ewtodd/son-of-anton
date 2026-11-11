package tools

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"unicode/utf8"

	"github.com/ewtodd/son-of-anton/daemon/internal/cron"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

// ReminderKind marks a cron job that is a reminder: at its time the daemon
// sends ReminderPrefix and its message to the chat that set it, and no model
// runs. The cronjob tool neither lists nor changes such jobs.
const (
	ReminderKind   = "reminder"
	ReminderPrefix = "⏰ Reminder: "
)

// IsReminder says whether j is a reminder (ReminderKind).
func IsReminder(j cron.Job) bool { k, _ := j["kind"].(string); return k == ReminderKind }

// Reminder is the reminder tool: plain reminders a chat sets for itself.
func Reminder() Tool { return reminderTool{} }

type reminderTool struct{}

func (reminderTool) Schema() provider.Tool {
	params := `{
  "type": "object",
  "properties": {
    "action": {"type": "string", "description": "What to do: set, list, change or cancel."},
    "text": {"type": "string", "description": "For set and change: what to remind the user of, written to them, for example \"Text Uncle Mike and Daniel.\" It is sent as it is, after \"` + ReminderPrefix + `\"."},
    "when": {"type": "string", "description": "For set and change: when to send it. A delay such as '1h' or '30m'; a time such as '2026-11-17T19:30:00', in local time as the user said it (do not convert it); or, for a reminder that repeats, 'every 2h' or a five-field cron expression such as '0 9 * * 1'."},
    "reminder_id": {"type": "string", "description": "For change and cancel: the reminder's id, from set or list."}
  },
  "required": ["action"]
}`
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "reminder",
		Description: "Remind the user of something at a time. When the time comes, the text is sent to this chat as it is. " +
			"Nothing else happens then: no model reads it and nothing is done for the user. " +
			"Use this whenever someone asks to be reminded of something. " +
			"For work that has to be done at a later time (checking something, writing a summary), use cronjob instead. " +
			"Actions: set (needs text and when), list, change (needs reminder_id, and text or when), cancel (needs reminder_id).",
		Parameters: json.RawMessage(params),
	}}
}

type reminderArgs struct {
	Action     string  `json:"action"`
	Text       *string `json:"text"`
	When       *string `json:"when"`
	ReminderID string  `json:"reminder_id"`
}

func (reminderTool) Run(ctx context.Context, env *Env, raw json.RawMessage) (string, error) {
	if env.Cron == nil {
		return errorResult("reminders are not available in this session"), nil
	}
	var a reminderArgs
	if err := json.Unmarshal(raw, &a); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	h := &cronHandler{c: env.Cron, zone: cron.Zone(env.Cron.Home)}
	res, err := h.reminder(a)
	if err != nil {
		return toolError(err.Error())
	}
	return indented(res)
}

func (h *cronHandler) reminder(a reminderArgs) (any, error) {
	origin, _ := h.origin().(map[string]any)
	if origin == nil {
		return nil, errors.New("A reminder is sent to the chat that sets it, and this session is not a chat.")
	}
	switch action := strings.ToLower(strings.TrimSpace(a.Action)); action {
	case "set":
		return h.setReminder(a, origin)
	case "list":
		jobs, err := cron.Load(h.c.Home)
		if err != nil {
			return nil, err
		}
		out := []any{}
		for _, j := range jobs {
			if n := cron.Normalize(j); IsReminder(n) && cron.Enabled(n) && sameChat(n, origin) {
				out = append(out, formatReminder(n))
			}
		}
		return answer{"success": true, "count": len(out), "reminders": out}, nil
	case "change", "cancel":
		job, err := h.ownReminder(a.ReminderID, origin)
		if err != nil {
			return nil, err
		}
		if action == "cancel" {
			if _, err := h.remove(job["id"].(string), job); err != nil {
				return nil, err
			}
			return answer{"success": true, "message": "Cancelled the reminder.", "cancelled": formatReminder(job)}, nil
		}
		return h.changeReminder(job, a)
	default:
		return nil, fmt.Errorf("'%s' is not a reminder action: use set, list, change or cancel", a.Action)
	}
}

func (h *cronHandler) setReminder(a reminderArgs, origin map[string]any) (any, error) {
	text := strings.TrimSpace(deref(a.Text))
	if text == "" {
		return nil, errors.New("set needs the text of the reminder")
	}
	if strings.TrimSpace(deref(a.When)) == "" {
		return nil, errors.New("set needs when")
	}
	now := h.now()
	sched, err := cron.ParseSchedule(deref(a.When), now)
	if err != nil {
		return nil, err
	}
	next, ok := cron.NextRun(sched, nil, now)
	if sched.Kind == "once" && !ok {
		return nil, fmt.Errorf("That time, %s, has already passed.", sched.RunAt)
	}
	var times any
	if sched.Kind == "once" {
		times = 1
	}
	var b [6]byte
	if _, err := rand.Read(b[:]); err != nil {
		return nil, err
	}
	job := cron.Job{
		"id": hex.EncodeToString(b[:]), "kind": ReminderKind, "name": reminderName(text), "message": text, "prompt": "",
		"script": nil, "no_agent": false, "schedule": sched.Map(), "schedule_display": sched.Display,
		"repeat": map[string]any{"times": times, "completed": 0}, "enabled": true, "state": "scheduled",
		"paused_at": nil, "paused_reason": nil, "created_at": cron.ISO(now), "next_run_at": cron.ISO(next),
		"last_run_at": nil, "last_status": nil, "last_error": nil, "last_delivery_error": nil, "failure_streak": 0,
		"deliver": "origin", "origin": origin, "workdir": nil,
	}
	if err := cron.Update(h.c.Home, func(jobs []cron.Job) ([]cron.Job, error) { return append(jobs, job), nil }); err != nil {
		return nil, err
	}
	return answer{"success": true, "message": "Set the reminder.", "reminder": formatReminder(cron.Normalize(job))}, nil
}

func (h *cronHandler) changeReminder(job cron.Job, a reminderArgs) (any, error) {
	if a.Text == nil && a.When == nil {
		return nil, errors.New("change needs text or when")
	}
	text := strings.TrimSpace(deref(a.Text))
	if a.Text != nil && text == "" {
		return nil, errors.New("the text of a reminder cannot be empty")
	}
	var sched cron.Schedule
	if a.When != nil {
		var err error
		if sched, err = cron.ParseSchedule(*a.When, h.now()); err != nil {
			return nil, err
		}
	}
	updated, err := h.change(job["id"].(string), func(j cron.Job) error {
		if a.Text != nil {
			j["message"], j["name"] = text, reminderName(text)
		}
		if a.When != nil {
			next, ok := cron.NextRun(sched, nil, h.now())
			if sched.Kind == "once" && !ok {
				return fmt.Errorf("That time, %s, has already passed.", sched.RunAt)
			}
			var times any
			if sched.Kind == "once" {
				times = 1
			}
			j["schedule"], j["schedule_display"] = sched.Map(), sched.Display
			j["repeat"] = map[string]any{"times": times, "completed": 0}
			j["state"], j["enabled"], j["next_run_at"] = "scheduled", true, cron.ISO(next)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return answer{"success": true, "message": "Changed the reminder.", "reminder": formatReminder(updated)}, nil
}

// ownReminder is the reminder id names, if it belongs to the chat at origin.
func (h *cronHandler) ownReminder(id string, origin map[string]any) (cron.Job, error) {
	if strings.TrimSpace(id) == "" {
		return nil, errors.New("this action needs a reminder_id; list the reminders to find it")
	}
	jobs, err := cron.Load(h.c.Home)
	if err != nil {
		return nil, err
	}
	for _, j := range jobs {
		if j["id"] == id && IsReminder(j) && sameChat(j, origin) {
			return cron.Normalize(j), nil
		}
	}
	return nil, fmt.Errorf("No reminder of this chat has the id '%s'. List the reminders to see them.", id)
}

// sameChat says whether job j delivers to the chat at origin.
func sameChat(j cron.Job, origin map[string]any) bool {
	o, _ := j["origin"].(map[string]any)
	return o != nil && fmt.Sprint(o["chat_id"]) == fmt.Sprint(origin["chat_id"])
}

func reminderName(text string) string {
	if utf8.RuneCountInString(text) > 50 {
		return "Reminder: " + strings.TrimSpace(string([]rune(text)[:50])) + "..."
	}
	return "Reminder: " + text
}

func formatReminder(j cron.Job) map[string]any {
	return map[string]any{"reminder_id": j["id"], "text": j["message"], "when": j["schedule_display"],
		"next": j["next_run_at"], "repeats": repeatDisplay(j) != "once" && repeatDisplay(j) != "1/1"}
}
