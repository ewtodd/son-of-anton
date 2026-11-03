package main

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/BurntSushi/toml"
)

// The vectors under testdata/merge are the contract of `soa-d config merge`
// and `soa-d rules merge`: each scenario is a sequence of activations, run
// through the Python scripts (nix/config_merge.py and nix/rules_merge.py,
// since deleted) and recorded with the files and stderr each one produced.
// They are recorded results: never edit one to make Go pass.

// content is a recorded file: text, {"hex": ...} for bytes that are not
// UTF-8, or {"dir": true} for a directory.
type content struct {
	data  []byte
	isDir bool
}

func (c *content) UnmarshalJSON(b []byte) error {
	var s string
	if json.Unmarshal(b, &s) == nil {
		c.data = []byte(s)
		return nil
	}
	var o struct {
		Hex string `json:"hex"`
		Dir bool   `json:"dir"`
	}
	if err := json.Unmarshal(b, &o); err != nil {
		return err
	}
	c.isDir = o.Dir
	var err error
	c.data, err = hex.DecodeString(o.Hex)
	return err
}

type mergeStep struct {
	Declared *content            `json:"declared"` // nil: the declared file is missing
	Pre      map[string]*content `json:"pre"`      // edits made before the run; nil value: delete
	Args     []string            `json:"args"`
	State    *string             `json:"state"` // nil: no --state
	Exit     int                 `json:"exit"`
	Stderr   *string             `json:"stderr"` // nil: a crash, only the exit code is compared
	Files    map[string]content  `json:"files"`  // after the run
}

type mergeScenario struct {
	Name        string      `json:"name"`
	Doc         string      `json:"doc"`
	ParseDetail bool        `json:"parse_detail"`
	Steps       []mergeStep `json:"steps"`
}

func loadMergeVectors(t *testing.T, kind string) []mergeScenario {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", "merge", kind+".json"))
	if err != nil {
		t.Fatal(err)
	}
	var out []mergeScenario
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatal(err)
	}
	if len(out) == 0 {
		t.Fatal("no scenarios")
	}
	return out
}

type mergeKind struct {
	cmd, declared, target string
}

var mergeKinds = map[string]mergeKind{
	"config": {"config", "nix.toml", "config.toml"},
	"rules":  {"rules", "declared.toml", "memories/rules.toml"},
}

// A TOML parser's own wording differs from tomllib's; everything around it
// is compared.
var parseDetail = []*regexp.Regexp{
	regexp.MustCompile(`(is unusable \().*(\)\.\n)`),
	regexp.MustCompile(`(does not parse \().*(\)\. Fix)`),
}

func maskDetail(s string) string {
	for _, re := range parseDetail {
		s = re.ReplaceAllString(s, "${1}...${2}")
	}
	return s
}

func TestMergeVectors(t *testing.T) {
	for kind, mk := range mergeKinds {
		for _, sc := range loadMergeVectors(t, kind) {
			t.Run(kind+"/"+sc.Name+"/chained", func(t *testing.T) { runMergeScenario(t, mk, sc, false) })
			t.Run(kind+"/"+sc.Name+"/from-python-output", func(t *testing.T) { runMergeScenario(t, mk, sc, true) })
		}
	}
}

// runMergeScenario replays the steps. Chained, each activation starts from
// what Go wrote the activation before (Go's output read back by Go); fromPython,
// each starts from the files the Python recorded after the step before (the
// Python's output fed to Go). Either way the result must be the recorded one.
func runMergeScenario(t *testing.T, mk mergeKind, sc mergeScenario, fromPython bool) {
	dir := t.TempDir()
	declared := filepath.Join(dir, mk.declared)
	for i, st := range sc.Steps {
		if fromPython && i > 0 {
			for name := range sc.Steps[i-1].Files {
				_ = os.Remove(filepath.Join(dir, name))
			}
			for name, c := range sc.Steps[i-1].Files {
				writeMergeFile(t, filepath.Join(dir, name), c.data)
			}
		}
		_ = os.RemoveAll(declared)
		if st.Declared != nil {
			if st.Declared.isDir {
				if err := os.Mkdir(declared, 0o755); err != nil {
					t.Fatal(err)
				}
			} else {
				writeMergeFile(t, declared, st.Declared.data)
			}
		}
		for name, c := range st.Pre {
			p := filepath.Join(dir, name)
			if c == nil {
				_ = os.Remove(p)
			} else {
				writeMergeFile(t, p, c.data)
			}
		}
		before := readTree(t, dir, mk.declared)

		argv := []string{mk.cmd, "merge", declared, filepath.Join(dir, mk.target)}
		if st.State != nil {
			argv = append(argv, "--state", filepath.Join(dir, *st.State))
		}
		argv = append(argv, st.Args...)
		var stdout, stderr bytes.Buffer
		code := run(argv, &stdout, &stderr)

		if code != st.Exit {
			t.Fatalf("step %d: exit %d, want %d; stderr:\n%s", i+1, code, st.Exit, stderr.String())
		}
		if stdout.Len() != 0 {
			t.Errorf("step %d: stdout %q, want none", i+1, stdout.String())
		}
		if st.Stderr != nil {
			want := strings.ReplaceAll(*st.Stderr, "{DIR}", dir)
			got := stderr.String()
			if sc.ParseDetail {
				want, got = maskDetail(want), maskDetail(got)
			}
			if got != want {
				t.Errorf("step %d: stderr\n got: %q\nwant: %q", i+1, got, want)
			}
		}
		after := readTree(t, dir, mk.declared)
		names := sortedKeys(after)
		wantNames := sortedKeys(st.Files)
		if !reflect.DeepEqual(names, wantNames) {
			t.Fatalf("step %d: files %v, want %v", i+1, names, wantNames)
		}
		for _, name := range names {
			wantData := st.Files[name].data
			gotData := after[name]
			// What the Python left byte for byte as it was, Go must too.
			if prev, ok := before[name]; ok && bytes.Equal(wantData, prev) {
				if !bytes.Equal(gotData, prev) {
					t.Errorf("step %d: %s changed, the Python left it as it was:\n%s", i+1, name, gotData)
				}
				continue
			}
			if msg := sameParsed(name, gotData, wantData); msg != "" {
				t.Errorf("step %d: %s: %s\n got:\n%s\nwant:\n%s", i+1, name, msg, gotData, wantData)
			}
		}
	}
}

func writeMergeFile(t *testing.T, p string, data []byte) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, data, 0o644); err != nil {
		t.Fatal(err)
	}
}

// readTree is every regular file under dir (relative names) but skip.
func readTree(t *testing.T, dir, skip string) map[string][]byte {
	t.Helper()
	out := map[string][]byte{}
	err := filepath.WalkDir(dir, func(p string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		rel, _ := filepath.Rel(dir, p)
		if rel == skip {
			return nil
		}
		b, err := os.ReadFile(p)
		out[rel] = b
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// sameParsed compares two files the way a reader sees them: TOML as parsed
// (a local date is not a datetime), JSON as parsed (1 is not 1.0); a file
// that is neither, or does not parse, byte for byte. It returns "" when
// they agree.
func sameParsed(name string, got, want []byte) string {
	switch {
	case strings.HasSuffix(name, ".toml"):
		var g, w map[string]any
		_, ge := toml.Decode(string(got), &g)
		_, we := toml.Decode(string(want), &w)
		if ge == nil && we == nil {
			if a, b := canonValue(g), canonValue(w); a != b {
				return "parsed TOML differs:\n" + a + "\n" + b
			}
			return ""
		}
	case strings.HasSuffix(name, ".json"):
		var g, w any
		gd, wd := json.NewDecoder(bytes.NewReader(got)), json.NewDecoder(bytes.NewReader(want))
		gd.UseNumber()
		wd.UseNumber()
		if gd.Decode(&g) == nil && wd.Decode(&w) == nil {
			if !reflect.DeepEqual(g, w) {
				return "parsed JSON differs"
			}
			return ""
		}
	}
	if !bytes.Equal(got, want) {
		return "bytes differ"
	}
	return ""
}

// canonValue renders a decoded TOML value with its type, keys sorted.
func canonValue(v any) string {
	switch x := v.(type) {
	case map[string]any:
		var parts []string
		for _, k := range sortedKeys(x) {
			parts = append(parts, fmt.Sprintf("%q: %s", k, canonValue(x[k])))
		}
		return "{" + strings.Join(parts, ", ") + "}"
	case []map[string]any:
		parts := make([]string, len(x))
		for i, e := range x {
			parts[i] = canonValue(e)
		}
		return "tables[" + strings.Join(parts, ", ") + "]"
	case []any:
		parts := make([]string, len(x))
		for i, e := range x {
			parts[i] = canonValue(e)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	case string:
		return fmt.Sprintf("s%q", x)
	case int64:
		return fmt.Sprintf("i%d", x)
	case float64:
		return fmt.Sprintf("f%v", x)
	case bool:
		return fmt.Sprintf("b%v", x)
	}
	if tm, ok := v.(time.Time); ok {
		// The zone name tells a local date from a datetime; an offset datetime
		// is its instant and offset.
		if name := tm.Location().String(); strings.HasSuffix(name, "-local") {
			return name + " " + tm.Format("2006-01-02T15:04:05.999999999")
		}
		return "offset " + tm.Format(time.RFC3339Nano)
	}
	return fmt.Sprintf("%T:%v", v, v)
}
