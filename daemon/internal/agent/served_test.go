package agent_test

import (
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// Shell commands see the model serving the step as $SOA_MODEL (a commit's
// Model: line): the served model the endpoint reported, else the session's.
func TestTheServedModelReachesShellCommands(t *testing.T) {
	st := newStore(t)
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c1", "Bash", `{"command":"echo model=$SOA_MODEL"}`)}},
		fakellm.Turn{Content: "ok"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c2", "Bash", `{"command":"echo model=$SOA_MODEL"}`)}, ServedModel: "radiance/qwen3.8-27b"},
		fakellm.Turn{Content: "ok", ServedModel: "radiance/qwen3.8-27b"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("c3", "Bash", `{"command":"echo model=$SOA_MODEL"}`)}},
		fakellm.Turn{Content: "ok"},
	)
	s, _ := toolRunner(t, st, fake, tools.Builtin()...).NewSession(ctx, "interactive", t.TempDir())
	defer s.Close()
	output := func() string {
		rows, _ := st.Messages(ctx, s.ID())
		for i := len(rows) - 1; i >= 0; i-- {
			if rows[i].Role == "tool" {
				return rows[i].Content
			}
		}
		return ""
	}
	send(t, s, "which model")
	if !strings.Contains(output(), "model=m") || s.ServedModel() != "m" {
		t.Fatalf("with nothing reported: %q, ServedModel %q; the session's model", output(), s.ServedModel())
	}
	send(t, s, "again")
	if !strings.Contains(output(), "model=radiance/qwen3.8-27b") || s.ServedModel() != "radiance/qwen3.8-27b" {
		t.Fatalf("after maitred named it: %q, ServedModel %q", output(), s.ServedModel())
	}
	s.SetModel(provider.New(fake.URL, "k"), "m2", "")
	if s.ServedModel() != "m2" {
		t.Fatalf("after /model the old served model is forgotten: %q", s.ServedModel())
	}
	send(t, s, "and now")
	if !strings.Contains(output(), "model=m2") {
		t.Fatalf("$SOA_MODEL never comes back stale from the shell snapshot: %q", output())
	}
}
