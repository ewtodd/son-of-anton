// Package check is the end-of-turn checker: before a reply that ends a turn is
// kept, a decision model (laya's System One API, POST <base_url>/systemone)
// answers yes/no questions about it, and a flagged reply is sent back to the
// model with a note naming each problem, at most max_retries times (a retry is
// asked only about claims).
//
// The questions and the state's text are the ones the checker's heads were
// fitted on (son-of-anton's fiona replay, 2026-11-10); changing either wording
// needs a refit, so they live here and nowhere else.
package check

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/config"
)

// Question is one yes/no question: a yes is a problem, and Nudge is what the
// model is told when the answer is yes.
type Question struct {
	ID, Instructions, Nudge string
}

// Questions are the first check of a draft, in the order a note names them.
var Questions = []Question{
	{"claim", "Does the assistant's draft reply say it has done something (written, logged, updated, saved, created, " +
		"removed, scheduled, checked, looked up, sent, noted or remembered) that none of this turn's tool calls did?",
		"Your reply says you did something that no tool call in this turn did. Do it now with the right tool call, " +
			"or correct the reply."},
	{"unchecked", "The user's latest message asks about the current state of something (a scheduled job, a file, a log, a " +
		"worker) or why something happened. Does the draft reply answer it without any tool call in this turn " +
		"that checked that state? Answer no if the message asks no such thing.",
		"Your reply answers a question about the current state of something without checking it in this turn. " +
			"Check it with your tools first (the job list, the notes, the worker's status) and answer from what " +
			"they show."},
	{"offtopic", "Does the draft reply fail to respond to the user's latest message (it answers something else, such as " +
		"an earlier topic)?",
		"Your reply does not answer the user's latest message. Read it again and reply to that message."},
}

// Recheck is the only question asked of a retried draft: a nudge to check or
// fix something tends to come back as a claim that it was done.
var Recheck = Question{Questions[0].ID, Questions[0].Instructions,
	"Your reply still says you did something that no tool call in this turn did. Make the tool call now, or say " +
		"plainly that you have not done it."}

// NotePrefix marks the note as the checker's, not the user's, and NoteSuffix
// ends it: on 2026-11-10 Bolt answered a note with no such ending as if it were
// the conversation ("Understood. I will check cronjobs ... before answering"),
// and that reply went to the chat.
const (
	NotePrefix = "[A check by son-of-anton, not a message from the user] "
	NoteSuffix = " Then write your reply to the user's latest message. This note needs no answer of its own."
)

// Config is the resolved [checker] table.
type Config struct {
	BaseURL, APIKey string
	Models          []string // served-model prefixes the check applies to
	Timeout         time.Duration
	Cutoffs         map[string]float64
	// Questions are the ids of Questions the first draft is asked (all of
	// them when empty).
	Questions     []string
	RecheckCutoff float64
	MaxRetries    int
	// LogOnly checks every draft but sends none back: a draft is asked every
	// question in Questions (the unchecked gate is for sending back), and
	// stands whatever the answers.
	LogOnly bool
	// RecordPath, when set, is a JSONL file each check is appended to (see
	// Record), the data to label and fit the heads on.
	RecordPath string
}

// ConfigFrom resolves [checker]; ok says it is configured (a base URL and at
// least one model prefix).
func ConfigFrom(c *config.Config) (Config, bool) {
	k := c.Checker
	base := strings.TrimRight(strings.TrimSpace(k.BaseURL), "/")
	if base == "" || len(k.Models) == 0 {
		return Config{}, false
	}
	cfg := Config{BaseURL: base, Models: k.Models, Timeout: 3 * time.Second,
		Cutoffs: map[string]float64{"claim": 0.5, "unchecked": 0.5, "offtopic": 0.5}, RecheckCutoff: 0.5, MaxRetries: 2}
	if env := strings.TrimSpace(k.APIKeyEnv); env != "" {
		cfg.APIKey = c.Getenv(env)
	}
	if k.TimeoutSeconds != nil && *k.TimeoutSeconds > 0 {
		cfg.Timeout = time.Duration(*k.TimeoutSeconds * float64(time.Second))
	}
	for q, v := range k.Cutoffs {
		cfg.Cutoffs[q] = v
	}
	if k.RecheckCutoff != nil {
		cfg.RecheckCutoff = *k.RecheckCutoff
	}
	if k.MaxRetries != nil {
		cfg.MaxRetries = *k.MaxRetries
	}
	cfg.Questions = k.Questions
	cfg.LogOnly = k.LogOnly
	return cfg, true
}

// Applies says whether the check runs for replies served by model.
func (c Config) Applies(model string) bool {
	for _, p := range c.Models {
		if p != "" && strings.HasPrefix(model, p) {
			return true
		}
	}
	return false
}

// Call is one tool call: its name and its arguments as sent.
type Call struct{ Name, Arguments string }

// Step is one step of the turn so far: the calls the model made, then their
// results in the same order.
type Step struct {
	Calls   []Call
	Results []string
}

// Input is what the checker reads: never the history before the turn.
type Input struct {
	Tools []string // the names of the tools the model can call
	User  string   // the turn's user message as the model saw it
	Steps []Step   // the turn's tool steps before the draft
	Draft string   // the reply that would end the turn
	// Chat and Model only label the record (Config.RecordPath); State
	// leaves them out.
	Chat, Model string
}

// State is the text the questions are asked of, in the exact shape the
// heads were fitted on (the replay's checker.py context(): each step's calls,
// then its results).
func State(in Input) string {
	var turn []string
	for _, s := range in.Steps {
		for _, c := range s.Calls {
			turn = append(turn, "call "+c.Name+" "+cut(c.Arguments, 600))
		}
		for _, r := range s.Results {
			turn = append(turn, "result "+cut(r, 1500))
		}
	}
	steps := "(none)"
	if len(turn) > 0 {
		steps = strings.Join(turn, "\n")
	}
	return "Tools the assistant can call: " + strings.Join(in.Tools, ", ") + "\n\n" +
		"The user's latest message:\n" + cut(in.User, 3000) + "\n\n" +
		"This turn's tool calls and results before the draft:\n" + steps + "\n\n" +
		"The assistant's draft reply (the turn ends with it; it makes no tool call):\n" + cut(in.Draft, 3000)
}

// cut keeps n characters (code points, as Python counts them) and marks the cut.
func cut(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + " [...]"
}

// Checker asks the questions over HTTP.
type Checker struct {
	cfg      Config
	http     *http.Client
	recordMu sync.Mutex
}

// New returns a checker for cfg.
func New(cfg Config) *Checker { return &Checker{cfg: cfg, http: &http.Client{}} }

// Config returns the resolved configuration.
func (k *Checker) Config() Config { return k.cfg }

// ask returns each question's probability of yes, within the timeout.
func (k *Checker) ask(ctx context.Context, state string, qs []Question) (map[string]float64, error) {
	ctx, cancel := context.WithTimeout(ctx, k.cfg.Timeout)
	defer cancel()
	questions := map[string]any{}
	for _, q := range qs {
		questions[q.ID] = map[string]any{"type": "noul", "instructions": q.Instructions}
	}
	body, _ := json.Marshal(map[string]any{"state": state, "questions": questions})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, k.cfg.BaseURL+"/systemone", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if k.cfg.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+k.cfg.APIKey)
	}
	resp, err := k.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("checker request failed: %w", err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, fmt.Errorf("checker request failed: %w", err)
	}
	if resp.StatusCode/100 != 2 {
		return nil, fmt.Errorf("checker request failed: HTTP %d", resp.StatusCode)
	}
	var out struct {
		Answers map[string]struct {
			Noul *float64 `json:"noul"`
		} `json:"answers"`
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("checker response unreadable: %w", err)
	}
	ps := map[string]float64{}
	for _, q := range qs {
		a, ok := out.Answers[q.ID]
		if !ok || a.Noul == nil {
			return nil, fmt.Errorf("checker gave no answer to %q", q.ID)
		}
		ps[q.ID] = *a.Noul
	}
	return ps, nil
}

// first are the questions a first draft is asked: those Config.Questions
// enables, less "unchecked" once the turn has made a tool call. The head cannot
// tell whether a call checked the state in question; on the fiona replay every
// draft it flagged came from a turn without one, and on 2026-11-10 it flagged
// the reply to a check-in the turn had just written down (0.93).
func (k *Checker) first(in Input) []Question {
	var qs []Question
	for _, q := range Questions {
		if len(k.cfg.Questions) > 0 && !slices.Contains(k.cfg.Questions, q.ID) {
			continue
		}
		if q.ID == "unchecked" && len(in.Steps) > 0 {
			continue
		}
		qs = append(qs, q)
	}
	return qs
}

// Review checks a draft. attempt is how many times the turn's draft has been
// sent back already: the first draft gets the questions first gives it, each
// at its cut-off, a retried one only Recheck at RecheckCutoff (when claim is
// asked at all), and none past MaxRetries. note is "" when the draft stands;
// otherwise it is the message that sends it back. flags are the questions at
// or over their cut-offs, sent back or not. With LogOnly the draft is asked
// every question in Questions, and note is always "". A checker that fails or
// times out lets the draft stand (err says why). Every answered check is
// recorded (Record).
func (k *Checker) Review(ctx context.Context, in Input, attempt int) (note string, flags []string, err error) {
	if attempt >= k.cfg.MaxRetries {
		return "", nil, nil
	}
	qs, cutoff := k.first(in), func(q string) float64 { return k.cfg.Cutoffs[q] }
	if k.cfg.LogOnly {
		qs = nil
		for _, q := range Questions {
			if len(k.cfg.Questions) == 0 || slices.Contains(k.cfg.Questions, q.ID) {
				qs = append(qs, q)
			}
		}
	}
	if attempt > 0 {
		qs, cutoff = nil, func(string) float64 { return k.cfg.RecheckCutoff }
		if len(k.cfg.Questions) == 0 || slices.Contains(k.cfg.Questions, Recheck.ID) {
			qs = []Question{Recheck}
		}
	}
	if len(qs) == 0 {
		return "", nil, nil
	}
	state := State(in)
	ps, err := k.ask(ctx, state, qs)
	if err != nil {
		return "", nil, err
	}
	var nudges []string
	cutoffs := map[string]float64{}
	for _, q := range qs {
		cutoffs[q.ID] = cutoff(q.ID)
		if ps[q.ID] >= cutoff(q.ID) {
			flags = append(flags, fmt.Sprintf("%s %.2f", q.ID, ps[q.ID]))
			nudges = append(nudges, q.Nudge)
		}
	}
	if len(nudges) > 0 && !k.cfg.LogOnly {
		note = NotePrefix + strings.Join(nudges, " ") + NoteSuffix
	}
	if err := k.record(Record{Time: time.Now().Format(time.RFC3339), Chat: in.Chat, Model: in.Model, Attempt: attempt,
		State: state, Answers: ps, Cutoffs: cutoffs, Flags: flags, SentBack: note != ""}); err != nil {
		log.Printf("check: recording: %v", err)
	}
	return note, flags, nil
}

// Record is one check as Config.RecordPath keeps it, a line of JSON: the
// text the questions were asked of and every answer, so that the drafts can
// be labelled and the heads fitted again.
type Record struct {
	Time     string             `json:"time"`
	Chat     string             `json:"chat"`
	Model    string             `json:"model"`
	Attempt  int                `json:"attempt"`
	State    string             `json:"state"`
	Answers  map[string]float64 `json:"answers"`
	Cutoffs  map[string]float64 `json:"cutoffs"`
	Flags    []string           `json:"flags"`
	SentBack bool               `json:"sent_back"`
}

// record appends r to RecordPath (nothing when it is ""), creating the file
// and its directory private to this account.
func (k *Checker) record(r Record) error {
	if k.cfg.RecordPath == "" {
		return nil
	}
	b, err := json.Marshal(r)
	if err != nil {
		return err
	}
	k.recordMu.Lock()
	defer k.recordMu.Unlock()
	if err := os.MkdirAll(filepath.Dir(k.cfg.RecordPath), 0o700); err != nil {
		return err
	}
	f, err := os.OpenFile(k.cfg.RecordPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	if _, err := f.Write(append(b, '\n')); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}
