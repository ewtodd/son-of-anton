package tools

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
)

// runScript runs code through execute_code with no tools behind it.
func runScript(t *testing.T, ctx context.Context, code string) map[string]any {
	t.Helper()
	st, err := NewState(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = st.Close() }()
	env := &Env{Gate: approval.NewGate(approval.Default, nil, nil), State: st,
		Call: func(context.Context, string, json.RawMessage) string { return "{}" }}
	args, _ := json.Marshal(map[string]string{"code": code})
	out, err := executeCodeTool{}.Run(ctx, env, args)
	if err != nil {
		t.Fatal(err)
	}
	var res map[string]any
	if err := json.Unmarshal([]byte(out), &res); err != nil {
		t.Fatalf("result: %s", out)
	}
	return res
}
