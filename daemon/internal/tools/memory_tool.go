package tools

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/ewtodd/son-of-anton/daemon/internal/memory"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

// memoryTool is ../tools/memory_tool.py: rules and facts by id.
type memoryTool struct{}

func (memoryTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "memory",
		Description: "Save to memory. A rule (kind='rule') is a standing instruction or core context that must " +
			"hold every turn: rules sit in your system prompt, are capped, and every change needs the " +
			"user's approval. A fact (kind='fact') is knowledge that matters only when its topic comes " +
			"up (hosts, IPs, quirks, procedures, personal details): saved freely, kept out of the " +
			"prompt, and relevant facts arrive with the turn, so there is no read action.\n" +
			"replace and remove address an entry by id; add returns the id. A full rules store refuses: " +
			"ask the user which rule to retire. Skip task progress and anything stale within a week. " +
			"Files: memories/rules.toml, facts.toml, facts.gateway.toml under the instance home.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "kind": {"type": "string", "enum": ["rule", "fact"], "description": "'rule' (standing, approved by the user) or 'fact' (looked up when relevant)."},
    "action": {"type": "string", "enum": ["add", "replace", "remove"]},
    "id": {"type": "string", "description": "Entry id, for replace and remove."},
    "text": {"type": "string", "description": "Rule: an instruction. Fact: a declarative statement. For add and replace."},
    "why": {"type": "string", "description": "Rules only: the reason, optional."},
    "scope": {"type": "string", "enum": ["current", "common", "gateway"], "description": "Omit for this session's own. 'common': every session. 'gateway': personal, never shown in the terminal."}
  },
  "required": ["kind", "action"]
}`),
	}}
}

func memoryFail(msg string, extra map[string]any) (string, error) {
	out := map[string]any{"success": false, "error": msg}
	for k, v := range extra {
		out[k] = v
	}
	return jsonResult(out)
}

func (memoryTool) Run(ctx context.Context, env *Env, raw json.RawMessage) (string, error) {
	if env.Memory == nil {
		return memoryFail("Memory is not available. It may be disabled in config or this environment.", nil)
	}
	var a struct {
		Kind, Action, ID, Text, Why, Scope string
	}
	var in map[string]any
	if err := json.Unmarshal(raw, &in); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	for k, dst := range map[string]*string{"kind": &a.Kind, "action": &a.Action, "id": &a.ID, "text": &a.Text, "why": &a.Why, "scope": &a.Scope} {
		if s, ok := in[k].(string); ok {
			*dst = s
		}
	}
	scope := env.MemoryScope
	if scope == "" {
		scope = "common"
	}
	change := memory.Change{Kind: a.Kind, Action: a.Action, ID: a.ID, Text: a.Text, Why: a.Why, Scope: a.Scope}
	plan, err := env.Memory.Check(change, scope)
	if err != nil {
		return memoryError(err)
	}
	if change.Kind == "rule" || env.MemoryApproval {
		if env.Gate == nil {
			return memoryFail("Nobody can approve a memory change in this run; it was not saved.", nil)
		}
		if v := env.Gate.Memory(ctx, plan.Summary, plan.Detail); !v.Allowed {
			return memoryFail(v.Message, nil)
		}
	}
	res, err := env.Memory.Apply(change, scope)
	if err != nil {
		return memoryError(err)
	}
	out := map[string]any{"id": res.ID, "success": true, "kind": res.Kind, "scope": res.Scope,
		"note": "Saved. This update is complete — do not repeat it."}
	if res.Usage != "" {
		out["usage"] = res.Usage
	}
	return jsonResult(out)
}

func memoryError(err error) (string, error) {
	var me *memory.Error
	if !errors.As(err, &me) {
		return memoryFail(err.Error(), nil)
	}
	extra := map[string]any{}
	if len(me.Similar) > 0 {
		sim := make([]map[string]string, 0, len(me.Similar))
		for _, f := range me.Similar {
			sim = append(sim, map[string]string{"id": f.ID, "text": f.Text})
		}
		extra["similar"] = sim
	}
	if len(me.IDs) > 0 {
		extra["ids"] = me.IDs
	}
	return memoryFail(me.Message, extra)
}
