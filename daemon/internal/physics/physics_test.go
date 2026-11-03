package physics

import (
	"strings"
	"testing"
)

func TestFormatCheckForms(t *testing.T) {
	keys := []string{"a", "b", "c"}
	for _, c := range []struct {
		name, text string
		found      bool
		want       string
	}{
		{"missing file", "", false, "RESULTS.txt: missing."},
		{"all good", "a = 1\nb = -2.5e3\nc = .5\n", true, "RESULTS.txt: every key present, each a plain number."},
		{"missing keys", "a = 1\n", true, "RESULTS.txt: missing keys: b, c."},
		{"not plain", "a = 1\nb = 2\nc = 1.2 ± 0.1\n", true, `RESULTS.txt: not plain numbers: c ("1.2 ± 0.1").`},
		{"both", "a = 1\nc = 1.2 ± 0.1\n", true, `RESULTS.txt: missing keys: b; not plain numbers: c ("1.2 ± 0.1").`},
		{"several bad", "a = nan\nb = 3 keV\nc = 1\n", true, `RESULTS.txt: not plain numbers: a ("nan"), b ("3 keV").`},
		{"empty file", "", true, "RESULTS.txt: missing keys: a, b, c."},
		{"empty value", "a =\nb = 1\nc = 2\n", true, `RESULTS.txt: not plain numbers: a ("").`},
		{"last value wins", "a = 1 +- 2\na = 3\nb = 1\nc = 2\n", true, "RESULTS.txt: every key present, each a plain number."},
		{"the last line can be the bad one", "a = 3\na = abc\nb = 1\nc = 2\n", true, `RESULTS.txt: not plain numbers: a ("abc").`},
		{"comments and spaces", "# a = nope\n  a  =  7  \n\tb=1\nc = +2\n", true, "RESULTS.txt: every key present, each a plain number."},
		{"a comment is not a key", "# a = 1\nb = 1\nc = 2\n", true, "RESULTS.txt: missing keys: a."},
	} {
		if got := FormatCheck(keys, c.text, c.found); got != c.want {
			t.Errorf("%s: %q, want %q", c.name, got, c.want)
		}
	}
}

func TestScore(t *testing.T) {
	checks := []Check{{ID: "1", Key: "a", Expected: 10, Tolerance: 0.5}, {ID: "2", Key: "b", Expected: -3, Tolerance: 1}, {ID: "3", Key: "c", Expected: 0, Tolerance: 0.001}}
	e := Score(checks, "a = 10.5\nb = -4.0001\n# c = 0\nc = 1e-4\n", true)
	if e.OK() || e.Passed != 2 || !e.Results[0].Passed || e.Results[1].Passed || !e.Results[2].Passed {
		t.Fatalf("%+v", e)
	}
	md := e.Markdown()
	for _, want := range []string{"Status: failed", "Checks passed: 2/3", "- 1 (a): expected 10, tolerance 0.5, value 10.5: passed", "- 2 (b): expected -3, tolerance 1, value -4.0001: failed"} {
		if !strings.Contains(md, want) {
			t.Errorf("FORMAL_EVAL.md lacks %q:\n%s", want, md)
		}
	}
	e = Score(checks, "a = x\n", true)
	md = e.Markdown()
	if e.Passed != 0 || !strings.Contains(md, `no value: not a plain number ("x")`) || !strings.Contains(md, "no value: no line for the key") {
		t.Errorf("%s", md)
	}
	if md := Score(checks, "", false).Markdown(); !strings.Contains(md, "no value: RESULTS.txt is missing") {
		t.Errorf("%s", md)
	}
	if !Score(checks[:1], "a = 9.5\n", true).OK() {
		t.Error("a value exactly one tolerance away should pass")
	}
}

func TestParseSpec(t *testing.T) {
	const ok = `name = "p"
problem = "report alpha"
[[checks]]
id = "x"
key = "alpha"
expected = 3
tolerance = 0.1
`
	s, err := ParseSpec(ok, nil)
	if err != nil || s.Name != "p" || len(s.Checks) != 1 || s.Checks[0].Expected != 3 || strings.Join(s.Keys(), ",") != "alpha" {
		t.Fatalf("%+v %v", s, err)
	}
	for name, spec := range map[string]string{
		"name":       strings.Replace(ok, `"p"`, `"a b"`, 1),
		"slug start": strings.Replace(ok, `"p"`, `"-p"`, 1),
		"key":        strings.Replace(ok, `"alpha"`, `"beta"`, 1),
	} {
		if _, err := ParseSpec(spec, nil); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestPromptsHaveNoPlaceholdersLeft(t *testing.T) {
	w := WorkerPrompt("/runs/r1", []string{"/data/a", "/data/b"}, []string{"k1", "k2"})
	m := ManagerPrompt("/runs/r1", []string{"k1", "k2"}, "Find {KEYS} and {RUN_DIR}.")
	for _, p := range []string{w, m} {
		for _, ph := range []string{"{RUN_DIR}", "{DATA}", "{KEYS}"} {
			if strings.Contains(strings.Replace(p, "Find {KEYS} and {RUN_DIR}.", "", 1), ph) {
				t.Errorf("a placeholder %s is left in:\n%s", ph, p)
			}
		}
	}
	for _, want := range []string{"Your run directory is /runs/r1.", "mounted read-only at /data/a, /data/b,", "The keys are: k1, k2.", "your home is /runs/r1/.home."} {
		if !strings.Contains(w, want) {
			t.Errorf("worker prompt lacks %q", want)
		}
	}
	if !strings.HasSuffix(m, "The problem:\nFind {KEYS} and {RUN_DIR}.") || !strings.Contains(m, "RESULTS.txt has every key (k1, k2)") {
		t.Errorf("manager prompt:\n%s", m)
	}
}

func TestMessages(t *testing.T) {
	if got := FeedbackMessage(2, 8, "Do X.", "RESULTS.txt: missing."); got != "Reviewer's feedback on work session 2:\n\nDo X.\n\nRESULTS.txt: missing.\n\nThis is work session 3 of at most 8." {
		t.Errorf("%q", got)
	}
	if got := FeedbackMessage(7, 8, "Do X.", "c"); !strings.HasSuffix(got, "This is work session 8 of at most 8. It is your last: leave RESULTS.txt complete with your best values before you stop.") {
		t.Errorf("%q", got)
	}
	if got := BeginMessage("# Problem\n\ntext\n"); got != "# Problem\n\ntext\n\nBegin." {
		t.Errorf("%q", got)
	}
}

func TestClip(t *testing.T) {
	var lines []string
	for i := 1; i <= 100; i++ {
		lines = append(lines, "l"+string(rune('0'+i%10)))
	}
	got := Clip(strings.Join(lines, "\n"))
	if n := strings.Count(got, "\n") + 1; n != 46 || !strings.Contains(got, "[55 lines omitted]") {
		t.Errorf("%d lines:\n%s", n, got)
	}
	if short := Clip("a\nb\n"); short != "a\nb" {
		t.Errorf("%q", short)
	}
	long := Clip(strings.Repeat("y", 10000))
	if n := len(long); n > 4100 || !strings.Contains(long, "[clipped]") {
		t.Errorf("%d characters", n)
	}
}
