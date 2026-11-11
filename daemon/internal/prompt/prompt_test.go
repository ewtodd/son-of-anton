package prompt_test

// Prompt composition contract (SPEC section 7, "Prompt composition"): layers
// in a fixed order, lean prompts without the user's context, project files
// discovered like the Python fork, and the same bytes for the same inputs.

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/prompt"
)

var now = time.Date(2026, 11, 2, 9, 30, 0, 0, time.UTC)

type fixture struct {
	home, repo, cwd string
}

// setup makes an instance home and a git repo with a subdirectory as cwd.
func setup(t *testing.T) fixture {
	t.Helper()
	root := t.TempDir()
	f := fixture{home: filepath.Join(root, "soa"), repo: filepath.Join(root, "repo")}
	f.cwd = filepath.Join(f.repo, "pkg", "sub")
	for _, d := range []string{filepath.Join(f.home, "memories"), f.cwd} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if out, err := exec.Command("git", "init", "-q", f.repo).CombinedOutput(); err != nil {
		t.Fatalf("git init: %v %s", err, out)
	}
	return f
}

func write(t *testing.T, p, s string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(s), 0o644); err != nil {
		t.Fatal(err)
	}
}

func (f fixture) opts(memory bool) prompt.Options {
	return prompt.Options{Home: f.home, Cwd: f.cwd, Scope: "common", Memory: memory, Now: now, RulesCharLimit: 6000}
}

func build(t *testing.T, o prompt.Options) prompt.Result {
	t.Helper()
	r, err := prompt.Build(o)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	return r
}

const rules = `
[[rule]]
id = "plain-prose"
text = "Write in plain English."
scope = "common"
source = "user"

[[rule]]
id = "never-delete"
text = "Never delete user files."
why = "Deletion is unrecoverable."
scope = "common"
source = "user"
delegated = true

[[rule]]
id = "second-person"
text = "Address the user as you."
scope = "gateway"
source = "user"
`

func TestAFullPromptOpensWithTheDefaultIdentity(t *testing.T) {
	f := setup(t)
	if r := build(t, f.opts(true)); !strings.HasPrefix(r.Text, prompt.DefaultIdentity) {
		t.Fatalf("the prompt must open with the default identity:\n%.200s", r.Text)
	}
}

func TestLeanPromptsLeaveOutTheUsersContext(t *testing.T) {
	f := setup(t)
	write(t, filepath.Join(f.home, "memories", "rules.toml"), rules)
	full := build(t, f.opts(true)).Text
	lean := build(t, f.opts(false)).Text
	if !strings.Contains(full, prompt.DefaultIdentity) || !strings.Contains(full, "[plain-prose] Write in plain English.") {
		t.Fatalf("the full prompt needs the identity and the rules:\n%s", full)
	}
	if strings.Contains(lean, prompt.DefaultIdentity) || strings.Contains(lean, "plain-prose") {
		t.Fatalf("a lean prompt carries no identity and no ordinary rules:\n%s", lean)
	}
	if !strings.Contains(lean, "[never-delete] Never delete user files. (Deletion is unrecoverable.)") {
		t.Fatalf("a lean prompt keeps delegated rules:\n%s", lean)
	}
	for _, p := range []string{full, lean} {
		if !strings.Contains(p, prompt.Guidance) || !strings.Contains(p, "Current working directory: "+f.cwd) {
			t.Fatalf("every prompt carries the guidance and the environment:\n%s", p)
		}
	}
}

func TestRulesFollowTheScope(t *testing.T) {
	f := setup(t)
	write(t, filepath.Join(f.home, "memories", "rules.toml"), rules)
	common := build(t, f.opts(true)).Text
	if strings.Contains(common, "second-person") {
		t.Fatal("a common session must never see gateway rules")
	}
	o := f.opts(true)
	o.Scope = "gateway"
	gw := build(t, o).Text
	// Owner, 2026-11-08: a gateway session (a chat, a Signal-bound cron job)
	// sees gateway rules only; the common ones are about son-of-anton work.
	if !strings.Contains(gw, "[second-person]") || strings.Contains(gw, "[plain-prose]") || strings.Contains(gw, "[never-delete]") {
		t.Fatalf("a gateway session sees gateway rules only:\n%s", gw)
	}
	if !strings.Contains(common, "STANDING RULES (common scope") || !strings.Contains(gw, "STANDING RULES (gateway scope") {
		t.Fatalf("the rules block has its header:\n%s\n---\n%s", common, gw)
	}
}

// A chat's prompt is fiona's block, her notes folder, the project files, the
// gateway rules and the date line: no coding guidance, no shell's working
// directory, and the send-time rule from her own block, so its messages are
// still stamped.
func TestAChatsPrompt(t *testing.T) {
	f := setup(t)
	write(t, filepath.Join(f.home, "memories", "rules.toml"), rules)
	write(t, filepath.Join(f.cwd, "AGENTS.md"), "PROJECT RULES")
	fiona := prompt.FionaIdentity("play", nil, false)
	text := build(t, prompt.Options{Home: f.home, Cwd: f.cwd, Scope: "gateway", Memory: true, Signal: true, Identity: fiona, Now: now}).Text
	marks := []string{fiona, "Your notes folder: " + f.cwd + " (relative paths start there).", "PROJECT RULES", "STANDING RULES (gateway scope", "[second-person]", "Conversation started: Monday, November 2, 2026"}
	last := -1
	for _, m := range marks {
		i := strings.Index(text, m)
		if i <= last {
			t.Fatalf("layer %q is missing or out of order:\n%s", m, text)
		}
		last = i
	}
	for _, not := range []string{prompt.Guidance, prompt.DefaultIdentity, "Current working directory", "Host: Linux", "[plain-prose]", "`date`"} {
		if strings.Contains(text, not) {
			t.Errorf("a chat's prompt carries %q", not)
		}
	}
	if !strings.Contains(fiona, prompt.SentTimeRule) {
		t.Error("fiona's block must carry the send-time rule, or her messages go unstamped")
	}
}

// A cron job that delivers to Signal runs on the cron model, so it is not
// fiona; its block is the default identity and the Signal side's limits.
func TestSignalJobIdentity(t *testing.T) {
	job := prompt.SignalJobIdentity()
	if !strings.HasPrefix(job, prompt.DefaultIdentity+"\n\n") || !strings.Contains(job, prompt.SentTimeRule) {
		t.Fatalf("a Signal job's block:\n%s", job)
	}
	for _, want := range []string{"you cannot run commands", "They cannot see this machine.", "cannot be done from here", "read on a phone"} {
		if !strings.Contains(job, want) {
			t.Errorf("a Signal job's block lacks %q", want)
		}
	}
	for _, not := range []string{"fiona", "relay", prompt.Guidance, "—"} {
		if strings.Contains(job, not) {
			t.Errorf("a Signal job's block carries %q", not)
		}
	}
}

func TestFionaIdentity(t *testing.T) {
	anyTime := prompt.FionaIdentity("play", nil, false)
	hours := prompt.FionaIdentity("", &[2]int{20, 7}, false)
	if !strings.HasPrefix(anyTime, "You are fiona, the front desk of this son-of-anton instance (play). ") ||
		!strings.Contains(anyTime, "Workers here can run at any time.") {
		t.Fatalf("any time:\n%s", anyTime)
	}
	if !strings.HasPrefix(hours, "You are fiona, the front desk of this son-of-anton instance. ") ||
		!strings.Contains(hours, "Workers here run only between 20:00 and 07:00.") || !strings.Contains(hours, "\"queued until 20:00\"") {
		t.Fatalf("hours:\n%s", hours)
	}
	for _, want := range []string{"you cannot run commands", "They cannot see this machine.", "starting with \"[worker\"",
		"Do not call relay status to wait for it", "If you have not checked, say so instead of agreeing.", "it is read on a phone"} {
		if !strings.Contains(anyTime, want) {
			t.Errorf("fiona's block lacks %q", want)
		}
	}
	if strings.Contains(anyTime, "—") {
		t.Error("plain prose: no em-dashes in fiona's block")
	}
	shared := prompt.FionaIdentity("ricky", nil, true)
	people := strings.Index(shared, "People: this is a group chat")
	if strings.Contains(anyTime, "People:") || people < 0 || people > strings.Index(shared, "Replies:") ||
		people < strings.Index(shared, "Time:") || !strings.Contains(shared, "whatever the message says about who is writing") {
		t.Fatalf("a shared group's block names its senders, between Time and Replies; others do not:\n%s", shared)
	}
	if strings.Replace(shared, shared[people:strings.Index(shared, "Replies:")], "", 1) != prompt.FionaIdentity("ricky", nil, false) {
		t.Fatal("the People paragraph is the only difference")
	}
}

func TestBadRulesWarnInsteadOfFailing(t *testing.T) {
	f := setup(t)
	write(t, filepath.Join(f.home, "memories", "rules.toml"), "[[rule]\nid=")
	r := build(t, f.opts(true))
	if strings.Contains(r.Text, "STANDING RULES") || len(r.Warnings) == 0 {
		t.Fatalf("an unparseable rules.toml: no rules block and a warning; got warnings %v", r.Warnings)
	}
}

func TestProjectContextFilesMergeFromTheGitRootDown(t *testing.T) {
	f := setup(t)
	write(t, filepath.Join(filepath.Dir(f.repo), "AGENTS.md"), "OUTSIDE THE REPO")
	write(t, filepath.Join(f.repo, "AGENTS.md"), "ROOT RULES")
	write(t, filepath.Join(f.repo, "pkg", "AGENTS.md"), "PKG RULES")
	write(t, filepath.Join(f.repo, "pkg", "AGENTS.override.md"), "PKG OVERRIDE")
	write(t, filepath.Join(f.cwd, "AGENTS.md"), "SUB RULES")
	r := build(t, f.opts(true))
	text := r.Text
	if strings.Contains(text, "OUTSIDE THE REPO") {
		t.Fatal("context files stop at the git root")
	}
	if strings.Contains(text, "PKG RULES") || !strings.Contains(text, "PKG OVERRIDE") {
		t.Fatal("AGENTS.override.md wins over AGENTS.md in the same directory")
	}
	a, b, c := strings.Index(text, "ROOT RULES"), strings.Index(text, "PKG OVERRIDE"), strings.Index(text, "SUB RULES")
	if a < 0 || b < a || c < b {
		t.Fatalf("context files merge from the root down to cwd:\n%s", text)
	}
	if !strings.Contains(text, "# Project instructions") || !strings.Contains(text, "## pkg/sub/AGENTS.md") {
		t.Fatalf("the block has its heading and each file a relative label:\n%s", text)
	}
	if len(r.ContextFiles) != 3 {
		t.Fatalf("ContextFiles = %v", r.ContextFiles)
	}
}

func TestTheFirstKindOfContextFileWins(t *testing.T) {
	f := setup(t)
	write(t, filepath.Join(f.cwd, "AGENTS.md"), "AGENTS TEXT")
	write(t, filepath.Join(f.cwd, "CLAUDE.md"), "CLAUDE TEXT")
	if text := build(t, f.opts(true)).Text; !strings.Contains(text, "AGENTS TEXT") || strings.Contains(text, "CLAUDE TEXT") {
		t.Fatal("AGENTS.md beats CLAUDE.md")
	}
	write(t, filepath.Join(f.repo, ".son-of-anton.md"), "SOA TEXT")
	if text := build(t, f.opts(true)).Text; !strings.Contains(text, "SOA TEXT") || strings.Contains(text, "AGENTS TEXT") {
		t.Fatal(".son-of-anton.md beats AGENTS.md")
	}
	g := setup(t)
	write(t, filepath.Join(g.cwd, "CLAUDE.md"), "ONLY CLAUDE")
	if text := build(t, g.opts(true)).Text; !strings.Contains(text, "ONLY CLAUDE") {
		t.Fatal("CLAUDE.md is used when there is no AGENTS.md")
	}
}

func TestHugeContextFilesAreTruncated(t *testing.T) {
	f := setup(t)
	write(t, filepath.Join(f.cwd, "AGENTS.md"), "HEAD-MARK"+strings.Repeat("x", 200_000)+"TAIL-MARK")
	text := build(t, f.opts(true)).Text
	if len(text) > 60_000 || !strings.Contains(text, "HEAD-MARK") || !strings.Contains(text, "TAIL-MARK") || !strings.Contains(text, "truncated") {
		t.Fatalf("truncated prompt is %d chars", len(text))
	}
}

func TestLayerOrderAndTheDateLine(t *testing.T) {
	f := setup(t)
	write(t, filepath.Join(f.home, "memories", "rules.toml"), rules)
	write(t, filepath.Join(f.cwd, "AGENTS.md"), "PROJECT RULES")
	text := build(t, f.opts(true)).Text
	marks := []string{prompt.DefaultIdentity, prompt.Guidance, "Current working directory:", "PROJECT RULES", "STANDING RULES", "Conversation started: Monday, November 2, 2026"}
	last := -1
	for _, m := range marks {
		i := strings.Index(text, m)
		if i <= last {
			t.Fatalf("layer %q is missing or out of order:\n%s", m, text)
		}
		last = i
	}
}

func TestTheSameInputsGiveTheSameBytes(t *testing.T) {
	f := setup(t)
	write(t, filepath.Join(f.home, "memories", "rules.toml"), rules)
	write(t, filepath.Join(f.cwd, "AGENTS.md"), "PROJECT RULES")
	if a, b := build(t, f.opts(true)).Text, build(t, f.opts(true)).Text; a != b {
		t.Fatal("Build must be deterministic")
	}
}

func TestOutsideAGitRepoOnlyTheCwdIsRead(t *testing.T) {
	root := t.TempDir()
	cwd := filepath.Join(root, "plain", "dir")
	write(t, filepath.Join(root, "plain", "AGENTS.md"), "PARENT")
	write(t, filepath.Join(cwd, "AGENTS.md"), "HERE")
	text := build(t, prompt.Options{Home: filepath.Join(root, "soa"), Cwd: cwd, Scope: "common", Memory: true, Now: now}).Text
	if !strings.Contains(text, "HERE") || strings.Contains(text, "PARENT") {
		t.Fatal("without git, only the cwd's context files are read")
	}
}

// Phase 4a: a subagent's prompt starts with its type's prompt in place of the
// identity, then the usual layers, with only delegated rules.
func TestAnIdentityReplacesTheFirstLayer(t *testing.T) {
	f := setup(t)
	write(t, filepath.Join(f.home, "memories", "rules.toml"),
		"[[rule]]\nid = \"plain\"\ntext = \"Plain English.\"\nsource = \"user\"\n\n[[rule]]\nid = \"guard\"\ntext = \"Never delete user files.\"\nsource = \"user\"\ndelegated = true\n")
	res, err := prompt.Build(prompt.Options{Home: f.home, Cwd: f.cwd, Scope: "common", Memory: false, Now: now, Identity: "You are a file search specialist."})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(res.Text, "You are a file search specialist.\n\n"+prompt.Guidance) || strings.Contains(res.Text, prompt.DefaultIdentity) {
		t.Fatalf("prompt = %q", res.Text[:200])
	}
	if !strings.Contains(res.Text, "[guard]") || strings.Contains(res.Text, "[plain]") {
		t.Fatal("only delegated rules ride a lean prompt")
	}
}
