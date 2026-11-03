package server

// Cron (phase 6): the daemon runs the instance's scheduled jobs from
// cron/jobs.json (the file the `son-of-anton cron` command also uses), and
// delivers their results to Signal through the chat hub's client.

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/agent"
	"github.com/ewtodd/son-of-anton/daemon/internal/cron"
	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
	"github.com/ewtodd/son-of-anton/daemon/internal/signal"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// cronHint opens every agent job's prompt. It tells the model that nobody is
// watching the run, that its final reply is what gets delivered, and that a
// reply of exactly [SILENT] (see cron.Silent) delivers nothing.
const cronHint = "[Scheduled run. No one is present and no one can answer questions. " +
	"Whatever you reply last is sent to the user as it stands, so make it a finished report " +
	"and do not try to send it yourself. " +
	"If there is nothing worth telling the user, reply with the single word [SILENT] " +
	"and nothing else; that sends nothing. [SILENT] must never be mixed with other text.]\n\n"

// cronIdle is how long an agent job may go without any event.
var cronIdle = 600 * time.Second

const cronTickDefault = 60 * time.Second

// cronSched ticks the jobs file and runs what is due.
type cronSched struct {
	srv   *Server
	every time.Duration

	mu      sync.Mutex
	running map[string]bool // job ids with a run in flight

	workdirMu sync.Mutex // jobs with a workdir run one at a time
	wg        sync.WaitGroup
}

func (s *Server) newCron() *cronSched {
	every := cronTickDefault
	if n := s.cfg.Daemon.CronTick; n > 0 {
		every = time.Duration(n) * time.Second
	}
	return &cronSched{srv: s, every: every, running: map[string]bool{}}
}

// cronHost is what the cronjob tool gets in a session; chat is the stamp of
// the chat the session serves, "" for none.
func (s *Server) cronHost(chat string) *tools.Cron {
	return &tools.Cron{Home: s.opt.Home, Chat: chat, HomeChannel: s.signalHome(), Run: s.cron.fire}
}

// signalHome is the chat that "signal" and "all" deliver to: SIGNAL_HOME_CHANNEL,
// else the one group SIGNAL_GROUP_ALLOWED_USERS names.
func (s *Server) signalHome() string {
	if v := strings.TrimSpace(s.cfg.Getenv("SIGNAL_HOME_CHANNEL")); v != "" {
		return v
	}
	groups := signal.ConfigFromEnv(s.cfg.Getenv).Groups
	if len(groups) == 1 && groups[0] != "*" {
		return "group:" + groups[0]
	}
	return ""
}

// start runs the tick loop until the server's context ends.
func (c *cronSched) start() {
	c.wg.Add(1)
	go func() {
		defer c.wg.Done()
		for {
			c.tick()
			select {
			case <-c.srv.base.Done():
				return
			case <-time.After(c.every):
			}
		}
	}()
}

// stop waits for the tick loop to end (the server's context is cancelled).
func (c *cronSched) stop() { c.wg.Wait() }

// tick finds the due jobs under the tick lock, moves recurring ones to their
// next slot before anything runs, and starts each job not already running.
func (c *cronSched) tick() {
	s := c.srv
	if s.base.Err() != nil {
		return
	}
	release, ok, err := cron.TryTickLock(s.opt.Home)
	if err != nil {
		log.Printf("cron: tick lock: %v", err)
		return
	}
	if !ok {
		return
	}
	defer release()
	now := time.Now().In(cron.Zone(s.opt.Home))
	var due []cron.Job
	err = cron.Update(s.opt.Home, func(jobs []cron.Job) ([]cron.Job, error) {
		before, _ := json.Marshal(jobs)
		ids := cron.Due(jobs, now)
		for _, id := range ids {
			for _, j := range jobs {
				if j["id"] != id {
					continue
				}
				if sched := cron.ScheduleOf(j); sched.Kind == "cron" || sched.Kind == "interval" {
					if next, ok := cron.NextRun(sched, &now, now); ok {
						j["next_run_at"] = cron.ISO(next)
					}
				}
				due = append(due, cron.Normalize(j))
			}
		}
		if after, _ := json.Marshal(jobs); bytes.Equal(before, after) {
			return nil, cron.ErrNoChange
		}
		return jobs, nil
	})
	if err != nil {
		log.Printf("cron: tick: %v", err)
		return
	}
	for _, j := range due {
		c.start1(j, "")
	}
}

// fire starts a job now (the cronjob tool's run); false when it is already
// running or does not exist.
func (c *cronSched) fire(id, extra string) bool {
	jobs, err := cron.Load(c.srv.opt.Home)
	if err != nil {
		return false
	}
	for _, j := range jobs {
		if j["id"] == id {
			return c.start1(cron.Normalize(j), extra)
		}
	}
	return false
}

// start1 runs a job in the background unless it is already running.
func (c *cronSched) start1(job cron.Job, extra string) bool {
	s := c.srv
	id, _ := job["id"].(string)
	if s.base.Err() != nil {
		return false
	}
	c.mu.Lock()
	if c.running[id] {
		c.mu.Unlock()
		return false
	}
	c.running[id] = true
	c.mu.Unlock()
	s.turns.Add(1)
	go func() {
		defer s.turns.Done()
		defer func() {
			c.mu.Lock()
			delete(c.running, id)
			c.mu.Unlock()
		}()
		defer func() {
			if r := recover(); r != nil {
				log.Printf("cron: job %s panicked: %v", id, r)
			}
		}()
		c.runJob(job, extra)
	}()
	return true
}

// outcome is how a run ended.
type outcome struct {
	ok       bool
	silent   bool   // nothing to deliver
	response string // what is delivered
	errMsg   string
	doc      string // the saved run document
}

func (c *cronSched) runJob(job cron.Job, extra string) {
	s := c.srv
	home := s.opt.Home
	id, _ := job["id"].(string)
	if wd, _ := job["workdir"].(string); wd != "" {
		c.workdirMu.Lock()
		defer c.workdirMu.Unlock()
	}
	var o outcome
	if no, _ := job["no_agent"].(bool); no {
		o = c.scriptRun(job)
	} else {
		o = c.agentRun(job, extra)
	}
	if s.base.Err() != nil {
		return // the daemon is stopping: leave the job as it is
	}
	zone := cron.Zone(home)
	if err := cron.SaveOutput(home, id, o.doc, time.Now().In(zone)); err != nil {
		log.Printf("cron: saving the output of %s: %v", id, err)
	}
	deliveryErr := c.deliver(job, o)
	end := time.Now().In(zone)
	err := cron.Update(home, func(jobs []cron.Job) ([]cron.Job, error) {
		for _, j := range jobs {
			if j["id"] == id {
				recordRun(j, o, deliveryErr, end)
				return jobs, nil
			}
		}
		return nil, cron.ErrNoChange // removed while it ran
	})
	if err != nil {
		log.Printf("cron: recording the run of %s: %v", id, err)
	}
}

// recordRun writes a finished run into its job.
func recordRun(j cron.Job, o outcome, deliveryErr string, end time.Time) {
	j["last_run_at"] = cron.ISO(end)
	if o.ok {
		j["last_status"], j["last_error"], j["failure_streak"] = "ok", nil, 0
	} else {
		streak := 0
		if n, ok := j["failure_streak"].(float64); ok {
			streak = int(n)
		}
		j["last_status"], j["last_error"], j["failure_streak"] = "error", o.errMsg, streak+1
	}
	j["last_delivery_error"] = nil
	if deliveryErr != "" {
		j["last_delivery_error"] = deliveryErr
	}
	if _, ok := j["run_claim"]; ok {
		j["run_claim"] = nil
	}
	if rep, ok := j["repeat"].(map[string]any); ok {
		completed := 0
		if n, ok := rep["completed"].(float64); ok {
			completed = int(n)
		}
		completed++
		rep["completed"] = completed
		if times, ok := rep["times"].(float64); ok && times > 0 && float64(completed) >= times {
			j["enabled"], j["state"], j["next_run_at"] = false, "completed", nil
			return
		}
	}
	sched := cron.ScheduleOf(j)
	if next, ok := cron.NextRun(sched, &end, end); ok {
		j["next_run_at"] = cron.ISO(next)
		if state, _ := j["state"].(string); state != "paused" {
			j["state"] = "scheduled"
		}
		return
	}
	j["next_run_at"] = nil
	if sched.Kind == "cron" || sched.Kind == "interval" {
		j["state"] = "error"
		if e, _ := j["last_error"].(string); e == "" {
			j["last_error"] = "Could not work out the next run time for this repeating schedule"
		}
		return
	}
	j["enabled"], j["state"] = false, "completed"
}

func jobText(j cron.Job, key string) string {
	s, _ := j[key].(string)
	return strings.TrimSpace(s)
}

// header is the document's opening lines.
func header(job cron.Job, suffix string, at time.Time) string {
	return fmt.Sprintf("# Cron Job: %s%s\n\n**Job ID:** %s\n**Run Time:** %s\n", job["name"], suffix, job["id"], at.Format("2006-01-02 15:04:05"))
}

func (c *cronSched) scriptRun(job cron.Job) outcome {
	s := c.srv
	at := time.Now().In(cron.Zone(s.opt.Home))
	out, ok := cron.RunScript(s.base, s.opt.Home, jobText(job, "script"), jobText(job, "workdir"))
	mode := header(job, "", at) + "**Run:** the script alone (no_agent)\n"
	switch {
	case !ok:
		return outcome{errMsg: out, doc: mode + "**Result:** the script failed\n\n" + out + "\n"}
	case !cron.WakeGate(out):
		return outcome{ok: true, silent: true, doc: mode + "**Result:** nothing to deliver (wakeAgent=false)\n"}
	case strings.TrimSpace(out) == "":
		return outcome{ok: true, silent: true, doc: mode + "**Result:** nothing to deliver (no output)\n"}
	}
	return outcome{ok: true, response: out, doc: mode + "\n---\n\n" + out + "\n"}
}

// jobPrompt is the user message of an agent job: the cron hint, the script
// block and the prompt. skip says the run is silent before any model call.
func (c *cronSched) jobPrompt(job cron.Job, extra string) (text string, skip *outcome) {
	s := c.srv
	user := fmt.Sprint(job["prompt"])
	if extra != "" {
		user += "\n\n## Extra instructions for this run\n" + extra
	}
	body := user
	if script := jobText(job, "script"); script != "" {
		at := time.Now().In(cron.Zone(s.opt.Home))
		out, ok := cron.RunScript(s.base, s.opt.Home, script, jobText(job, "workdir"))
		switch {
		case ok && !cron.WakeGate(out):
			return "", &outcome{ok: true, silent: true,
				doc: header(job, "", at) + "\nThe script's gate said wakeAgent=false, so the agent did not run.\n"}
		case ok && out == "":
			return "", &outcome{ok: true, silent: true,
				doc: header(job, "", at) + "\nThe script printed nothing, so the agent did not run.\n"}
		case ok:
			body = "## Script output\nA script ran just before this job and printed the text below. " +
				"Treat it as input for the task.\n\n```\n" + out + "\n```\n\n" + user
		default:
			body = "## Script failure\nThe script that runs before this job failed and printed the text below. Tell the user about the failure.\n\n```\n" + out + "\n```\n\n" + user
		}
	}
	return cronHint + body, nil
}

func (c *cronSched) agentRun(job cron.Job, extra string) (o outcome) {
	s := c.srv
	zone := cron.Zone(s.opt.Home)
	name := fmt.Sprint(job["name"])
	userPrompt, skip := c.jobPrompt(job, extra)
	if skip != nil {
		return *skip
	}
	fail := func(msg string) outcome {
		return outcome{errMsg: msg, doc: header(job, " (FAILED)", time.Now().In(zone)) +
			fmt.Sprintf("**Schedule:** %v\n\n## Prompt\n\n%s\n\n## Error\n\n```\n%s\n```\n", orNA(job["schedule_display"]), userPrompt, msg)}
	}
	if strings.TrimSpace(fmt.Sprint(job["prompt"])) == "" && extra == "" {
		return fail("The job has no prompt.")
	}
	model := jobText(job, "model")
	if model == "" {
		model = strings.TrimSpace(s.cfg.Cron.Model)
	}
	ep, err := s.cfg.Endpoint(model)
	if err != nil {
		return fail(err.Error())
	}
	if e := jobText(job, "reasoning_effort"); e != "" {
		ep.ReasoningEffort = e
	}
	s.fetchMeta(s.base, ep, false)

	targets, _ := cron.ResolveTargets(job, s.signalHome())
	scope := "common"
	if len(targets) > 0 {
		scope = "gateway"
	}
	r := s.runner(&live{}, ep, s.cfg.PermissionMode(), "cron", nil)
	r.Tools, r.Approver, r.Ask, r.AsyncDelivery, r.Cron = s.regCron, nil, nil, false, nil
	r.MemoryScope = scope
	if scope == "gateway" {
		r.Scope = "gateway"
	}
	r.Prompt = func(_ context.Context, ss store.Session) (string, error) {
		res, err := prompt.Build(prompt.Options{
			Home: s.opt.Home, Cwd: ss.Cwd, Scope: scope, Memory: true,
			Now: time.Now().In(prompt.LocalZone()), RulesCharLimit: s.cfg.Memory.RulesCharLimit,
		})
		for _, w := range res.Warnings {
			log.Printf("cron: prompt: %s", w)
		}
		return res.Text, err
	}
	if s.idx != nil {
		r.Recall = func(ctx context.Context, text string) string { return s.idx.Recall(ctx, scope, text) }
		r.SearchMessages = s.searchMessages(scope)
	}
	cwd := s.opt.Home
	if wd := jobText(job, "workdir"); wd != "" {
		if fi, err := os.Stat(wd); err == nil && fi.IsDir() {
			cwd = wd
		}
	}
	sess, err := r.NewSession(s.base, "cron", cwd)
	if err != nil {
		return fail(err.Error())
	}
	defer sess.Close()
	if err := s.st.UpdateSessionSettings(s.base, sess.ID(), ep.Model, ep.ReasoningEffort, string(s.cfg.PermissionMode())); err != nil {
		return fail(err.Error())
	}
	_, _ = s.st.SetTitle(s.base, sess.ID(), cronTitle(name, time.Now().In(zone)), "user")

	ctx, cancel := context.WithCancel(s.base)
	defer cancel()
	var idled atomic.Bool
	timer := time.AfterFunc(cronIdle, func() { idled.Store(true); cancel() })
	defer timer.Stop()
	res, err := sess.Send(ctx, userPrompt, func(agent.Event) { timer.Reset(cronIdle) })
	switch {
	case idled.Load():
		return fail(fmt.Sprintf("The job '%s' made no progress for %ds and was stopped", name, int(cronIdle.Seconds())))
	case err != nil:
		return fail(err.Error())
	case strings.TrimSpace(res.Content) == "":
		return fail("The agent finished but its reply was empty")
	}
	return outcome{ok: true, silent: cron.Silent(res.Content), response: res.Content,
		doc: header(job, "", time.Now().In(zone)) +
			fmt.Sprintf("**Schedule:** %v\n\n## Prompt\n\n%s\n\n## Response\n\n%s\n", orNA(job["schedule_display"]), userPrompt, res.Content)}
}

func orNA(v any) any {
	if s, _ := v.(string); s == "" {
		return "N/A"
	}
	return v
}

// cronTitle is a cron session's title: the job's name and the run's time.
func cronTitle(name string, at time.Time) string {
	if r := []rune(name); len(r) > 60 {
		name = string(r[:60])
	}
	return name + " · " + at.Format("Jan 02 15:04")
}

// deliver sends a run's result to the job's Signal targets and reports what
// went wrong, "" for nothing.
func (c *cronSched) deliver(job cron.Job, o outcome) string {
	s := c.srv
	var content string
	switch {
	case !o.ok:
		content = cron.FailureText(fmt.Sprint(job["name"]), o.errMsg)
	case o.silent:
		return ""
	default:
		content = o.response
	}
	targets, problem := cron.ResolveTargets(job, s.signalHome())
	var errs []string
	if problem != "" {
		errs = append(errs, problem)
	}
	if len(targets) > 0 {
		if s.chats == nil {
			errs = append(errs, "Signal is not served by this daemon")
		} else {
			text, styles := signal.Format(cron.Frame(fmt.Sprint(job["name"]), fmt.Sprint(job["id"]), content))
			for _, to := range targets {
				if err := s.chats.sendTo(to, text, styles); err != nil {
					errs = append(errs, fmt.Sprintf("delivery to signal:%s failed: %v", to, err))
				}
			}
		}
	}
	return strings.Join(errs, "; ")
}

// sendTo sends one message outside any chat session, and notes it so its
// echo is not taken for a message.
func (h *chatHub) sendTo(to, text string, styles []string) error {
	ts, err := h.cl.Send(h.srv.base, to, text, styles)
	if err != nil {
		return err
	}
	h.in.Sent(ts)
	return nil
}
