package main

// Phase 7c: -z has nobody to ask, so execute_code is refused unless --yolo
// (daemon/phases/07c-code-vision.md).

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

func codeResult(t *testing.T, fake *fakellm.Server) map[string]any {
	t.Helper()
	for _, raw := range fake.Messages(1) {
		var m struct{ Role, Content string }
		if json.Unmarshal(raw, &m) == nil && m.Role == "tool" {
			var r map[string]any
			if err := json.Unmarshal([]byte(m.Content), &r); err != nil {
				t.Fatalf("tool result: %s", m.Content)
			}
			return r
		}
	}
	t.Fatal("no tool result in the second request")
	return nil
}

func TestOneshotRefusesExecuteCodeUnlessYolo(t *testing.T) {
	turns := func() []fakellm.Turn {
		return []fakellm.Turn{
			{ToolCalls: []fakellm.ToolCall{{ID: "c1", Name: "execute_code", Arguments: `{"code":"print(6 * 7)"}`}}},
			{Content: "done"},
		}
	}
	fake := fakellm.New(t, turns()...)
	home := testHome(t, fake, "")
	if code, _, errOut := runIn(t, "oneshot", "--home", home, "compute"); code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	r := codeResult(t, fake)
	if r["status"] != "error" || r["tool_calls_made"] != float64(0) || strings.Contains(fmt.Sprint(r["output"]), "42") {
		t.Fatalf("without --yolo = %v", r)
	}

	py := os.Getenv("SON_OF_ANTON_PYTHON")
	if py == "" {
		py, _ = exec.LookPath("python3")
	}
	if py == "" {
		t.Skip("no Python 3 for the --yolo half")
	}
	t.Setenv("SON_OF_ANTON_PYTHON", py)
	fake = fakellm.New(t, turns()...)
	home = testHome(t, fake, "")
	if code, _, errOut := runIn(t, "oneshot", "--home", home, "--yolo", "compute"); code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	if r := codeResult(t, fake); r["status"] != "success" || !strings.Contains(fmt.Sprint(r["output"]), "42") {
		t.Fatalf("with --yolo = %v", r)
	}
}
