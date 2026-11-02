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

// DefaultIdentity is the identity when the home has no SOUL.md.
const DefaultIdentity = "You are Son of Anton Agent, an intelligent AI assistant. " +
	"You are helpful, knowledgeable, and direct. You assist users with a wide " +
	"range of tasks including answering questions, writing and editing code, " +
	"analyzing information, creative work, and executing actions via your tools. " +
	"You communicate clearly, admit uncertainty when appropriate, and prioritize " +
	"being genuinely useful over being verbose unless otherwise directed below. " +
	"Be targeted and efficient in your exploration and investigations."

//go:embed guidance.md
var guidanceFile string

// Guidance is the tool and behavior guidance layer.
var Guidance = strings.TrimSpace(guidanceFile)

const (
	defaultRulesLimit = 6000
	contextMax        = 40_000
)

// Options select what Build includes. The same Options always give the same
// bytes.
type Options struct {
	Home, Cwd, Scope string // instance home; session cwd; "common" | "gateway"
	Memory           bool   // identity + all rules (true), or lean (false)
	Now              time.Time
	RulesCharLimit   int // 0 means 6000
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
	if o.Memory {
		id := DefaultIdentity
		if data, err := os.ReadFile(filepath.Join(o.Home, "SOUL.md")); err == nil {
			if s := strings.TrimSpace(string(data)); s != "" {
				id = s
			}
		} else if !os.IsNotExist(err) {
			return res, err
		}
		layers = append(layers, id)
	}
	layers = append(layers, Guidance, "Host: Linux\nCurrent working directory: "+o.Cwd)
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
	layers = append(layers, fmt.Sprintf("Conversation started: %s (%s)", o.Now.Format("Monday, January 2, 2006"), o.Now.Location()))
	res.Text = strings.Join(layers, "\n\n")
	return res, nil
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
	var lines []string
	used := 0
	for _, r := range f.Rule {
		scope := r.Scope
		if scope == "" {
			scope = "common"
		}
		if scope != "common" && !(scope == "gateway" && o.Scope == "gateway") {
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
	scope := o.Scope
	if scope == "" {
		scope = "common"
	}
	head := fmt.Sprintf("STANDING RULES (%s scope, %d of %d characters used). Follow these; they come from the owner.", scope, used, limit)
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
	return "# Project Context\n\nThe following project context files have been loaded and should be followed:\n\n" + body, paths, nil
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
