// Package approval classifies shell commands and file writes against the
// fork's blocklists (ported from tools/approval.py) and enforces the
// permission modes through a gate.
//
// The gate is the only place a dangerous operation is permitted. It asks an
// approver when the mode requires it; with no approver available it denies,
// and every blocking message starts with "BLOCKED".
package approval

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
)

// Mode is a permission mode.
type Mode string

const (
	Default  Mode = "default"
	Ask      Mode = "ask"
	Lockdown Mode = "lockdown"
	Yolo     Mode = "yolo"
)

// ParseMode parses a mode name; anything else is an error.
func ParseMode(s string) (Mode, error) {
	switch Mode(s) {
	case Default, Ask, Lockdown, Yolo:
		return Mode(s), nil
	default:
		return "", fmt.Errorf("unknown approval mode %q", s)
	}
}

// Choice is an approver's answer.
type Choice string

const (
	Once    Choice = "once"
	Session Choice = "session"
	Always  Choice = "always"
	Deny    Choice = "deny"
)

// Request is what the gate hands to the approver.
type Request struct {
	Kind          string // "command" or "write"
	Subject       string // the command, or the write's path
	Reasons       []string
	PatternKeys   []string
	AllowRemember bool // false in lockdown and for protected writes
}

// DeniedError is what an approver returns with Deny to pass the user's reason
// on to the model.
type DeniedError struct{ Reason string }

func (e *DeniedError) Error() string { return "denied: " + e.Reason }

// Approver is asked when the mode requires it.
type Approver func(ctx context.Context, req Request) (Choice, error)

// Allowlist is the persistent "always" memory (store.Allowlist).
type Allowlist interface {
	Has(key string) bool
	Add(key string) error
}

// Finding is one blocklist hit.
type Finding struct {
	Key         string
	Description string
	Hardline    bool
}

// Verdict is a gate decision.
type Verdict struct {
	Allowed bool
	Asked   bool
	Message string
}

// Gate enforces one mode for one session.
type Gate struct {
	mode     Mode // guarded by mu
	approver Approver
	allow    Allowlist

	mu         sync.Mutex
	session    map[string]bool // keys approved for this gate's life
	lastBlock  bool
	lastReason string
	allowedLog []string
}

// NewGate builds a gate. An empty mode is Default.
func NewGate(mode Mode, approver Approver, allow Allowlist) *Gate {
	if mode == "" {
		mode = Default
	}
	return &Gate{mode: mode, approver: approver, allow: allow, session: map[string]bool{}}
}

// Decide is the pure policy answer: no approver is called and nothing is
// remembered. A dangerous command passes only when the mode is yolo or every
// finding key is in the allowlist.
func (g *Gate) Decide(command string) (bool, string) {
	findings := Classify(command)
	if hardline := firstHardline(findings); hardline != nil {
		return false, "hardline: " + hardline.Description
	}
	dangerous := len(findings) > 0
	switch g.curMode() {
	case Yolo:
		return true, ""
	case Lockdown:
		return false, "lockdown: approval is required for everything"
	default:
		if !dangerous {
			return true, ""
		}
		if g.allApproved(keysOf(findings)) {
			return true, ""
		}
		return false, "dangerous: " + strings.Join(descriptionsOf(findings), ", ")
	}
}

// Gate runs the full check for a command with a caller-chosen subject and
// detail, and records the verdict.
func (g *Gate) Gate(command string, ctx context.Context, subject, detail string) (bool, string) {
	v := g.commandCheck(ctx, command, subject, detail)
	g.record(v)
	return v.Allowed, v.Message
}

// Command runs the full check for a command.
func (g *Gate) Command(ctx context.Context, command string) Verdict {
	v := g.commandCheck(ctx, command, command, "")
	g.record(v)
	return v
}

// Write runs the full check for a file write. Protected writes ask in every
// mode, including yolo; ordinary writes ask only in lockdown.
func (g *Gate) Write(ctx context.Context, absPath string, protected bool) Verdict {
	var v Verdict
	switch {
	case protected:
		v = g.ask(ctx, Request{Kind: "write", Subject: absPath, AllowRemember: false})
	case g.curMode() == Lockdown:
		v = g.ask(ctx, Request{Kind: "write", Subject: absPath, AllowRemember: false})
	default:
		v = Verdict{Allowed: true}
	}
	g.record(v)
	return v
}

// Blocked reports whether the last verdict was a denial.
func (g *Gate) Blocked() bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.lastBlock
}

// Reason is the last denial's message.
func (g *Gate) Reason() string {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.lastReason
}

// Allow records that a dangerous command was let through (yolo).
func (g *Gate) Allow(subject, detail string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.allowedLog = append(g.allowedLog, subject)
}

func (g *Gate) commandCheck(ctx context.Context, command, subject, detail string) Verdict {
	findings := Classify(command)
	if hardline := firstHardline(findings); hardline != nil {
		return Verdict{
			Allowed: false,
			Message: "BLOCKED (hardline): " + hardline.Description +
				". This command is blocked in every mode, yolo included; run it yourself in a terminal outside the agent if you must.",
		}
	}
	dangerous := len(findings) > 0
	switch g.curMode() {
	case Yolo:
		if dangerous {
			g.Allow(subject, strings.Join(descriptionsOf(findings), ", "))
		}
		return Verdict{Allowed: true}
	case Lockdown:
		req := Request{Kind: "command", Subject: subject, AllowRemember: false}
		if dangerous {
			req.Reasons = descriptionsOf(findings)
			req.PatternKeys = keysOf(findings)
		}
		if detail != "" {
			req.Reasons = append(req.Reasons, detail)
		}
		return g.ask(ctx, req)
	default:
		if !dangerous {
			return Verdict{Allowed: true}
		}
		keys := keysOf(findings)
		if g.allApproved(keys) {
			return Verdict{Allowed: true}
		}
		req := Request{
			Kind:          "command",
			Subject:       subject,
			Reasons:       descriptionsOf(findings),
			PatternKeys:   keys,
			AllowRemember: true,
		}
		if detail != "" {
			req.Reasons = append(req.Reasons, detail)
		}
		return g.ask(ctx, req)
	}
}

// allApproved reports whether every key is approved for this gate's life or
// in the allowlist.
func (g *Gate) allApproved(keys []string) bool {
	if len(keys) == 0 {
		return true
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	for _, k := range keys {
		if g.session[k] {
			continue
		}
		if g.allow == nil || !g.allow.Has(k) {
			return false
		}
	}
	return true
}

func (g *Gate) ask(ctx context.Context, req Request) Verdict {
	if g.approver == nil {
		return Verdict{Allowed: false, Message: g.nobodyMessage(req)}
	}
	choice, err := g.approver(ctx, req)
	var denied *DeniedError
	if errors.As(err, &denied) {
		return Verdict{Allowed: false, Asked: true, Message: "BLOCKED: the user denied this: " + denied.Reason + ". Do not retry it."}
	}
	if err != nil {
		return Verdict{Allowed: false, Asked: true, Message: fmt.Sprintf("BLOCKED: approval failed: %v", err)}
	}
	switch choice {
	case Deny:
		return Verdict{Allowed: false, Asked: true, Message: "BLOCKED: the user denied this. Do not retry it."}
	case Once:
		return Verdict{Allowed: true, Asked: true}
	case Session, Always:
		if req.AllowRemember {
			g.remember(req.PatternKeys)
			if choice == Always && g.allow != nil {
				for _, k := range req.PatternKeys {
					_ = g.allow.Add(k)
				}
			}
		}
		return Verdict{Allowed: true, Asked: true}
	default:
		// An unknown answer fails closed.
		return Verdict{Allowed: false, Asked: true, Message: "BLOCKED: the approver gave no answer. Do not retry it."}
	}
}

func (g *Gate) remember(keys []string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	for _, k := range keys {
		g.session[k] = true
	}
}

func (g *Gate) nobodyMessage(req Request) string {
	switch {
	case g.curMode() == Lockdown:
		return "BLOCKED: lockdown mode requires approval for everything, and nobody is available to approve it. Lockdown stays on; start the run with an approver attached."
	case req.Kind == "write":
		return "BLOCKED: this write needs approval, and nobody is available to approve it. Start the run with an approver attached."
	default:
		reasons := strings.Join(req.Reasons, ", ")
		if reasons == "" {
			reasons = "a dangerous command"
		}
		return fmt.Sprintf("BLOCKED: %s — nobody is available to approve this. Start the run with --yolo to allow dangerous commands without asking.", reasons)
	}
}

func (g *Gate) record(v Verdict) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if v.Allowed {
		g.lastBlock = false
		g.lastReason = ""
	} else {
		g.lastBlock = true
		g.lastReason = v.Message
	}
}

func firstHardline(findings []Finding) *Finding {
	for i := range findings {
		if findings[i].Hardline {
			return &findings[i]
		}
	}
	return nil
}

func keysOf(findings []Finding) []string {
	keys := make([]string, 0, len(findings))
	for _, f := range findings {
		keys = append(keys, f.Key)
	}
	return keys
}

func descriptionsOf(findings []Finding) []string {
	descs := make([]string, 0, len(findings))
	for _, f := range findings {
		descs = append(descs, f.Description)
	}
	return descs
}

// SetMode changes the mode; it applies from the next check, even while a
// turn is running.
func (g *Gate) SetMode(m Mode) {
	if m == "" {
		m = Default
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	g.mode = m
}

func (g *Gate) curMode() Mode {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.mode
}

// memoryDenied is the model's answer to a refused memory write (WA:360).
const memoryDenied = "Memory write denied by user. The change was not saved."

// Memory asks the approver about one memory change whatever the permission
// mode, yolo included (WA:324-375). Only "once" and "session" allow it; with
// no approver nothing can be saved.
func (g *Gate) Memory(ctx context.Context, summary, detail string) Verdict {
	if g.approver == nil {
		return Verdict{Message: "Nobody can approve a memory change in this run; it was not saved."}
	}
	choice, err := g.approver(ctx, Request{
		Kind: "memory", Subject: detail, Reasons: []string{"Save to memory: " + summary}, AllowRemember: false,
	})
	var denied *DeniedError
	if errors.As(err, &denied) {
		return Verdict{Asked: true, Message: memoryDenied + " The user said: " + denied.Reason}
	}
	if err != nil || ctx.Err() != nil || (choice != Once && choice != Session) {
		return Verdict{Asked: true, Message: memoryDenied}
	}
	return Verdict{Allowed: true, Asked: true}
}

// Script gates running a model-written script (execute_code). The script is
// the subject. It runs without asking in default and yolo, as a shell
// command that is not dangerous would; ask and lockdown ask for every one.
// The calls the script makes go through their own gates.
func (g *Gate) Script(ctx context.Context, script string) Verdict {
	var v Verdict
	switch g.curMode() {
	case Ask, Lockdown:
		v = g.ask(ctx, Request{Kind: "command", Subject: script, Reasons: []string{"run a Python script"}, AllowRemember: false})
	default:
		v = Verdict{Allowed: true}
	}
	g.record(v)
	return v
}
