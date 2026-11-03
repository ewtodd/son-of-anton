# Phase 6: cron

Goal: soa-d runs the instance's scheduled jobs from `cron/jobs.json`, the
same file (same format, same locks) the Python's `son-of-anton cron` and
`cronjob` write, and delivers results to Signal through its own chat
adapter (phase 5a). The `cronjob` tool lets an agent manage jobs.

Read first: `SPEC.md` section 13, `phases/05a-signal.md`, and the code. The
references are the Python `cron/jobs.py` (JOBS), `cron/scheduler.py` (SCH),
`tools/cronjob_tools.py` (CJT), `gateway/response_filters.py`. Where this
file and the Python differ, this file wins.

## Already written (do not modify)

`internal/cron/*_test.go` and `internal/server/cron_server_test.go`.

## Decisions made without the owner (2026-11-03, night)

- Scope from the owner's jobs: cron and interval schedules, one-shots,
  agent jobs, script jobs (`script`, `no_agent`), delivery to the origin
  chat, Signal targets and the home channel. Left out (each is kept as it is
  in the file, never dropped): `monitor_script`/`monitor_url`,
  `context_from`, the job notepad, `executions.db`, the usage audit, the
  model drift guard, preflight alerts, the failure-streak nudge,
  `attach_to_session`, `MEDIA:` attachments, `enabled_toolsets`.
- The run limit is the Python's real one, 600 s without any agent event
  (AGENTS.md's "3-minute hard interrupt" is not what the code does).
- Runs only with `[daemon] cron = true` (default false), and every tick
  holds the Python's own `cron/.tick.lock`, so the two schedulers can never
  tick at once while an instance moves.

## package cron (new)

```go
type Job map[string]any // a record as stored; unknown fields survive every write
func Load(home string) ([]Job, error)             // cron/jobs.json under the jobs lock
func Update(home string, f func([]Job) ([]Job, error)) error // read, change, write, under the lock
type Schedule struct { Kind string; Minutes int; Expr, RunAt, Display string }
func ParseSchedule(s string, now time.Time) (Schedule, error)
func NextRun(s Schedule, last *time.Time, now time.Time) (time.Time, bool)
func Due(jobs []Job, now time.Time) []string     // ids to fire now (and see Advance)
func Grace(s Schedule) time.Duration
func Silent(response string) bool
func Frame(name, id, content string) string
```

**The file** (JOBS:1045-1106, 1277-1282): `{"jobs": [...], "updated_at":
<now ISO>}` written with two-space indents, non-ASCII and `<>&` written
as they are (the Python's `ensure_ascii=False`), to a temp
file in `cron/` then renamed, mode 0600; read accepting a BOM, a bare list,
and records keyed `job_id` (rewritten to `id`). The lock is an exclusive
flock on `cron/.jobs.lock`, waited for up to 30 s (then an error, unlike the
Python's degraded mode). `cron/` and `cron/output/` are made 0700. Times are
ISO 8601 with the configured zone's offset (`SON_OF_ANTON_TIMEZONE`, else
config `timezone`, else local).

**Schedules** (JOBS:519-655): in order, `every <duration>` →
`{"kind": "interval", "minutes": N, "display": "every Nm"}`; five fields
each matching `^[\d*\-,/]+$` (more than five: an error) → `{"kind": "cron",
"expr", "display": expr}`; a string with `T` or starting `YYYY-MM-DD` →
`{"kind": "once", "run_at", "display": "once at 2006-01-02 15:04"}` (no
offset: the configured zone); a bare duration → `once` at now plus it,
display "once in <as given>"; anything else is an error listing the four
forms. Durations: `^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$`,
lowercased. Cron fields: numbers, `*`, ranges, lists, steps; day of month
and day of week combine as cron does (either matches when both are
restricted); evaluated in the configured zone.

**NextRun** (JOBS:865-916): once: `run_at` when never run and not more than
120 s past; interval: (last or now) plus the minutes; cron: the next match
after (last or now).

**Due** (JOBS:2758-3134, simplified): an enabled, unpaused job whose
`next_run_at` is not after now. A recurring job later than `Grace` (half its
period, clamped to 120 s - 2 h) still fires once, and its `next_run_at`
moves to the next slot after now (a backlog is one run). A one-shot fires
whenever it is due. A job with no `next_run_at` gets one computed (a
one-shot only within 120 s).

## Ticking and running (server)

- With `[daemon] cron = true`, a tick every 60 s (`[daemon] cron_tick`,
  seconds, for tests): take `cron/.tick.lock` (flock, non-blocking; held:
  skip this tick), find due jobs, advance recurring ones' `next_run_at`
  before running, and run each due job, different jobs in parallel, one job
  never twice at once.
- **Agent job:** a session of kind `cron`, source `cron`, cwd `workdir` or
  the home, model `job.model` (not "" ) else `cron.model` else
  `model.default`, title "<name, 60 chars> · Jan 02 15:04"; the full prompt
  (memory on) in the scope of where it delivers (gateway when it delivers
  to Signal, else common); every builtin tool and MCP except `cronjob`,
  `clarify` and `task`; nobody to ask (approvals deny, as `-z`). The user
  message is SCH:2551-2715's composition limited to the script block and the
  prompt: the cron hint (SCH:2698-2708, verbatim), then, for a job with a
  script, "## Script Output\nThe following data was collected by a pre-run
  script. Use it as context for your analysis.\n\n```\n<stdout>\n```\n\n" (or
  the "## Script Error" form on failure; empty stdout skips the run as
  silent), then the prompt. A last stdout line that is JSON
  `{"wakeAgent": false}` skips the agent (silent). 600 s without any agent
  event interrupts the run: "Cron job '<name>' idle for 600s".
- **Script job** (`no_agent`): the script's stdout is the result (empty, or
  `{"wakeAgent": false}` last, is silent); a failure is an error with its
  stderr. Scripts (`script` relative to `<home>/scripts/`, never outside it)
  run with `bash` for `.sh`/`.bash`, else `$SON_OF_ANTON_PYTHON` or
  `python3`, in the home, for at most 3600 s.
- **After a run** (JOBS:2162-2309): `last_run_at` now, `last_status` "ok" or
  "error", `last_error` (null on success), `last_delivery_error`,
  `failure_streak` (0 on success, else +1), `repeat.completed` +1, and for a
  finished one-shot or a reached `repeat.times`: `enabled` false, `state`
  "completed", `next_run_at` null; else `next_run_at` from the run's end.
  The output is saved as `cron/output/<id>/2006-01-02_15-04-05.md` (SCH:4112-4125:
  "# Cron Job: <name>", "**Job ID:**", "**Run Time:**", "**Schedule:**",
  "## Prompt", "## Response"; "(FAILED)" and "## Error" on failure), the
  newest 50 kept.

## Delivery

- `deliver`: `local` (nothing), `origin` (the origin chat: `origin.platform`
  "signal", `origin.chat_id`; none: the home channel; none either: local),
  `signal` (the home channel, else the origin chat), `signal:<chat id>`
  (`group:<id>` or a number), comma-separated lists of those. The home
  channel is `SIGNAL_HOME_CHANNEL`, else `group:<id>` when
  `SIGNAL_GROUP_ALLOWED_USERS` names exactly one group.
- Sent through the daemon's Signal client (and its echo filter), only when
  this daemon serves Signal; otherwise `last_delivery_error` is "Signal is
  not served by this daemon".
- `Frame` (SCH:1809-1817), exactly: "Cronjob Response: <name>\n(job_id:
  <id>)\n-------------\n\n<content>\n\nTo stop or manage this job, send me a
  new message (e.g. \"stop reminder <name>\")."; the content through phase
  5a's `Format`.
- `Silent` (response_filters.py:18-23, 72-110): the tokens `[SILENT]`,
  `SILENT`, `NO_REPLY`, `NO REPLY` (case-insensitive, whitespace collapsed)
  as the whole response, or as its first or last non-empty line, or a
  response starting `[SILENT]`. A silent success delivers nothing and stays
  "ok"; an empty success is an error ("Agent completed but produced empty
  response"); a failure always delivers "⚠️ Cron '<name>' failed: <the error,
  180 chars>", framed like any delivery.

## The cronjob tool

CJT:1398-1476's schema, verbatim, without `continuity`, `monitor_script`,
`monitor_url`, `context_from`, `enabled_toolsets` and `attach_to_session`
(and their mentions in the description). Actions `create`, `list`,
`update`, `pause`, `resume`, `remove`, `run` (CJT:1005-1394; their answer
shapes and messages, `job_id` by id or by unique name). `create` makes the
Python's record (JOBS:1757-1808: id 12 hex, every field it writes, `origin`
from the calling chat when there is one, `deliver` "origin" then, else
"local"). `run` fires the job now in the background and answers at once.
Offered to interactive, chat and worker sessions; not to cron sessions.

## README

Status: cron moves to "On soa-d now (off until an instance moves;
`[daemon] cron = true`)".

## Done when

All gates pass: the Go gates in `daemon/AGENTS.md`, `ruff check .` and
`ruff check --select F401,F811,F821,F841 .`, `scripts/run_tests.sh` (the whole
suite), and `nix flake check`. One commit: summary `daemon: phase 6 (cron)`,
body listing what changed, ending with the `Model:` line.
