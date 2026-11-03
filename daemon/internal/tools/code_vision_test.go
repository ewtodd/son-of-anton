package tools_test

// Phase 7c: the schemas follow the registry, and vision_analyze refuses a
// private address (daemon/phases/07c-code-vision.md).

import (
	"net/http"
	"net/http/httptest"
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
	few := executeCodeDescription(t, tools.NewRegistry(tools.Builtin()...).Only("terminal", "read_file", "execute_code"))
	for _, name := range []string{"web_search(", "write_file(", "patch(", "search_files(", "terminal(", "read_file("} {
		if !strings.Contains(all, name) {
			t.Errorf("the full registry lacks %s", name)
		}
	}
	if !strings.Contains(few, "terminal(") || !strings.Contains(few, "read_file(") ||
		strings.Contains(few, "web_search(") || strings.Contains(few, "write_file(") {
		t.Errorf("a registry of terminal and read_file documents %q", few)
	}
	if again := executeCodeDescription(t, tools.NewRegistry(tools.Builtin()...)); again != all {
		t.Error("the schema changed between two registries of the same tools")
	}
}

func TestVisionAnalyzeRefusesPrivateAddresses(t *testing.T) {
	hit := false
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { hit = true }))
	defer srv.Close()
	f := setup(t, nil)
	f.env.Vision = &tools.Vision{BaseURL: "http://127.0.0.1:1", Model: "m"}
	res := f.call(t, "vision_analyze", map[string]any{"image_url": srv.URL + "/x.png", "question": "q"})
	if res["success"] != false || !strings.Contains(res["error"].(string), "private") || hit {
		t.Fatalf("result = %v, server hit = %v", res, hit)
	}
}
