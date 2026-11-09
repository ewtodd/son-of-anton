package main

import (
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

// --reasoning sets the request's reasoning effort, as son-of-anton -z/-q
// --reasoning did (phase 2b maps those flags onto soa-d).
func TestReasoningFlagSetsTheEffort(t *testing.T) {
	for _, cmd := range []string{"oneshot", "query"} {
		fake := fakellm.New(t, fakellm.Turn{Content: "ok"})
		home := testHome(t, fake)
		if code, _, errOut := runIn(t, cmd, "--home", home, "--reasoning", "low", "go"); code != 0 {
			t.Fatalf("%s: exit %d: %s", cmd, code, errOut)
		}
		if got := fake.Request(0)["reasoning_effort"]; got != "low" {
			t.Fatalf("%s --reasoning low: reasoning_effort = %v", cmd, got)
		}
	}
}
