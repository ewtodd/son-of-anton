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
			"A worker is any session you can drive: a terminal session is one, whether a terminal has it open or not. So on a " +
			"single-user instance you can drive the user's own sessions as well as the ones you start. A send continues that session " +
			"itself, with its own history, model and directory. When the user says to pick up, resume or continue a session, send to " +
			"it; do not start a new worker for it. " +
			"On a single-user instance a send takes the session from a terminal that has it open (state picked up), even mid-turn, and " +
			"that terminal closes, so send to one only when the user asked for that session; the result then says took_from_terminal.\n\n" +
			"Actions:\n" +
			"- list: the sessions you can drive, most recent first (optional `workspace` path filter), each with its cwd, whether it is " +
			"bound to this chat, and its state: running, idle, queued (with when it will run) or picked up (a terminal is driving it).\n" +
			"- send: give `message` to a worker. `session` (id or title) defaults to the one bound to this chat; `new_in` " +
			"(a directory) starts a new worker there instead, and is refused while a session is running in that repository: send to " +
			"that session. A busy worker runs the message after its current turn; `now` steers the running turn with it instead.\n" +
			"- status: state, recent activity, queued messages, pending approvals. Do not poll it: a worker's results arrive as notifications.\n" +
			"- stop: stop the running turn; the worker stays bound.\n" +
			"- detach: unbind the worker from this chat. On a single-user instance it then stops reporting here.\n" +
			"- report: send the user a PDF of a worker's session (its requests, replies, tool calls and subagents) as an " +
			"attachment in this chat. Use it when the user asks for a report or a write-up of what a worker did; the file " +
			"arrives on its own, so just tell them it is there.\n\n" +
			"Relay the user's words faithfully: pass their request as they gave it, without rewriting it; anything you " +
			"add from your memory or notes goes after it, marked as context from you. Before choosing between continuing an existing worker and starting a new one, ask " +
			"the user. A worker's results and its questions (answer one by sending the user's reply) arrive here as " +
			"notifications; tell the user briefly. Its approval requests also arrive here, and the user answers them with " +
			"/approve or /deny. " +
			"A send can also choose the worker's model: pick from the latest [Models] note by what the task needs, and tell the user which you chose.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "action": {"type": "string", "enum": ["list", "send", "status", "stop", "detach", "report"]},
    "session": {"type": "string", "description": "Worker id or title. Default: the worker bound to this chat."},
    "message": {"type": "string", "description": "send: the user's request for the worker, as they gave it."},
    "new_in": {"type": "string", "description": "send: start a new worker in this directory instead."},
    "now": {"type": "boolean", "description": "send: steer the running turn instead of queueing; only when the user asks for it now."},
    "workspace": {"type": "string", "description": "list: only workers in this directory or below it."},
    "model": {"type": "string", "description": "send: the model this worker runs on, from its next turn: one of the names in the latest [Models] note, or \"default\" for the default model the [Models] note names. Leave it out to keep the worker's model."}
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
