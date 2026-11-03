package physics

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"github.com/ewtodd/son-of-anton/daemon/internal/sandbox"
)

// plainNumber is a value RESULTS.txt may hold: no units, no uncertainty.
var plainNumber = regexp.MustCompile(`^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$`)

// Value is what RESULTS.txt says for one key (the last line that names it).
type Value struct {
	Raw string
	Num float64
	OK  bool // Raw is a plain number
}

// ParseResults reads RESULTS.txt's lines `key = value`. A `#` line is a
// comment; a line without `=` names no key; the last line of a key wins.
func ParseResults(text string) map[string]Value {
	out := map[string]Value{}
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, raw, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		key, raw = strings.TrimSpace(key), strings.TrimSpace(raw)
		v := Value{Raw: raw}
		if plainNumber.MatchString(raw) {
			if n, err := strconv.ParseFloat(raw, 64); err == nil {
				v.Num, v.OK = n, true
			}
		}
		out[key] = v
	}
	return out
}

// ReadResults reads <runDir>/RESULTS.txt through the confined opener; found is
// false when it is not there or cannot be read (a symlink out of the run
// directory, say).
func ReadResults(fs *sandbox.FS, runDir string) (text string, found bool) {
	b, err := fs.ReadFile(runDir + "/RESULTS.txt")
	if err != nil {
		return "", false
	}
	return string(b), true
}

// FormatCheck says whether RESULTS.txt has every key as a plain number. It
// never says whether a value is right.
func FormatCheck(keys []string, text string, found bool) string {
	if !found {
		return "RESULTS.txt: missing."
	}
	vals := ParseResults(text)
	var missing, bad []string
	for _, k := range keys {
		v, ok := vals[k]
		switch {
		case !ok:
			missing = append(missing, k)
		case !v.OK:
			bad = append(bad, fmt.Sprintf("%s (%q)", k, v.Raw))
		}
	}
	var parts []string
	if len(missing) > 0 {
		parts = append(parts, "missing keys: "+strings.Join(missing, ", "))
	}
	if len(bad) > 0 {
		parts = append(parts, "not plain numbers: "+strings.Join(bad, ", "))
	}
	if len(parts) == 0 {
		return "RESULTS.txt: every key present, each a plain number."
	}
	return "RESULTS.txt: " + strings.Join(parts, "; ") + "."
}

// CheckResult is one scored check.
type CheckResult struct {
	Check
	Value  float64
	Has    bool   // there is a value
	Why    string // why there is none
	Passed bool
}

// Eval is a scored run.
type Eval struct {
	Results []CheckResult
	Passed  int
}

// OK says whether every check passed.
func (e Eval) OK() bool { return e.Passed == len(e.Results) }

// Score checks RESULTS.txt against the spec's checks.
func Score(checks []Check, text string, found bool) Eval {
	vals := ParseResults(text)
	var e Eval
	for _, c := range checks {
		r := CheckResult{Check: c}
		v, ok := vals[c.Key]
		switch {
		case !found:
			r.Why = "RESULTS.txt is missing"
		case !ok:
			r.Why = "no line for the key"
		case !v.OK:
			r.Why = fmt.Sprintf("not a plain number (%q)", v.Raw)
		default:
			r.Value, r.Has = v.Num, true
			d := v.Num - c.Expected
			if d < 0 {
				d = -d
			}
			r.Passed = d <= c.Tolerance
		}
		if r.Passed {
			e.Passed++
		}
		e.Results = append(e.Results, r)
	}
	return e
}

func num(f float64) string { return strconv.FormatFloat(f, 'g', -1, 64) }

// Markdown is FORMAL_EVAL.md.
func (e Eval) Markdown() string {
	status := "failed"
	if e.OK() {
		status = "passed"
	}
	var b strings.Builder
	fmt.Fprintf(&b, "# Formal evaluation\n\nStatus: %s\nChecks passed: %d/%d\n\n", status, e.Passed, len(e.Results))
	for _, r := range e.Results {
		fmt.Fprintf(&b, "- %s (%s): expected %s, tolerance %s, ", r.ID, r.Key, num(r.Expected), num(r.Tolerance))
		switch {
		case !r.Has:
			fmt.Fprintf(&b, "no value: %s: failed\n", r.Why)
		case r.Passed:
			fmt.Fprintf(&b, "value %s: passed\n", num(r.Value))
		default:
			fmt.Fprintf(&b, "value %s: failed\n", num(r.Value))
		}
	}
	return b.String()
}

// Status is "passed" or "failed".
func (e Eval) Status() string {
	if e.OK() {
		return "passed"
	}
	return "failed"
}
