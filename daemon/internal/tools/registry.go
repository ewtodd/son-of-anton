// Package tools implements the daemon's builtin tools: terminal,
// process, read_file, write_file, patch, search_files, todo and clarify. Results are JSON objects;
// a tool reports failures as {"error": "..."} and reserves returned errors
// for infrastructure failures the loop should surface.
package tools

import (
	"context"
	"encoding/json"

	"github.com/ewtodd/son-of-anton/daemon/internal/approval"
	"github.com/ewtodd/son-of-anton/daemon/internal/paths"
	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
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
