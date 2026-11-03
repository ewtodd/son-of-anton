// Package tools implements the daemon's builtin tools: terminal,
// process, read_file, write_file, patch, search_files, todo and clarify. Results are JSON objects;
// a tool reports failures as {"error": "..."} and reserves returned errors
// for infrastructure failures the loop should surface.
package tools

import (
	"context"
	"encoding/json"

	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/mcp"
	"github.com/ewtodd/son-of-anton/daemon/internal/memory"
	"github.com/ewtodd/son-of-anton/daemon/internal/paths"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

// Env carries what a tool may touch: the path policy, the approval gate and
// the session state.
type Env struct {
	Policy paths.Policy
	Gate   *approval.Gate
	State  *State
	// Ask answers clarify questions; nil means nobody can answer.
	Ask Questioner
	// AsyncDelivery says whether a background completion notice can ever be
	// delivered in this run.
	AsyncDelivery bool
	// Model is the model serving this step; shell commands see it as
	// $SOA_MODEL (for a commit's Model: line).
	Model string
	// Memory is the rule and fact store (nil: the memory tool is unavailable);
	// MemoryScope is the calling session's scope and MemoryApproval is
	// memory.write_approval.
	Memory         *memory.Store
	MemoryScope    string
	MemoryApproval bool
	// Store and SessionID back session_search (nil Store: unavailable);
	// SearchMessages adds meaning-based matches (nil: recall is off). The
	// caller's scope is MemoryScope.
	Store          *store.Store
	SessionID      string
	SearchMessages MessageSearch
	// WallChat walls a gateway caller to one chat's sessions and the common
	// ones; "" lets it read every session.
	WallChat string
	// Web configures web_search and web_extract (nil: they say they are not
	// configured).
	Web *Web
	// MCP reaches the MCP servers (nil: none is configured).
	MCP *mcp.Manager
	// CallID is the id of the tool call being run.
	CallID string
	// Call runs one of the session's tools as a direct call would, through the
	// same registry, environment and gates; execute_code's scripts use it.
	Call func(ctx context.Context, name string, args json.RawMessage) string
	// NoScripts, when set, is why execute_code refuses to run (a run with
	// nobody to ask).
	NoScripts string
	// Vision is the endpoint vision_analyze sends images to (nil: none).
	Vision *Vision
}

// Tool is one builtin tool.
type Tool interface {
	Schema() provider.Tool
	Run(ctx context.Context, env *Env, args json.RawMessage) (string, error)
}

// Registry is the fixed, ordered tool list for a session.
type Registry struct {
	tools map[string]Tool
	order []string
}

// adaptable is a tool whose schema depends on the tools its registry holds.
type adaptable interface{ adapt(names []string) Tool }

// NewRegistry builds a registry in the given order; later duplicates of a
// name are ignored.
func NewRegistry(ts ...Tool) *Registry {
	r := &Registry{tools: map[string]Tool{}}
	for _, t := range ts {
		name := t.Schema().Function.Name
		if _, exists := r.tools[name]; exists {
			continue
		}
		r.tools[name] = t
		r.order = append(r.order, name)
	}
	for name, t := range r.tools {
		if a, ok := t.(adaptable); ok {
			r.tools[name] = a.adapt(r.order)
		}
	}
	return r
}

// Schemas returns the tool schemas in registration order. The bytes are
// stable across calls, so the request prefix stays cache-friendly.
func (r *Registry) Schemas() []provider.Tool {
	out := make([]provider.Tool, 0, len(r.order))
	for _, name := range r.order {
		out = append(out, r.tools[name].Schema())
	}
	return out
}

// Lookup finds a tool by name.
func (r *Registry) Lookup(name string) (Tool, bool) {
	t, ok := r.tools[name]
	return t, ok
}

// Concurrent is implemented by a tool that may run beside its sibling calls
// of one assistant message.
type Concurrent interface{ Concurrent() bool }

// Names lists the registered tool names in order.
func (r *Registry) Names() []string { return append([]string(nil), r.order...) }

// Only is a registry of the named tools (those registered), in the given order.
func (r *Registry) Only(names ...string) *Registry {
	var ts []Tool
	for _, n := range names {
		if t, ok := r.tools[n]; ok {
			ts = append(ts, t)
		}
	}
	return NewRegistry(ts...)
}

// Builtin is the builtin tool set, in canonical order.
func Builtin() []Tool {
	return []Tool{
		terminalTool{},
		processTool{},
		readFileTool{},
		writeFileTool{},
		patchTool{},
		searchFilesTool{},
		todoTool{},
		clarifyTool{},
		memoryTool{},
		sessionSearchTool{},
		webSearchTool{},
		webExtractTool{},
		executeCodeTool{},
		visionTool{},
	}
}

// jsonResult marshals a map to the tool's JSON result.
func jsonResult(v any) (string, error) {
	b, err := json.Marshal(v)
	return string(b), err
}

func errorResult(msg string) string {
	return `{"error":` + mustQuote(msg) + `}`
}

func mustQuote(s string) string {
	b, _ := json.Marshal(s)
	return string(b)
}
