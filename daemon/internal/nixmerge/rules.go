package nixmerge

import (
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"sort"
	"strings"
	"unicode"
)

const rulesUsage = `usage: soa-d rules merge DECLARED RULES --state STATE
`

// What Nix owns of a rule. Anything else on a disk entry (added, notes the
// user wrote, ...) is the user's and is left alone.
var managedKeys = []string{"text", "why", "scope", "source"}

// RunRules runs `soa-d rules ...` (args after "rules") and returns the exit
// code.
func RunRules(args []string, stderr io.Writer) int {
	if len(args) == 0 || args[0] != "merge" {
		fmt.Fprint(stderr, rulesUsage)
		return 2
	}
	var pos []string
	var state string
	rest := args[1:]
	for i := 0; i < len(rest); i++ {
		switch a := rest[i]; {
		case a == "--state":
			if i+1 >= len(rest) {
				fmt.Fprint(stderr, rulesUsage)
				return 2
			}
			i++
			state = rest[i]
		case strings.HasPrefix(a, "--state="):
			state = strings.TrimPrefix(a, "--state=")
		case strings.HasPrefix(a, "--"):
			fmt.Fprint(stderr, rulesUsage)
			return 2
		default:
			pos = append(pos, a)
		}
	}
	if len(pos) != 2 || state == "" {
		fmt.Fprint(stderr, rulesUsage)
		return 2
	}
	if err := mergeRulesFiles(pos[0], pos[1], state, stderr); err != nil {
		fmt.Fprintf(stderr, "soa-d rules merge: %v\n", err)
		return 1
	}
	return 0
}

// managed is the Nix-owned view of an entry, with the defaults Nix would
// have written.
func managed(entry *tbl) *tbl {
	view := newTbl()
	text, _ := entry.get("text")
	view.set("text", text)
	scope, ok := entry.get("scope")
	if !ok {
		scope = "common"
	}
	view.set("scope", scope)
	if why, _ := entry.get("why"); truthy(why) {
		view.set("why", why)
	}
	if src, _ := entry.get("source"); src != nil {
		view.set("source", src)
	}
	return view
}

// withID is {"id": rid, **managed(entry)}.
func withID(rid string, entry *tbl) *tbl {
	out := newTbl()
	out.set("id", rid)
	m := managed(entry)
	for _, k := range m.keys {
		out.set(k, m.m[k])
	}
	return out
}

// canonical is a declared rule as it is compared and recorded.
func canonical(entry *tbl) *tbl {
	e := newTbl()
	e.set("source", "user")
	for _, k := range entry.keys {
		e.set(k, entry.m[k])
	}
	id, _ := entry.get("id")
	return withID(fmt.Sprint(id), e)
}

// mergeRules returns (result, newState, report). declared are canonical
// entries; lastWritten is nil when there is no state file.
func mergeRules(declared []*tbl, onDisk []*tbl, lastWritten *tbl) ([]*tbl, *tbl, []string) {
	want := newTbl()
	var wantIDs []string
	for _, e := range declared {
		id := e.m["id"].(string)
		want.set(id, e)
		wantIDs = append(wantIDs, id)
	}
	last := lastWritten
	if last == nil {
		last = newTbl()
	}
	result := make([]*tbl, len(onDisk))
	byID := map[string]*tbl{}
	for i, r := range onDisk {
		result[i] = deepCopy(r).(*tbl)
		if id, ok := result[i].m["id"].(string); ok {
			if _, seen := byID[id]; !seen {
				byID[id] = result[i]
			}
		}
	}
	state := newTbl()
	var report []string

	for _, rid := range wantIDs {
		canon := want.m[rid].(*tbl)
		disk := byID[rid]
		if disk == nil {
			fresh := deepCopy(canon).(*tbl)
			result = append(result, fresh)
			byID[rid] = fresh
			state.set(rid, canon)
			report = append(report, "rules.toml: added "+rid)
			continue
		}
		current := withID(rid, disk)
		lastRid, inLast := last.get(rid)
		switch {
		case pyEq(current, canon):
			state.set(rid, canon)
		case inLast && pyEq(current, lastRid):
			for _, key := range managedKeys {
				if v, ok := canon.get(key); ok {
					disk.set(key, v)
				} else {
					disk.del(key)
				}
			}
			state.set(rid, canon)
			report = append(report, "rules.toml: updated "+rid+" to the declared value")
		default:
			why := "no record of Nix writing it"
			if inLast {
				why = "changed after Nix wrote it"
			}
			report = append(report, fmt.Sprintf("rules.toml: kept %s as it is on disk — it differs from the declared value (%s)", rid, why))
			if inLast {
				state.set(rid, lastRid)
			}
		}
	}

	for _, rid := range last.keys {
		if _, ok := want.get(rid); ok {
			continue
		}
		disk := byID[rid]
		if disk == nil {
			continue
		}
		if pyEq(withID(rid, disk), last.m[rid]) {
			for i, r := range result {
				if r == disk {
					result = append(result[:i], result[i+1:]...)
					break
				}
			}
			report = append(report, "rules.toml: removed "+rid+" (retired in Nix)")
		} else {
			report = append(report, "rules.toml: kept "+rid+" — no longer declared in Nix, but it changed after Nix wrote it")
		}
	}
	return result, state, report
}

var idRE = regexp.MustCompile(`\A[a-z0-9][a-z0-9-]*\z`)

// pyStrip is str.strip's whitespace.
func pyBlank(s string) bool {
	return strings.TrimFunc(s, func(r rune) bool {
		return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f)
	}) == ""
}

// validateDeclared returns the canonical declared rules from a parsed file,
// or an error naming the problem. The declared input can be a runtime file
// that Nix never type-checked, so the shape Nix options enforce is enforced
// here.
func validateDeclared(data *tbl) ([]*tbl, error) {
	rv, ok := data.get("rule")
	if !ok {
		return nil, errors.New("no [[rule]] tables")
	}
	rules, ok := rv.([]any)
	if !ok {
		return nil, errors.New("`rule` is not an array of tables")
	}
	var out []*tbl
	seen := map[string]bool{}
	for n, raw := range rules {
		entry, ok := raw.(*tbl)
		if !ok {
			return nil, fmt.Errorf("rule #%d is not a table", n+1)
		}
		rid, isStr := entry.m["id"].(string)
		label := fmt.Sprintf("rule #%d", n+1)
		if isStr {
			label += " (" + rid + ")"
		}
		if !isStr || !idRE.MatchString(rid) {
			return nil, fmt.Errorf("%s: id must match [a-z0-9][a-z0-9-]*", label)
		}
		if seen[rid] {
			return nil, fmt.Errorf("%s: duplicate id", label)
		}
		seen[rid] = true
		if text, ok := entry.m["text"].(string); !ok || pyBlank(text) {
			return nil, fmt.Errorf("%s: text must be a non-empty string", label)
		}
		if why, ok := entry.get("why"); ok {
			if _, isStr := why.(string); !isStr {
				return nil, fmt.Errorf("%s: why must be a string", label)
			}
		}
		scope, ok := entry.get("scope")
		if !ok {
			scope = "common"
		}
		if s, _ := scope.(string); s != "common" && s != "gateway" {
			return nil, fmt.Errorf("%s: scope must be one of common, gateway", label)
		}
		source, ok := entry.get("source")
		if !ok {
			source = "user"
		}
		if s, _ := source.(string); s != "user" && s != "agent" {
			return nil, fmt.Errorf("%s: source must be one of user, agent", label)
		}
		var unknown []string
		for _, k := range entry.keys {
			switch k {
			case "id", "text", "why", "scope", "source":
			default:
				unknown = append(unknown, k)
			}
		}
		if len(unknown) > 0 {
			sort.Strings(unknown)
			return nil, fmt.Errorf("%s: unknown key(s) %s", label, strings.Join(unknown, ", "))
		}
		clean := newTbl()
		clean.set("id", rid)
		clean.set("text", entry.m["text"])
		clean.set("scope", scope)
		clean.set("source", source)
		if why, _ := entry.get("why"); truthy(why) {
			clean.set("why", why)
		}
		out = append(out, canonical(clean))
	}
	return out, nil
}

func sameRules(a, b []*tbl) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if !pyEq(a[i], b[i]) {
			return false
		}
	}
	return true
}

func mergeRulesFiles(declaredPath, rulesPath, statePath string, stderr io.Writer) error {
	// A declared file that is missing, unreadable or invalid changes NOTHING:
	// it must never read as "Nix declares no rules", which would retire every
	// rule Nix wrote.
	declared, err := func() ([]*tbl, error) {
		data, err := loadTOML(declaredPath)
		if err != nil {
			return nil, err
		}
		return validateDeclared(data)
	}()
	if err != nil {
		fmt.Fprintf(stderr, "rules.toml: left untouched — declared rules file %s is unusable (%s).\n", declaredPath, err)
		return nil
	}
	state := loadState(statePath)
	_, statErr := os.Stat(rulesPath)
	exists := statErr == nil
	var onDisk []*tbl
	if exists {
		data, err := loadTOML(rulesPath)
		if err != nil {
			fmt.Fprintf(stderr, "rules.toml: left untouched — it does not parse (%s). Fix it by "+
				"hand; the next activation will merge the declared rules.\n", err)
			return nil
		}
		if rv, ok := data.get("rule"); ok {
			list, isList := rv.([]any)
			if !isList {
				return fmt.Errorf("%s: `rule` is not an array of tables", rulesPath)
			}
			for _, e := range list {
				t, ok := e.(*tbl)
				if !ok {
					return fmt.Errorf("%s: `rule` is not an array of tables", rulesPath)
				}
				onDisk = append(onDisk, t)
			}
		}
	}
	if len(declared) == 0 && (state == nil || len(state.keys) == 0) && !exists {
		return nil
	}

	result, newState, report := mergeRules(declared, onDisk, state)
	for _, line := range report {
		fmt.Fprintln(stderr, line)
	}
	if !sameRules(result, onDisk) || !exists {
		list := make([]any, len(result))
		for i, r := range result {
			list[i] = r
		}
		root := newTbl()
		root.set("rule", list)
		text, err := dumpTOML(root)
		if err != nil {
			return err
		}
		if err := writeAtomic(rulesPath, text); err != nil {
			return err
		}
	}
	js, err := dumpJSON(newState)
	if err != nil {
		return err
	}
	return writeAtomic(statePath, js)
}
