package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

// -q is a full-memory run and recalls; -z (oneshot) is lean and never does.
func TestQueryRecallsAndOneshotDoesNot(t *testing.T) {
	for _, c := range []struct {
		cmd    string
		recall bool
	}{{"query", true}, {"oneshot", false}} {
		fake := fakellm.New(t, fakellm.Turn{Content: "ok"})
		home := testHome(t, fake, "")
		cfg, _ := os.ReadFile(filepath.Join(home, "config.toml"))
		cfg = append(cfg, []byte("\n[memory.rag]\nenabled = true\nbase_url = \""+fake.URL+"\"\nmodel = \"embed\"\nmin_score = 0.4\n")...)
		if err := os.WriteFile(filepath.Join(home, "config.toml"), cfg, 0o600); err != nil {
			t.Fatal(err)
		}
		facts := filepath.Join(home, "memories", "facts.toml")
		_ = os.MkdirAll(filepath.Dir(facts), 0o700)
		if err := os.WriteFile(facts, []byte("[[fact]]\nid = \"nas-address\"\ntext = \"NAS address 10.0.0.3 home network\"\nsource = \"agent\"\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		if code, _, errOut := runIn(t, c.cmd, "--home", home, "NAS network address"); code != 0 {
			t.Fatalf("%s: exit %d: %s", c.cmd, code, errOut)
		}
		msgs := fake.Messages(0)
		got := strings.Contains(string(msgs[len(msgs)-1]), "recalled-notes")
		if got != c.recall {
			t.Fatalf("%s: recalled = %v, want %v (%s)", c.cmd, got, c.recall, msgs[len(msgs)-1])
		}
	}
}
