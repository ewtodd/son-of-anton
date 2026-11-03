// Package physics is the pure part of the physics mode (phase 11c): the spec
// file and its validation, RESULTS.txt's format check and scoring, the
// prompts and messages the two sessions get, and the run's records. The
// controller that drives the sessions is in internal/server.
package physics

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/BurntSushi/toml"
)

// Check is one scored value: RESULTS.txt's Key must lie within Tolerance of
// Expected. Checks never leave the daemon.
type Check struct {
	ID        string
	Key       string
	Expected  float64
	Tolerance float64
}

// Spec is a validated problem spec.
type Spec struct {
	Name    string
	Problem string
	// Data are the data paths as written; DataReal the same, symlinks resolved
	// (what the sandbox binds).
	Data     []string
	DataReal []string
	Checks   []Check
}

// Keys are the distinct check keys, in the order the checks first name them.
func (s Spec) Keys() []string {
	var keys []string
	seen := map[string]bool{}
	for _, c := range s.Checks {
		if !seen[c.Key] {
			seen[c.Key] = true
			keys = append(keys, c.Key)
		}
	}
	return keys
}

var slugRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`)

// ParseSpec reads and validates a spec. dataDirs are physics.data_dirs: every
// data path must exist and lie under one of them.
func ParseSpec(text string, dataDirs []string) (Spec, error) {
	var raw struct {
		Name     string   `toml:"name"`
		Problem  string   `toml:"problem"`
		Data     []string `toml:"data"`
		Template any      `toml:"answer_template"`
		Checker  any      `toml:"checker"`
		Checks   []struct {
			ID        string `toml:"id"`
			Key       string `toml:"key"`
			Expected  any    `toml:"expected"`
			Tolerance any    `toml:"tolerance"`
		} `toml:"checks"`
	}
	md, err := toml.Decode(text, &raw)
	if err != nil {
		return Spec{}, fmt.Errorf("the spec is not valid TOML: %v", err)
	}
	for _, f := range []string{"answer_template", "checker"} {
		if md.IsDefined(f) {
			return Spec{}, fmt.Errorf("%s is not supported (it is a field of the Python harness's specs); scoring is by [[checks]] on RESULTS.txt", f)
		}
	}
	if !slugRe.MatchString(raw.Name) {
		return Spec{}, fmt.Errorf("name %q is not a slug (letters, digits, - and _, starting with a letter or digit)", raw.Name)
	}
	if strings.TrimSpace(raw.Problem) == "" {
		return Spec{}, errors.New("problem is empty")
	}
	if len(raw.Checks) == 0 {
		return Spec{}, errors.New("the spec has no [[checks]]")
	}
	s := Spec{Name: raw.Name, Problem: raw.Problem}
	for i, c := range raw.Checks {
		label := fmt.Sprintf("check %d", i+1)
		if c.ID != "" {
			label = "check " + c.ID
		}
		if strings.TrimSpace(c.Key) == "" {
			return Spec{}, fmt.Errorf("%s has no key", label)
		}
		exp, ok := number(c.Expected)
		if !ok {
			return Spec{}, fmt.Errorf("%s: expected must be a number", label)
		}
		tol, ok := number(c.Tolerance)
		if !ok || tol <= 0 {
			return Spec{}, fmt.Errorf("%s: tolerance must be a positive number", label)
		}
		if !strings.Contains(raw.Problem, c.Key) {
			return Spec{}, fmt.Errorf("%s: the key %q does not appear in the problem text", label, c.Key)
		}
		id := c.ID
		if id == "" {
			id = fmt.Sprint(i + 1)
		}
		s.Checks = append(s.Checks, Check{ID: id, Key: c.Key, Expected: exp, Tolerance: tol})
	}
	var allowed []string
	for _, d := range dataDirs {
		if real, err := filepath.EvalSymlinks(d); err == nil {
			allowed = append(allowed, real)
		}
	}
	for _, d := range raw.Data {
		if !filepath.IsAbs(d) {
			return Spec{}, fmt.Errorf("data path %q is not absolute", d)
		}
		real, err := filepath.EvalSymlinks(d)
		if err != nil {
			return Spec{}, fmt.Errorf("data path %s does not exist", d)
		}
		if _, err := os.Stat(real); err != nil {
			return Spec{}, fmt.Errorf("data path %s does not exist", d)
		}
		under := false
		for _, a := range allowed {
			under = under || real == a || strings.HasPrefix(real, a+"/")
		}
		if !under {
			return Spec{}, fmt.Errorf("data path %s is not under physics.data_dirs", d)
		}
		s.Data = append(s.Data, d)
		s.DataReal = append(s.DataReal, real)
	}
	return s, nil
}

// number reads a TOML integer or float.
func number(v any) (float64, bool) {
	switch n := v.(type) {
	case int64:
		return float64(n), true
	case float64:
		return n, true
	}
	return 0, false
}
