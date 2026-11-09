// Package prompt composes a session's system prompt from layers: identity,
// guidance, environment, project context files, and standing rules.
package prompt

import (
	_ "embed"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/BurntSushi/toml"
)

// DefaultIdentity is the identity of every full prompt but a chat's and a
// subagent's (Options.Identity).
const DefaultIdentity = "You are Son of Anton, an assistant that works through tools on the user's machine. " +
	"Answer plainly and get to the point. Read before you change things, " +
	"check your work, and say so when you are unsure or when something failed. " +
	"Keep your investigations proportionate to the question."

//go:embed guidance.md
var guidanceFile string

// Guidance is the tool and behavior guidance layer.
var Guidance = strings.TrimSpace(guidanceFile)

// SentTimeRule marks the guidance's send-time rule: a prompt carries the rule
// that explains a stamped user message when it contains this. A session
// stamps its user messages only while its prompt carries the rule (a resumed
// session keeps its stored prompt until compaction rebuilds it, and a
// --system one-shot's prompt is whatever the file says).
const SentTimeRule = "[Day YYYY-MM-DD HH:MM ZONE]"

const (
	defaultRulesLimit = 6000
	contextMax        = 40_000
)

// Options select what Build includes. The same Options always give the same
// bytes.
type Options struct {
	Home, Cwd, Scope string // instance home; session cwd; "common" | "gateway"
	Memory           bool   // identity + all rules (true), or lean (false)
	Identity         string // when set, layer 1 whatever Memory says (a subagent's type prompt)
	// Signal is a prompt for the Signal side, a chat or a cron job that
	// delivers to one: Identity is its whole identity and guidance
	// (FionaIdentity, SignalJobIdentity), so the coding guidance is left out
	// and the environment line names the notes folder.
	Signal         bool
	Now            time.Time
	RulesCharLimit int // 0 means 6000
}

// Result is the prompt and what went into it.
type Result struct {
	Text         string
	Warnings     []string // e.g. an unparseable rules.toml
	ContextFiles []string // absolute paths of the project files loaded
}

// Build composes the system prompt.
func Build(o Options) (Result, error) {
	var res Result
	var layers []string
	if o.Identity != "" {
		layers = append(layers, o.Identity)
	} else if o.Memory {
		layers = append(layers, DefaultIdentity)
	}
	if o.Signal {
		layers = append(layers, "Your notes folder: "+o.Cwd+" (relative paths start there).")
	} else {
		layers = append(layers, Guidance, "Host: Linux\nCurrent working directory: "+o.Cwd)
	}
	if block, files, err := projectContext(o.Cwd); err != nil {
		return res, err
	} else if block != "" {
		layers = append(layers, block)
		res.ContextFiles = files
	}
	rules, warn := loadRules(o)
	res.Warnings = warn
	if rules != "" {
		layers = append(layers, rules)
	}
	layers = append(layers, fmt.Sprintf("Conversation started: %s (%s)", o.Now.Format("Monday, January 2, 2006"), zoneSuffix(o.Now)))
	res.Text = strings.Join(layers, "\n\n")
	return res, nil
}

// zoneSuffix names Now's zone: the zone name, the
// abbreviation when it differs, and the UTC offset, so the model never has
// to guess between standard and daylight time. An unnamed local zone
// ("Local") gives only the abbreviation and the offset.
func zoneSuffix(t time.Time) string {
	var bits []string
	name := t.Location().String()
	if name != "Local" {
		bits = append(bits, name)
	}
	if abbrev, _ := t.Zone(); abbrev != "" && abbrev != name {
		bits = append(bits, abbrev)
	}
	return strings.Join(append(bits, "UTC"+t.Format("-07:00")), ", ")
}

type rule struct {
	ID        string `toml:"id"`
	Text      string `toml:"text"`
	Why       string `toml:"why"`
	Scope     string `toml:"scope"`
	Delegated bool   `toml:"delegated"`
}

func loadRules(o Options) (string, []string) {
	path := filepath.Join(o.Home, "memories", "rules.toml")
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return "", nil
		}
		return "", []string{fmt.Sprintf("rules: reading %s: %v", path, err)}
	}
	var f struct {
		Rule []rule `toml:"rule"`
	}
	if err := toml.Unmarshal(data, &f); err != nil {
		return "", []string{fmt.Sprintf("rules: %s does not parse, no rules loaded: %v", path, err)}
	}
	// A session loads the rules of its own scope only: the terminal side
	// (common) never sees gateway rules, and a chat or Signal-bound cron job
	// (gateway) never sees common ones, which are about son-of-anton work
	// (commits, /etc/nixos, downloads) and only crowded fiona's prompt.
	sessionScope := o.Scope
	if sessionScope == "" {
		sessionScope = "common"
	}
	var lines []string
	used := 0
	for _, r := range f.Rule {
		scope := r.Scope
		if scope == "" {
			scope = "common"
		}
		if scope != sessionScope {
			continue
		}
		if !o.Memory && !r.Delegated {
			continue
		}
		line := fmt.Sprintf("- [%s] %s", r.ID, strings.TrimSpace(r.Text))
		if r.Why != "" {
			line += " (" + strings.TrimSpace(r.Why) + ")"
		}
		lines = append(lines, line)
		used += len([]rune(line))
	}
	if len(lines) == 0 {
		return "", nil
	}
	limit := o.RulesCharLimit
	if limit <= 0 {
		limit = defaultRulesLimit
	}
	head := fmt.Sprintf("STANDING RULES (%s scope, %d of %d characters used). Follow these; they come from the owner.", sessionScope, used, limit)
	return head + "\n" + strings.Join(lines, "\n"), nil
}

// ---- project context ----

type ctxFile struct{ path, label, content string }

func readTrimmed(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

// gitRoot is the nearest ancestor of dir (inclusive) containing .git, or "".
func gitRoot(dir string) string {
	for d := dir; ; d = filepath.Dir(d) {
		if _, err := os.Stat(filepath.Join(d, ".git")); err == nil {
			return d
		}
		if filepath.Dir(d) == d {
			return ""
		}
	}
}

func projectContext(cwd string) (string, []string, error) {
	if cwd == "" {
		return "", nil, nil
	}
	cwd = filepath.Clean(cwd)
	root := gitRoot(cwd)
	base := root
	if base == "" {
		base = cwd
	}
	rel := func(p string) string {
		if r, err := filepath.Rel(base, p); err == nil {
			return r
		}
		return p
	}
	var files []ctxFile
	add := func(p string) bool {
		if c := readTrimmed(p); c != "" {
			files = append(files, ctxFile{p, rel(p), c})
			return true
		}
		return false
	}

	// 1. .son-of-anton.md: the nearest, from cwd up to the git root.
	for d := cwd; len(files) == 0; d = filepath.Dir(d) {
		for _, n := range []string{".son-of-anton.md", "SON_OF_ANTON.md"} {
			if add(filepath.Join(d, n)) {
				break
			}
		}
		if root == "" || d == root || filepath.Dir(d) == d {
			break
		}
	}
	// 2. The AGENTS chain, root first.
	if len(files) == 0 {
		dirs := []string{cwd}
		if root != "" {
			dirs = nil
			for d := cwd; ; d = filepath.Dir(d) {
				dirs = append([]string{d}, dirs...)
				if d == root {
					break
				}
			}
		}
		seen := map[string]bool{}
		for _, d := range dirs {
			for _, n := range []string{"AGENTS.override.md", "AGENTS.md", "agents.md"} {
				p := filepath.Join(d, n)
				if c := readTrimmed(p); c != "" {
					if !seen[c] {
						seen[c] = true
						files = append(files, ctxFile{p, rel(p), c})
					}
					break
				}
			}
		}
	}
	// 3. CLAUDE.md, cwd only.
	if len(files) == 0 {
		for _, n := range []string{"CLAUDE.md", "claude.md"} {
			if add(filepath.Join(cwd, n)) {
				break
			}
		}
	}
	// 4. Cursor rules, cwd only.
	if len(files) == 0 {
		add(filepath.Join(cwd, ".cursorrules"))
		mdc, _ := filepath.Glob(filepath.Join(cwd, ".cursor", "rules", "*.mdc"))
		sort.Strings(mdc)
		for _, p := range mdc {
			add(p)
		}
	}
	if len(files) == 0 {
		return "", nil, nil
	}

	var sections, paths []string
	for _, f := range files {
		sections = append(sections, "## "+f.label+"\n\n"+f.content)
		paths = append(paths, f.path)
	}
	body := truncateContext(strings.Join(sections, "\n\n"), paths, cwd)
	return "# Project instructions\n\nThese instruction files apply to the working directory. Follow them.\n\n" + body, paths, nil
}

// truncateContext keeps the first 70% and the last 20% of contextMax around
// a marker naming where to read the rest.
func truncateContext(s string, paths []string, cwd string) string {
	r := []rune(s)
	if len(r) <= contextMax {
		return s
	}
	where := cwd
	if len(paths) == 1 {
		where = paths[0]
	}
	head, tail := contextMax*7/10, contextMax*2/10
	return string(r[:head]) + "\n\n[... truncated: read " + where + " for the rest ...]\n\n" + string(r[len(r)-tail:])
}
