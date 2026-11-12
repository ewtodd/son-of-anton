package tools_test

// Phase 7c: the execute_code schema follows the registry (phases/07c). The
// vision_analyze tool that once lived here is gone: images reach the model's
// own vision encoder as message parts (images.go, read.go) or the model
// gets an error.

import (
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

func executeCodeDescription(t *testing.T, reg *tools.Registry) string {
	t.Helper()
	tool, ok := reg.Lookup("execute_code")
	if !ok {
		t.Fatal("no execute_code")
	}
	return tool.Schema().Function.Description
}

func TestExecuteCodeDocumentsOnlyTheToolsTheRegistryHolds(t *testing.T) {
	all := executeCodeDescription(t, tools.NewRegistry(tools.Builtin()...))
	few := executeCodeDescription(t, tools.NewRegistry(tools.Builtin()...).Only("Bash", "Read", "execute_code"))
	for _, name := range []string{"web_search(", "web_fetch(", "write(", "edit(", "glob(", "grep(", "bash(", "read("} {
		if !strings.Contains(all, name) {
			t.Errorf("the full registry lacks %s", name)
		}
	}
	if !strings.Contains(few, "bash(") || !strings.Contains(few, "read(") ||
		strings.Contains(few, "web_search(") || strings.Contains(few, "write(") {
		t.Errorf("a registry of Bash and Read documents %q", few)
	}
	if again := executeCodeDescription(t, tools.NewRegistry(tools.Builtin()...)); again != all {
		t.Error("the schema changed between two registries of the same tools")
	}
}

func TestVisionAnalyzeIsGone(t *testing.T) {
	for _, name := range tools.NewRegistry(tools.Builtin()...).Names() {
		if name == "vision_analyze" {
			t.Fatal("vision_analyze is still registered")
		}
	}
}
