package tools

import (
	"context"
	"encoding/json"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

// relayTool is a chat's one way to hand work to son-of-anton workers. The
// daemon answers the actions (Env.Relay); the tool only carries them.
type relayTool struct{}

// RelayTool is the relay tool. It is not in Builtin: only a chat session has it.
func RelayTool() Tool { return relayTool{} }

func (relayTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "relay",
		Description: "This chat is a relay to son-of-anton workers. Project work (code, files, commands in a repository) " +
			"is done by a worker in its own directory, never in this chat: hand it over with this tool and report back.\n\n" +
			"Actions:\n" +
			"- list: this chat's workers, most recent first (optional `workspace` path filter), each marked bound to this chat, " +
			"with its state: running, idle, queued (with when it will run) or picked up (a terminal is driving it).\n" +
			"- send: give `message` to a worker. `session` (id or title) defaults to the one bound to this chat; `new_in` " +
			"(a directory) starts a new worker there instead. A busy worker runs the message after its current turn; `now` " +
			"steers the running turn with it instead.\n" +
			"- status: state, recent activity, queued messages, pending approvals.\n" +
			"- stop: stop the running turn; the worker stays bound.\n" +
			"- detach: unbind the worker from this chat.\n\n" +
			"Relay the user's words faithfully: pass their request as they gave it, without rewriting it or adding " +
			"instructions of your own. Before choosing between continuing an existing worker and starting a new one, ask " +
			"the user. A worker's results and its questions (answer one by sending the user's reply) arrive here as " +
			"notifications; tell the user briefly. Its approval requests also arrive here, and the user answers them with " +
			"/approve or /deny.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "action": {"type": "string", "enum": ["list", "send", "status", "stop", "detach"]},
    "session": {"type": "string", "description": "Worker id or title. Default: the worker bound to this chat."},
    "message": {"type": "string", "description": "send: the user's request for the worker, as they gave it."},
    "new_in": {"type": "string", "description": "send: start a new worker in this directory instead."},
    "now": {"type": "boolean", "description": "send: steer the running turn instead of queueing; only when the user asks for it now."},
    "workspace": {"type": "string", "description": "list: only workers in this directory or below it."}
  },
  "required": ["action"]
}`),
	}}
}

func (relayTool) Run(ctx context.Context, env *Env, raw json.RawMessage) (string, error) {
	if env.Relay == nil {
		return jsonResult(map[string]any{"success": false, "error": "relay is not available here"})
	}
	return env.Relay(ctx, raw), nil
}
