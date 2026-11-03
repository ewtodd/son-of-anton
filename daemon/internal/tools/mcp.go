package tools

import (
	"context"
	"encoding/json"
	"strings"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

const (
	defaultMCPSearch = 5
	maxMCPSearch     = 20
	mcpNotConfigured = "no MCP servers are configured"
)

// MCPTools are the three fixed tools that reach the MCP servers' tools:
// tool_search, tool_describe and tool_call. servers (sorted) are named in
// tool_search's description, so the list is the same whichever servers are up.
func MCPTools(servers []string) []Tool {
	return []Tool{mcpSearchTool{servers}, mcpDescribeTool{}, mcpCallTool{}}
}

type mcpSearchTool struct{ servers []string }

func (mcpSearchTool) Concurrent() bool { return true }

func (t mcpSearchTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "tool_search",
		Description: "Search the tools of this instance's MCP servers (" + strings.Join(t.servers, ", ") + "). " +
			"Returns up to `limit` matching tools with their names and descriptions. Call tool_describe for a tool's parameters, then tool_call to run it.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "query": {"type": "string", "description": "What the tool should do, in a few words."},
    "limit": {"type": "integer", "description": "Maximum number of matches to return. Defaults to 5.", "minimum": 1, "maximum": 20, "default": 5}
  },
  "required": ["query"]
}`),
	}}
}

func (mcpSearchTool) Run(ctx context.Context, env *Env, args json.RawMessage) (string, error) {
	if env.MCP == nil {
		return errorResult(mcpNotConfigured), nil
	}
	var in map[string]any
	if err := json.Unmarshal(args, &in); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	query := argStr(in, "query")
	if query == "" {
		return errorResult("query is required"), nil
	}
	limit := defaultMCPSearch
	if n, ok := argInt(in, "limit"); ok {
		limit = int(min(max(n, 1), maxMCPSearch))
	}
	matches, total, down := env.MCP.Search(ctx, query, limit)
	list := make([]map[string]any, 0, len(matches))
	for _, m := range matches {
		list = append(list, map[string]any{"name": m.Name, "server": m.Server, "description": m.Description})
	}
	out := map[string]any{"query": query, "total_available": total, "matches": list}
	if len(matches) == 0 && len(down) > 0 {
		un := make([]map[string]any, len(down))
		for i, d := range down {
			un[i] = map[string]any{"server": d.Server, "error": d.Error}
		}
		out["unavailable"] = un
	}
	return jsonOut(out), nil
}

type mcpDescribeTool struct{}

func (mcpDescribeTool) Concurrent() bool { return true }

func (mcpDescribeTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name:        "tool_describe",
		Description: "Show an MCP tool's description and parameter schema, by the name tool_search gave.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "name": {"type": "string", "description": "The tool's name, as tool_search gave it."}
  },
  "required": ["name"]
}`),
	}}
}

func (mcpDescribeTool) Run(ctx context.Context, env *Env, args json.RawMessage) (string, error) {
	if env.MCP == nil {
		return errorResult(mcpNotConfigured), nil
	}
	var in map[string]any
	if err := json.Unmarshal(args, &in); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	name := argStr(in, "name")
	if name == "" {
		return errorResult("name is required"), nil
	}
	d, ok := env.MCP.Describe(ctx, name)
	if !ok {
		if why := env.MCP.Unavailable(name); why != "" {
			return errorResult(why), nil
		}
		return errorResult(noMCPTool(name)), nil
	}
	return jsonOut(map[string]any{"name": d.Name, "server": d.Server, "description": d.Description, "parameters": d.Parameters}), nil
}

type mcpCallTool struct{}

func (mcpCallTool) Concurrent() bool { return true }

func (mcpCallTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name:        "tool_call",
		Description: "Run an MCP tool found with tool_search, with arguments that match the schema tool_describe showed.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "name": {"type": "string", "description": "The tool's name, as tool_search gave it."},
    "arguments": {"type": "object", "description": "The tool's arguments, as its schema describes them.", "additionalProperties": true}
  },
  "required": ["name"]
}`),
	}}
}

func (mcpCallTool) Run(ctx context.Context, env *Env, args json.RawMessage) (string, error) {
	if env.MCP == nil {
		return errorResult(mcpNotConfigured), nil
	}
	var in map[string]any
	if err := json.Unmarshal(args, &in); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	name := argStr(in, "name")
	if name == "" {
		return errorResult("name is required"), nil
	}
	callArgs := map[string]any{}
	switch a := in["arguments"].(type) {
	case nil:
	case map[string]any:
		callArgs = a
	case string:
		if strings.TrimSpace(a) != "" {
			if err := json.Unmarshal([]byte(a), &callArgs); err != nil || callArgs == nil {
				return errorResult("arguments is not a JSON object"), nil
			}
		}
	default:
		return errorResult("arguments must be an object"), nil
	}
	result, found, reached := env.MCP.Call(ctx, name, callArgs, env.CallID)
	if !found {
		if why := env.MCP.Unavailable(name); why != "" {
			return errorResult(why), nil
		}
		return errorResult(noMCPTool(name)), nil
	}
	if !reached {
		return result, nil
	}
	// The result is wrapped here, under the MCP tool's name: the loop sees
	// only tool_call.
	return wrapUntrusted(name, result), nil
}

func noMCPTool(name string) string {
	return "no MCP tool " + name + "; use tool_search to find one"
}
