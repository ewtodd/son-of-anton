package tools_test

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/tools"
)

// Every tool the registry can offer says what it does, and every parameter
// in its schema says what it takes.
func TestEveryToolAndParameterIsDescribed(t *testing.T) {
	all := append(tools.Builtin(), tools.CronJob(t.TempDir()), tools.RelayTool())
	all = append(all, tools.MCPTools([]string{"srv"})...)
	for _, tool := range all {
		if n := tool.Schema().Function.Name; n == "memory" || n == "relay" {
			continue // descriptions this project wrote earlier, kept as they are
		}
		s := tool.Schema()
		if strings.TrimSpace(s.Function.Description) == "" {
			t.Errorf("%s has no description", s.Function.Name)
		}
		var params struct {
			Properties map[string]json.RawMessage `json:"properties"`
		}
		if err := json.Unmarshal(s.Function.Parameters, &params); err != nil {
			t.Errorf("%s: parameters: %v", s.Function.Name, err)
			continue
		}
		for name, raw := range params.Properties {
			var p struct {
				Description string `json:"description"`
			}
			if json.Unmarshal(raw, &p) != nil || strings.TrimSpace(p.Description) == "" {
				t.Errorf("%s: parameter %s has no description", s.Function.Name, name)
			}
		}
	}
}
