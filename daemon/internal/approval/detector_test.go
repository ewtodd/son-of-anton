package approval_test

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
)

// A corpus row marked "stricter: ..." records a verdict that is deliberately
// more cautious than the reference's. The main corpus test skips every
// intentional row; this one holds the stricter ones to the verdict they
// record, so they cannot drift back.
func TestStricterRowsKeepTheirVerdict(t *testing.T) {
	raw, err := os.ReadFile("testdata/python_verdicts.jsonl")
	if err != nil {
		t.Fatal(err)
	}
	n := 0
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		var r pythonVerdict
		if err := json.Unmarshal([]byte(line), &r); err != nil {
			t.Fatal(err)
		}
		if !strings.HasPrefix(r.Intentional, "stricter:") {
			continue
		}
		n++
		fs := approval.Classify(r.Cmd)
		if (len(fs) > 0) != r.Dangerous || hardline(fs) != r.Hardline {
			t.Errorf("%q: got dangerous=%v hardline=%v, row records dangerous=%v hardline=%v",
				r.Cmd, len(fs) > 0, hardline(fs), r.Dangerous, r.Hardline)
		}
		if !r.Dangerous {
			t.Errorf("%q: a stricter row cannot record a safe verdict", r.Cmd)
		}
	}
	if n == 0 {
		t.Fatal("no stricter rows found")
	}
}

// Large or knotted commands are answered quickly: either classified or
// refused as over the parser limit, never left to run for seconds.
func TestPathologicalCommandsAreBounded(t *testing.T) {
	for _, unit := range []string{
		"xargs rm ; ", "curl x | ", "find . -exec rm ; ", "pkill gateway ; ", "tee a ; ",
		"dd if=a ; ", "gateway run & ", "base64 -d | ", "rm -r a ", "$(curl x) ", "( ( ( ", "\" ' ",
	} {
		cmd := strings.Repeat(unit, 120_000/len(unit))
		start := time.Now()
		approval.Classify(cmd)
		if d := time.Since(start); d > 3*time.Second {
			t.Errorf("%q repeated: %v", unit, d)
		}
	}
}
