package approval_test

import (
	"context"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
)

// A script runs unasked in default and yolo; ask and lockdown put the script
// to the driver, and with nobody to ask it does not run.
func TestScriptGate(t *testing.T) {
	var asked []approval.Request
	approver := func(_ context.Context, req approval.Request) (approval.Choice, error) {
		asked = append(asked, req)
		return approval.Once, nil
	}
	for _, mode := range []approval.Mode{approval.Default, approval.Yolo} {
		if v := approval.NewGate(mode, approver, nil).Script(ctx, "print(1)"); !v.Allowed || len(asked) != 0 {
			t.Errorf("%s: verdict %+v, asked %d", mode, v, len(asked))
		}
	}
	for _, mode := range []approval.Mode{approval.Ask, approval.Lockdown} {
		asked = nil
		if v := approval.NewGate(mode, approver, nil).Script(ctx, "print(1)"); !v.Allowed || len(asked) != 1 || asked[0].Subject != "print(1)" || asked[0].AllowRemember {
			t.Errorf("%s: verdict %+v, asked %+v", mode, v, asked)
		}
		if v := approval.NewGate(mode, nil, nil).Script(ctx, "print(1)"); v.Allowed || v.Message == "" {
			t.Errorf("%s with nobody to ask: %+v", mode, v)
		}
	}
}
