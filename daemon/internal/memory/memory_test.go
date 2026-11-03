package memory_test

// Phase 3b: the rule and fact stores, ported from ../tools/memory_tool.py.
// The files are the Python fork's (the gateway and soa-d share play's home
// until phase 8), so the format and the .lock sidecars must match.

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/BurntSushi/toml"
	"github.com/ewtodd/son-of-anton/daemon/internal/memory"
)

func newStore(t *testing.T) (*memory.Store, string) {
	t.Helper()
	home := t.TempDir()
	return memory.New(home), home
}

func read(t *testing.T, home, name string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(home, "memories", name))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func write(t *testing.T, home, name, body string) {
	t.Helper()
	p := filepath.Join(home, "memories", name)
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func memErr(t *testing.T, err error) *memory.Error {
	t.Helper()
	var e *memory.Error
	if !errors.As(err, &e) {
		t.Fatalf("err = %v, want a *memory.Error", err)
	}
	return e
}

func TestIDsComeFromTheText(t *testing.T) {
	// Expected values computed with the Python make_entry_id.
	for text, want := range map[string]string{
		"The NAS lives at 10.0.0.3 on the lan":                         "the-nas-lives-at-10-0",
		"Ask before deleting any mail.":                                "ask-before-deleting-any-mail",
		"one two three four five six seven":                            "one-two-three-four-five-six",
		"Pneumonoultramicroscopicsilicovolcanoconiosis is a long word": "pneumonoultramicroscopicsilicovo",
		"!!!": "fact",
	} {
		if got := memory.EntryID(text, nil, "fact"); got != want {
			t.Errorf("EntryID(%q) = %q, want %q", text, got, want)
		}
	}
	taken := map[string]bool{"ask-before-deleting-any-mail": true, "ask-before-deleting-any-mail-2": true}
	if got := memory.EntryID("Ask before deleting any mail.", taken, "rule"); got != "ask-before-deleting-any-mail-3" {
		t.Fatalf("a clash gets the next free suffix: %q", got)
	}
}

func TestFactsAddReplaceAndRemoveByID(t *testing.T) {
	st, home := newStore(t)
	res, err := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: "The NAS lives at 10.0.0.3 on the lan"}, "common")
	if err != nil || res.ID != "the-nas-lives-at-10-0" || res.Kind != "fact" || res.Scope != "common" {
		t.Fatalf("add = %+v, %v", res, err)
	}
	raw := read(t, home, "facts.toml")
	var file struct {
		Fact []map[string]any `toml:"fact"`
	}
	if _, err := toml.Decode(raw, &file); err != nil || len(file.Fact) != 1 {
		t.Fatalf("facts.toml = %q, %v", raw, err)
	}
	f := file.Fact[0]
	if f["id"] != "the-nas-lives-at-10-0" || f["source"] != "agent" || f["added"] == nil || f["scope"] != nil {
		t.Fatalf("entry = %v; id, text, source = agent, added; the file is the scope", f)
	}
	if _, err := st.Apply(memory.Change{Kind: "fact", Action: "replace", ID: res.ID, Text: "The NAS moved to 10.0.0.4"}, "common"); err != nil {
		t.Fatal(err)
	}
	facts, _ := st.Facts("common")
	if len(facts) != 1 || facts[0].ID != res.ID || facts[0].Text != "The NAS moved to 10.0.0.4" {
		t.Fatalf("replace keeps the id: %+v", facts)
	}
	if _, err := st.Apply(memory.Change{Kind: "fact", Action: "remove", ID: res.ID}, "common"); err != nil {
		t.Fatal(err)
	}
	if facts, _ := st.Facts("common"); len(facts) != 0 {
		t.Fatalf("remove: %+v", facts)
	}
	e := memErr(t, func() error {
		_, err := st.Apply(memory.Change{Kind: "fact", Action: "remove", ID: "nope"}, "common")
		return err
	}())
	if !strings.Contains(e.Message, "No fact with id 'nope'") {
		t.Fatalf("unknown id: %+v", e)
	}
}

func TestRulesAreScopedAndCountTowardTheCap(t *testing.T) {
	st, home := newStore(t)
	st.RulesCharLimit = 200 // two rules fit (about 155 characters), a third does not
	res, err := st.Apply(memory.Change{Kind: "rule", Action: "add", Text: "Ask before deleting   any mail.", Why: "lost mail once"}, "common")
	if err != nil || res.ID != "ask-before-deleting-any-mail" || res.Scope != "common" || !strings.Contains(res.Usage, "/200 chars") {
		t.Fatalf("add rule = %+v, %v", res, err)
	}
	if raw := read(t, home, "rules.toml"); !strings.Contains(raw, `text = "Ask before deleting any mail."`) || !strings.Contains(raw, `why = "lost mail once"`) || !strings.Contains(raw, `scope = "common"`) {
		t.Fatalf("rules.toml = %s; rule text is whitespace-collapsed", raw)
	}
	if _, err := st.Apply(memory.Change{Kind: "rule", Action: "add", Text: "Keep personal plans out of the terminal", Scope: "gateway"}, "gateway"); err != nil {
		t.Fatal(err)
	}
	e := memErr(t, func() error {
		_, err := st.Apply(memory.Change{Kind: "rule", Action: "add", Text: "Another long standing instruction that will not fit under the cap"}, "common")
		return err
	}())
	if !strings.Contains(e.Message, "The rules store is full") || !strings.Contains(e.Message, "do not pick one yourself") {
		t.Fatalf("full store: %q", e.Message)
	}
	rules, _ := st.Rules()
	if len(rules) != 2 {
		t.Fatalf("a full store refuses and never evicts: %+v", rules)
	}
	// Shrinking while over the cap is allowed.
	st.RulesCharLimit = 10
	if _, err := st.Apply(memory.Change{Kind: "rule", Action: "replace", ID: "ask-before-deleting-any-mail", Text: "Ask first."}, "common"); err != nil {
		t.Fatalf("a shrinking change over the cap: %v", err)
	}
}

func TestScopesAndAliases(t *testing.T) {
	st, home := newStore(t)
	for i, scope := range []string{"shared", "cli", " Common ", "current", ""} {
		text := fmt.Sprintf("alpha%d beta%d gamma%d", i, i, i)
		if _, err := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: text, Scope: scope}, "common"); err != nil {
			t.Fatalf("scope %q: %v", scope, err)
		}
	}
	if facts, _ := st.Facts("common"); len(facts) != 5 {
		t.Fatalf("common aliases and the caller's own scope all land in facts.toml: %+v", facts)
	}
	if _, err := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: "a personal thing", Scope: "gateway"}, "gateway"); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(read(t, home, "facts.gateway.toml"), "a personal thing") || strings.Contains(read(t, home, "facts.toml"), "a personal thing") {
		t.Fatal("gateway facts go to facts.gateway.toml")
	}
	if got, ok := memory.NormalizeScope("shared"); got != "common" || !ok {
		t.Fatalf("NormalizeScope(shared) = %q, %v", got, ok)
	}
	if _, ok := memory.NormalizeScope("telegram"); ok {
		t.Fatal("an unknown scope is never treated as either scope")
	}
	e := memErr(t, func() error {
		_, err := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: "x", Scope: "bogus"}, "common")
		return err
	}())
	if !strings.Contains(e.Message, "Unknown memory scope 'bogus'") {
		t.Fatalf("bogus scope: %q", e.Message)
	}
}

func TestNearDuplicateFactsAreRefusedWithTheExistingID(t *testing.T) {
	st, _ := newStore(t)
	first, _ := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: "The NAS lives at 10.0.0.3 on the lan"}, "common")
	e := memErr(t, func() error {
		_, err := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: "the NAS lives at 10.0.0.3, on the LAN."}, "common")
		return err
	}())
	if !strings.Contains(e.Message, "near-identical fact already exists") || len(e.Similar) != 1 || e.Similar[0].ID != first.ID {
		t.Fatalf("near duplicate: %+v", e)
	}
	if _, err := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: "The printer is in the attic"}, "common"); err != nil {
		t.Fatalf("an unrelated fact: %v", err)
	}
	// The embedding hook, when set, decides instead.
	st.Similar = func(scope, text string) []memory.Fact { return []memory.Fact{{ID: "from-embeddings", Text: "x"}} }
	e = memErr(t, func() error {
		_, err := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: "Completely different words entirely"}, "common")
		return err
	}())
	if len(e.Similar) != 1 || e.Similar[0].ID != "from-embeddings" {
		t.Fatalf("Similar hook: %+v", e)
	}
}

func TestACommonCallerNeverReadsGatewayMemory(t *testing.T) {
	st, _ := newStore(t)
	if _, err := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: "My therapist is Dr Smith on Tuesdays", Scope: "gateway"}, "gateway"); err != nil {
		t.Fatal(err)
	}
	// Filing for the other surface works...
	if _, err := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: "Dentist appointment is in March", Scope: "gateway"}, "common"); err != nil {
		t.Fatalf("a common caller may file a gateway fact: %v", err)
	}
	// ...but no error ever hands it gateway text or ids.
	_, err := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: "my therapist is Dr Smith, on Tuesdays", Scope: "gateway"}, "common")
	e := memErr(t, err)
	if len(e.Similar) != 0 || strings.Contains(e.Message, "therapist") || strings.Contains(e.Message, "my-therapist") {
		t.Fatalf("a near-duplicate refusal leaks gateway memory to a common caller: %+v", e)
	}
	_, err = st.Apply(memory.Change{Kind: "fact", Action: "remove", ID: "nope", Scope: "gateway"}, "common")
	e = memErr(t, err)
	if len(e.IDs) != 0 || strings.Contains(e.Message, "my-therapist") {
		t.Fatalf("an unknown-id error lists gateway ids to a common caller: %+v", e)
	}
	// A gateway caller gets the details.
	_, err = st.Apply(memory.Change{Kind: "fact", Action: "remove", ID: "nope", Scope: "gateway"}, "gateway")
	if e := memErr(t, err); len(e.IDs) != 2 {
		t.Fatalf("a gateway caller sees its ids: %+v", e)
	}
}

func TestFactsHaveALengthLimit(t *testing.T) {
	st, _ := newStore(t)
	e := memErr(t, func() error {
		_, err := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: strings.Repeat("x", memory.DefaultFactCharLimit+1)}, "common")
		return err
	}())
	if !strings.Contains(e.Message, "notes/") {
		t.Fatalf("an over-long fact points at notes/: %q", e.Message)
	}
}

func TestAnUnparseableFileIsNeverOverwritten(t *testing.T) {
	st, home := newStore(t)
	write(t, home, "facts.toml", "this is [[ not toml")
	_, err := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: "anything at all"}, "common")
	if e := memErr(t, err); !strings.Contains(e.Message, "Refusing to write facts.toml") {
		t.Fatalf("err = %q", e.Message)
	}
	if read(t, home, "facts.toml") != "this is [[ not toml" {
		t.Fatal("the file must be left exactly as it is")
	}
}

func TestUnknownKeysSurviveARewrite(t *testing.T) {
	st, home := newStore(t)
	write(t, home, "facts.toml", "[[fact]]\nid = \"kept\"\ntext = \"A kept fact\"\nsource = \"user\"\ntags = [\"x\", \"y\"]\n")
	if _, err := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: "Something new to remember"}, "common"); err != nil {
		t.Fatal(err)
	}
	if raw := read(t, home, "facts.toml"); !strings.Contains(raw, "tags") || !strings.Contains(raw, `source = "user"`) {
		t.Fatalf("facts.toml = %s; keys the store does not know survive", raw)
	}
}

func TestCheckValidatesWithoutWriting(t *testing.T) {
	st, home := newStore(t)
	plan, err := st.Check(memory.Change{Kind: "rule", Action: "add", Text: "Answer in plain English", Why: "readability"}, "common")
	if err != nil || plan.Summary != "add rule (common)" || !strings.Contains(plan.Detail, "Answer in plain English") || !strings.Contains(plan.Detail, "why: readability") {
		t.Fatalf("Check = %+v, %v", plan, err)
	}
	if _, err := os.Stat(filepath.Join(home, "memories", "rules.toml")); !os.IsNotExist(err) {
		t.Fatal("Check writes nothing")
	}
	if _, err := st.Check(memory.Change{Kind: "rule", Action: "replace", ID: "missing", Text: "x"}, "common"); err == nil {
		t.Fatal("Check catches an unknown id before anyone is asked")
	}
}

func TestConcurrentWritersKeepEveryEntry(t *testing.T) {
	st, home := newStore(t)
	other := memory.New(home) // a second process on the same files, as the Python gateway is
	var wg sync.WaitGroup
	for w, s := range []*memory.Store{st, other} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range 20 {
				if _, err := s.Apply(memory.Change{Kind: "fact", Action: "add", Text: fmt.Sprintf("alpha%d beta%d gamma%d", w*100+i, w*100+i, w*100+i)}, "common"); err != nil {
					t.Error(err)
				}
			}
		}()
	}
	wg.Wait()
	if facts, _ := st.Facts("common"); len(facts) != 40 {
		t.Fatalf("facts = %d, want 40", len(facts))
	}
	if _, err := os.Stat(filepath.Join(home, "memories", "facts.toml.lock")); err != nil {
		t.Fatalf("writes lock the same <file>.lock sidecar as the Python fork: %v", err)
	}
}

// The Python writes `added` as a TOML local date; a rewrite must keep every
// entry's local dates, datetimes and times as they were (review fix: they
// came back as offset datetimes).
func TestARewriteKeepsTOMLDatesAsTheyWere(t *testing.T) {
	st, home := newStore(t)
	write(t, home, "facts.toml", "[[fact]]\nid = \"old\"\ntext = \"An older fact here\"\nsource = \"user\"\nadded = 2026-10-01\nseen = 2026-10-01T08:30:00\nat = 07:15:00\n")
	if _, err := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: "A brand new fact"}, "common"); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Apply(memory.Change{Kind: "fact", Action: "add", Text: "Another separate thing entirely"}, "common"); err != nil {
		t.Fatal(err)
	}
	raw := read(t, home, "facts.toml")
	for _, want := range []string{"added = 2026-10-01\n", "seen = 2026-10-01T08:30:00\n", "at = 07:15:00\n"} {
		if !strings.Contains(raw, want) {
			t.Fatalf("facts.toml lost %q:\n%s", want, raw)
		}
	}
	if strings.Count(raw, "T00:00:00") != 0 {
		t.Fatalf("an added date became a datetime:\n%s", raw)
	}
}
