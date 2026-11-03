package signal_test

// Phase 5a: Format is the Python markdown_to_signal exactly. The cases were
// produced by running gateway/platforms/signal_format.py over the inputs.

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/signal"
)

func TestFormatMatchesThePython(t *testing.T) {
	data, err := os.ReadFile("testdata/format_cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		In     string   `json:"in"`
		Text   string   `json:"text"`
		Styles []string `json:"styles"`
	}
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	if len(cases) < 20 {
		t.Fatalf("only %d cases", len(cases))
	}
	for _, c := range cases {
		text, styles := signal.Format(c.In)
		if text != c.Text || strings.Join(styles, ",") != strings.Join(c.Styles, ",") {
			t.Errorf("Format(%q)\n got  %q %v\n want %q %v", c.In, text, styles, c.Text, c.Styles)
		}
	}
}
