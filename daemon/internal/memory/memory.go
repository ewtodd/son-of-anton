// Package memory is the rule and fact stores. The files are TOML under the
// home, written with a <file>.lock flock convention, and files written by
// earlier releases must keep loading, so the TOML shape does not change.
package memory

import (
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/BurntSushi/toml"
)

// Default limits (memory.rules_char_limit, memory.fact_char_limit).
const DefaultRulesCharLimit, DefaultFactCharLimit = 6000, 400

// factSimilarity is the token-set Jaccard at which a new fact is refused as
// a duplicate when no embedding answer is available.
const factSimilarity = 0.8

const slugMax = 32

// Rule is one [[rule]] entry.
type Rule struct {
	ID, Text, Why, Scope, Source string
	Delegated                    bool
}

// Fact is one [[fact]] entry.
type Fact struct{ ID, Text, Source string }

// Change is one requested edit. Scope "" or "current" means the caller's.
type Change struct{ Kind, Action, ID, Text, Why, Scope string }

// Result describes an applied change. Usage is set for rules only.
type Result struct{ ID, Kind, Scope, Usage string }

// Plan is what the user is asked to approve.
type Plan struct{ Summary, Detail string }

// Error is a refused change; Message is for the model, verbatim.
type Error struct {
	Message string
	Similar []Fact
	IDs     []string
}

func (e *Error) Error() string { return e.Message }

// Store reads and writes <home>/memories.
type Store struct {
	RulesCharLimit, FactCharLimit int
	// Similar finds near-duplicate facts of a scope; nil (or a nil answer)
	// means token Jaccard.
	Similar func(scope, text string) []Fact

	home string
}

// New returns a store over home with the default limits.
func New(home string) *Store {
	return &Store{RulesCharLimit: DefaultRulesCharLimit, FactCharLimit: DefaultFactCharLimit, home: home}
}

// writeMu serializes read-modify-write cycles inside this process; the
// flock covers other processes.
var writeMu sync.Mutex

// NormalizeScope maps a scope name onto "common" or "gateway".
func NormalizeScope(s string) (string, bool) {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "common", "shared", "cli":
		return "common", true
	case "gateway":
		return "gateway", true
	}
	return "", false
}

// EntryID is a short slug of text, made unique against taken with -2, -3, ...
func EntryID(text string, taken map[string]bool, fallback string) string {
	words := tokens(text)
	slug := ""
	for i, w := range words {
		if i >= 6 {
			break
		}
		cand := w
		if slug != "" {
			cand = slug + "-" + w
		}
		if len(cand) > slugMax {
			break
		}
		slug = cand
	}
	if slug == "" {
		switch {
		case len(words) > 0 && len(words[0]) > slugMax:
			slug = words[0][:slugMax]
		case len(words) > 0:
			slug = words[0]
		default:
			slug = fallback
		}
	}
	cand := slug
	for n := 2; taken[cand]; n++ {
		cand = slug + "-" + strconv.Itoa(n)
	}
	return cand
}

// tokens are the lowercased [a-z0-9]+ runs of text.
func tokens(text string) []string {
	text = strings.ToLower(text)
	var out []string
	start := -1
	for i := 0; i <= len(text); i++ {
		in := i < len(text) && (text[i] >= 'a' && text[i] <= 'z' || text[i] >= '0' && text[i] <= '9')
		switch {
		case in && start < 0:
			start = i
		case !in && start >= 0:
			out = append(out, text[start:i])
			start = -1
		}
	}
	return out
}

func jaccardSimilar(text string, facts []Fact) []Fact {
	mine := map[string]bool{}
	for _, t := range tokens(text) {
		mine[t] = true
	}
	if len(mine) == 0 {
		return nil
	}
	var out []Fact
	for _, f := range facts {
		theirs := map[string]bool{}
		for _, t := range tokens(f.Text) {
			theirs[t] = true
		}
		if len(theirs) == 0 {
			continue
		}
		inter := 0
		for t := range mine {
			if theirs[t] {
				inter++
			}
		}
		union := len(mine) + len(theirs) - inter
		if float64(inter)/float64(union) >= factSimilarity {
			out = append(out, f)
		}
	}
	return out
}

type entry = map[string]any

func (s *Store) dir() string { return filepath.Join(s.home, "memories") }

func factsFile(scope string) string {
	if scope == "common" {
		return "facts.toml"
	}
	return "facts." + scope + ".toml"
}

func (s *Store) path(kind, scope string) string {
	if kind == "rule" {
		return filepath.Join(s.dir(), "rules.toml")
	}
	return filepath.Join(s.dir(), factsFile(scope))
}

func str(e entry, k string) string {
	v, _ := e[k].(string)
	return v
}

// readEntries reads the [[key]] tables of path; an error means the file must
// not be rewritten.
func readEntries(path, key string) ([]entry, error) {
	b, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	name := filepath.Base(path)
	if err != nil {
		return nil, fmt.Errorf("%s could not be parsed (%v)", name, err)
	}
	var data map[string]any
	if _, err := toml.Decode(string(b), &data); err != nil {
		return nil, fmt.Errorf("%s could not be parsed (%v)", name, err)
	}
	var out []entry
	switch raw := data[key].(type) {
	case nil:
	case []map[string]any:
		out = raw
	case []any:
		for _, el := range raw {
			m, ok := el.(map[string]any)
			if !ok {
				return nil, fmt.Errorf("%s: '%s' must be an array of tables", name, key)
			}
			out = append(out, m)
		}
	default:
		return nil, fmt.Errorf("%s: '%s' must be an array of tables", name, key)
	}
	seen := map[string]bool{}
	for i, e := range out {
		id, _ := e["id"].(string)
		if strings.TrimSpace(id) == "" {
			return nil, fmt.Errorf("%s: [[%s]] entry %d has no id", name, key, i+1)
		}
		if t, _ := e["text"].(string); strings.TrimSpace(t) == "" {
			return nil, fmt.Errorf("%s: entry '%s' has no text", name, id)
		}
		if seen[id] {
			return nil, fmt.Errorf("%s: duplicate id '%s'", name, id)
		}
		seen[id] = true
	}
	return out, nil
}

// withLock runs f holding the process mutex and an exclusive flock on
// <path>.lock (the lock file every writer of the store uses).
func withLock(path string, f func() error) error {
	writeMu.Lock()
	defer writeMu.Unlock()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	lf, err := os.OpenFile(path+".lock", os.O_RDWR|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer lf.Close()
	if err := syscall.Flock(int(lf.Fd()), syscall.LOCK_EX); err != nil {
		return err
	}
	defer func() { _ = syscall.Flock(int(lf.Fd()), syscall.LOCK_UN) }()
	return f()
}

func entryScope(e entry, def string) (string, bool) {
	raw, ok := e["scope"]
	if !ok {
		return def, true
	}
	s, isStr := raw.(string)
	if !isStr {
		return "", false
	}
	return NormalizeScope(s)
}

func ruleLine(e entry) string {
	line := "- [" + str(e, "id") + "] " + str(e, "text")
	if why := strings.TrimSpace(str(e, "why")); why != "" {
		line += " (" + why + ")"
	}
	return line
}

func rulesSize(rules []entry) int {
	n := 0
	for _, r := range rules {
		n += utf8.RuneCountInString(ruleLine(r)) + 1
	}
	return n
}

// comma formats n with thousands separators.
func comma(n int) string {
	s := strconv.Itoa(n)
	for i := len(s) - 3; i > 0; i -= 3 {
		s = s[:i] + "," + s[i:]
	}
	return s
}

func (s *Store) usage(rules []entry) string {
	return comma(rulesSize(rules)) + "/" + comma(s.RulesCharLimit) + " chars"
}

func fail(format string, a ...any) *Error { return &Error{Message: fmt.Sprintf(format, a...)} }

func visibleScopes(caller string) []string {
	if caller == "common" {
		return []string{"common"}
	}
	return []string{"common", caller}
}

func contains(l []string, s string) bool {
	for _, x := range l {
		if x == s {
			return true
		}
	}
	return false
}

// Check validates c against the files as they are now and writes nothing.
func (s *Store) Check(c Change, caller string) (Plan, error) {
	_, p, err := s.run(c, caller, false)
	return p, err
}

// Apply validates and performs c.
func (s *Store) Apply(c Change, caller string) (Result, error) {
	r, _, err := s.run(c, caller, true)
	return r, err
}

func (s *Store) run(c Change, caller string, write bool) (Result, Plan, error) {
	kind := strings.ToLower(strings.TrimSpace(c.Kind))
	action := strings.ToLower(strings.TrimSpace(c.Action))
	if kind != "rule" && kind != "fact" {
		return Result{}, Plan{}, fail("Invalid kind '%s'. Use 'rule' or 'fact'.", kind)
	}
	if action != "add" && action != "replace" && action != "remove" {
		return Result{}, Plan{}, fail("Unknown action '%s'. Use add, replace or remove. There is no "+
			"read action: rules are already in your system prompt and relevant "+
			"facts arrive with the turn.", action)
	}
	caller, ok := NormalizeScope(caller)
	if !ok {
		caller = "common"
	}
	dest := caller
	switch name := strings.ToLower(strings.TrimSpace(c.Scope)); name {
	case "", "current", "session":
	default:
		d, ok := NormalizeScope(name)
		if !ok {
			return Result{}, Plan{}, fail("Unknown memory scope '%s'. Use one of: common, gateway.", c.Scope)
		}
		dest = d
	}
	id := strings.TrimSpace(c.ID)
	text, why := c.Text, c.Why
	if kind == "rule" {
		text, why = strings.Join(strings.Fields(text), " "), strings.Join(strings.Fields(why), " ")
	} else {
		text, why = strings.TrimSpace(text), ""
	}
	if action != "remove" && text == "" {
		return Result{}, Plan{}, fail("'text' is required for '%s'.", action)
	}
	if action != "add" && id == "" {
		return Result{}, Plan{}, fail("'id' is required for '%s'.", action)
	}

	path := s.path(kind, dest)
	var res Result
	var plan Plan
	err := withLock(path, func() error {
		entries, err := readEntries(path, kind)
		if err != nil {
			return fail("Refusing to write %s: %v. The file is left exactly as it is -- the user must fix it by hand, then retry.", filepath.Base(path), err)
		}
		var next []entry
		if action == "add" {
			res.ID, next, err = s.add(kind, entries, caller, dest, text, why)
		} else {
			res.ID, dest, next, err = s.change(kind, action, entries, caller, dest, id, text, why, path)
		}
		if err != nil {
			return err
		}
		plan = describe(kind, action, res.ID, dest, text, why)
		if write {
			if err := writeEntries(path, kind, next); err != nil {
				return fmt.Errorf("Failed to write memory file %s: %v", path, err)
			}
		}
		res.Kind, res.Scope = kind, dest
		if kind == "rule" {
			res.Usage = s.usage(next)
		}
		return nil
	})
	if err != nil {
		return Result{}, Plan{}, err
	}
	return res, plan, nil
}

func describe(kind, action, id, dest, text, why string) Plan {
	var p Plan
	switch action {
	case "add":
		p = Plan{Summary: fmt.Sprintf("add %s (%s)", kind, dest), Detail: text}
	case "replace":
		p = Plan{Summary: fmt.Sprintf("replace %s '%s' (%s)", kind, id, dest), Detail: text}
	default:
		p = Plan{Summary: fmt.Sprintf("remove %s '%s' (%s)", kind, id, dest), Detail: id}
	}
	if kind == "rule" && why != "" && action != "remove" {
		p.Detail += "\nwhy: " + why
	}
	return p
}

func (s *Store) tooLong(text string) *Error {
	if n := utf8.RuneCountInString(text); n > s.FactCharLimit {
		return fail("Fact is %d chars; the limit is %d. Split it into several facts, or put the "+
			"details in a note under notes/ and save a pointer fact.", n, s.FactCharLimit)
	}
	return nil
}

func (s *Store) add(kind string, entries []entry, caller, dest, text, why string) (string, []entry, error) {
	if kind == "fact" {
		if e := s.tooLong(text); e != nil {
			return "", nil, e
		}
		var facts []Fact
		for _, e := range entries {
			facts = append(facts, Fact{ID: str(e, "id"), Text: str(e, "text"), Source: str(e, "source")})
		}
		var similar []Fact
		if s.Similar != nil {
			similar = s.Similar(dest, text)
		}
		if similar == nil {
			similar = jaccardSimilar(text, facts)
		}
		if len(similar) > 0 {
			e := fail("A near-identical fact already exists; use action='replace' with its id instead of adding.")
			if dest == "common" || caller == "gateway" { // the wall
				e.Similar = similar
			}
			return "", nil, e
		}
	}
	taken := map[string]bool{}
	for _, e := range entries {
		taken[str(e, "id")] = true
	}
	n := entry{"id": EntryID(text, taken, kind), "text": text}
	if kind == "rule" {
		if why != "" {
			n["why"] = why
		}
		n["scope"] = dest
	}
	n["source"] = "agent"
	now := time.Now()
	n["added"] = time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, localDate)
	next := append(append([]entry{}, entries...), n)
	if kind == "rule" {
		if e := s.capRefusal(entries, next); e != nil {
			return "", nil, e
		}
	}
	return str(n, "id"), next, nil
}

func (s *Store) change(kind, action string, entries []entry, caller, dest, id, text, why, path string) (string, string, []entry, error) {
	scopes := []string{dest}
	if dest == caller {
		scopes = visibleScopes(caller)
	}
	var inScope []string
	for _, e := range entries {
		if kind == "fact" {
			inScope = append(inScope, str(e, "id"))
		} else if sc, ok := entryScope(e, "common"); ok && contains(scopes, sc) {
			inScope = append(inScope, str(e, "id"))
		}
	}
	var target entry
	idx := -1
	for i, e := range entries {
		if str(e, "id") == id && contains(inScope, id) {
			target, idx = e, i
			break
		}
	}
	if target == nil {
		where := " in " + filepath.Base(path)
		if kind == "rule" {
			where = " in scope " + strings.Join(scopes, "/")
		}
		e := fail("No %s with id '%s'%s.", kind, id, where)
		// The wall: a common caller never learns what the gateway holds.
		if dest == "common" || caller == "gateway" {
			e.IDs = inScope
		}
		return "", "", nil, e
	}
	if kind == "rule" {
		if sc, ok := entryScope(target, "common"); ok && sc != "" {
			dest = sc
		}
	}
	next := make([]entry, 0, len(entries))
	if action == "remove" {
		next = append(next, entries[:idx]...)
		next = append(next, entries[idx+1:]...)
		return id, dest, next, nil
	}
	if kind == "fact" {
		if e := s.tooLong(text); e != nil {
			return "", "", nil, e
		}
	}
	updated := entry{}
	for k, v := range target {
		updated[k] = v
	}
	updated["text"] = text
	if kind == "rule" && why != "" {
		updated["why"] = why
	}
	next = append(next, entries...)
	next[idx] = updated
	if kind == "rule" {
		if e := s.capRefusal(entries, next); e != nil {
			return "", "", nil, e
		}
	}
	return id, dest, next, nil
}

func (s *Store) capRefusal(before, after []entry) *Error {
	nt, ot := rulesSize(after), rulesSize(before)
	if nt > s.RulesCharLimit && nt > ot {
		return fail("The rules store is full: this change makes it %s chars; the cap is %s. Nothing was "+
			"changed and nothing is removed automatically. Tell the user it is full and ask which "+
			"existing rule they want to retire (or to shorten); do not pick one yourself.",
			comma(nt), comma(s.RulesCharLimit))
	}
	return nil
}

// Rules reads every rule of rules.toml.
func (s *Store) Rules() ([]Rule, error) {
	entries, err := readEntries(s.path("rule", "common"), "rule")
	if err != nil {
		return nil, err
	}
	out := make([]Rule, 0, len(entries))
	for _, e := range entries {
		scope := "common"
		if sc, ok := e["scope"].(string); ok {
			scope = sc
			if n, ok := NormalizeScope(sc); ok {
				scope = n
			}
		}
		d, _ := e["delegated"].(bool)
		out = append(out, Rule{ID: str(e, "id"), Text: str(e, "text"), Why: str(e, "why"),
			Scope: scope, Source: str(e, "source"), Delegated: d})
	}
	return out, nil
}

// Facts reads the facts file of a scope.
func (s *Store) Facts(scope string) ([]Fact, error) {
	if n, ok := NormalizeScope(scope); ok {
		scope = n
	}
	entries, err := readEntries(s.path("fact", scope), "fact")
	if err != nil {
		return nil, err
	}
	out := make([]Fact, 0, len(entries))
	for _, e := range entries {
		out = append(out, Fact{ID: str(e, "id"), Text: str(e, "text"), Source: str(e, "source")})
	}
	return out, nil
}

// ---- writing ----

var keyOrder = []string{"id", "text", "why", "scope", "source", "added"}

func writeEntries(path, key string, entries []entry) error {
	var b strings.Builder
	if len(entries) == 0 {
		b.WriteString(key + " = []\n")
	}
	for i, e := range entries {
		if i > 0 {
			b.WriteString("\n")
		}
		b.WriteString("[[" + key + "]]\n")
		keys := make([]string, 0, len(e))
		for _, k := range keyOrder {
			if _, ok := e[k]; ok {
				keys = append(keys, k)
			}
		}
		var rest []string
		for k := range e {
			if !contains(keyOrder, k) {
				rest = append(rest, k)
			}
		}
		sort.Strings(rest)
		for _, k := range append(keys, rest...) {
			v, ok := tomlValue(e[k])
			if !ok {
				continue
			}
			b.WriteString(tomlKey(k) + " = " + v + "\n")
		}
	}
	return atomicWrite(path, b.String())
}

func atomicWrite(path, data string) error {
	mode := os.FileMode(0o600)
	if st, err := os.Stat(path); err == nil {
		mode = st.Mode().Perm()
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".mem_")
	if err != nil {
		return err
	}
	name := tmp.Name()
	cleanup := func(err error) error {
		_ = tmp.Close()
		_ = os.Remove(name)
		return err
	}
	if err := tmp.Chmod(mode); err != nil {
		return cleanup(err)
	}
	if _, err := tmp.WriteString(data); err != nil {
		return cleanup(err)
	}
	if err := tmp.Sync(); err != nil {
		return cleanup(err)
	}
	if err := tmp.Close(); err != nil {
		_ = os.Remove(name)
		return err
	}
	if err := os.Rename(name, path); err != nil {
		_ = os.Remove(name)
		return err
	}
	return nil
}

func tomlKey(k string) string {
	if k != "" && strings.Trim(k, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-") == "" {
		return k
	}
	return quote(k)
}

func quote(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch {
		case r == '"':
			b.WriteString(`\"`)
		case r == '\\':
			b.WriteString(`\\`)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r == '\b':
			b.WriteString(`\b`)
		case r == '\f':
			b.WriteString(`\f`)
		case r < 0x20 || r == 0x7f:
			fmt.Fprintf(&b, `\u%04x`, r)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte('"')
	return b.String()
}

// tomlValue renders a decoded TOML value; ok is false for values that have
// no representation.
func tomlValue(v any) (string, bool) {
	switch x := v.(type) {
	case bool:
		return strconv.FormatBool(x), true
	case int64:
		return strconv.FormatInt(x, 10), true
	case int:
		return strconv.Itoa(x), true
	case float64:
		switch {
		case math.IsNaN(x):
			return "nan", true
		case math.IsInf(x, 1):
			return "inf", true
		case math.IsInf(x, -1):
			return "-inf", true
		}
		s := strconv.FormatFloat(x, 'g', -1, 64)
		if !strings.ContainsAny(s, ".en") {
			s += ".0"
		}
		return s, true
	case string:
		return quote(x), true
	case time.Time:
		// The decoder marks TOML's local date, datetime and time with these
		// locations; keep each kind as it was written (`added` is a local date
		// in files from earlier releases).
		switch x.Location().String() { // the toml package's zones for local values
		case localDate.String():
			return x.Format("2006-01-02"), true
		case "datetime-local":
			return x.Format("2006-01-02T15:04:05.999999999"), true
		case "time-local":
			return x.Format("15:04:05.999999999"), true
		}
		return x.Format(time.RFC3339Nano), true
	case []any:
		parts := make([]string, 0, len(x))
		for _, el := range x {
			if s, ok := tomlValue(el); ok {
				parts = append(parts, s)
			}
		}
		return "[" + strings.Join(parts, ", ") + "]", true
	case []map[string]any:
		parts := make([]string, 0, len(x))
		for _, el := range x {
			if s, ok := tomlValue(el); ok {
				parts = append(parts, s)
			}
		}
		return "[" + strings.Join(parts, ", ") + "]", true
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		var parts []string
		for _, k := range keys {
			if s, ok := tomlValue(x[k]); ok {
				parts = append(parts, tomlKey(k)+" = "+s)
			}
		}
		return "{" + strings.Join(parts, ", ") + "}", true
	}
	return "", false
}

// localDate marks a TOML local date, as the toml decoder does (its zone is
// named "date-local"); tomlValue writes such a time as a bare date.
var localDate = time.FixedZone("date-local", 0)

// TOMLKey renders a key, quoted unless it is bare-safe.
func TOMLKey(k string) string { return tomlKey(k) }

// TOMLValue renders a decoded TOML scalar or array of them (a local date,
// datetime or time keeps its kind); ok is false for a value with no
// representation.
func TOMLValue(v any) (string, bool) { return tomlValue(v) }
