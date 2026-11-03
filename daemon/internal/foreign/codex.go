package foreign

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

// ListCodex lists the Codex CLI rollout files under root
// (root/**/rollout-*.jsonl), sorted. A root that is not a directory has none.
func ListCodex(root string) ([]string, error) {
	if fi, err := os.Stat(root); err != nil || !fi.IsDir() {
		return nil, nil
	}
	var paths []string
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if n := d.Name(); !d.IsDir() && strings.HasPrefix(n, "rollout-") && strings.HasSuffix(n, ".jsonl") {
			paths = append(paths, p)
		}
		return nil
	})
	sortPaths(paths)
	return paths, err
}

// ParseCodex parses one Codex CLI rollout file.
func ParseCodex(path string) (Session, error) {
	if _, err := os.Stat(path); err != nil {
		return Session{}, err
	}
	var (
		turns []rawTurn
		s     Session
		u     Usage
	)
	for _, obj := range readJSONLines(path) {
		payload, ok := dict(obj["payload"])
		if !ok {
			continue
		}
		switch obj["type"] {
		case "session_meta":
			if c, ok := str(payload, "cwd"); ok {
				s.Cwd = c
			}
			sid := payload["session_id"]
			if !truthy(sid) {
				sid = payload["id"]
			}
			if id, ok := sid.(string); ok {
				s.ID = id
			}
		case "event_msg":
			// Usage comes out of band: each token_count event carries the
			// cumulative totals and the last call's. Its input_tokens is the
			// whole prompt, of which the cached part is a subset.
			info, ok := dict(payload["info"])
			if payload["type"] != "token_count" || !ok {
				continue
			}
			if total, ok := dict(info["total_token_usage"]); ok {
				in, cached := intOr0(total["input_tokens"]), intOr0(total["cached_input_tokens"])
				u.Input, u.CacheRead = max(0, in-cached), cached
				u.CacheWrite = intOr0(total["cache_write_input_tokens"])
				u.Output = intOr0(total["output_tokens"])
				u.Reasoning = intOr0(total["reasoning_output_tokens"])
			}
			if last, ok := dict(info["last_token_usage"]); ok {
				if ctx := intOr0(last["input_tokens"]); ctx > 0 {
					u.LastPrompt = ctx
				}
			}
			u.Calls++
		case "response_item":
			switch payload["type"] {
			case "message":
				role, _ := payload["role"].(string)
				if role != "user" && role != "assistant" {
					continue // developer and system payloads are never imported
				}
				text := flattenBlocks(payload["content"])
				if text == "" || (role == "user" && isWrapperText(text)) {
					continue
				}
				turns = append(turns, rawTurn{role, text, coerceTimestamp(obj["timestamp"])})
			case "custom_tool_call", "function_call", "local_shell_call":
				name := payload["name"]
				if !truthy(name) {
					name = payload["tool"]
				}
				turns = append(turns, rawTurn{"assistant", "[ran tool: " + orName(name, "tool") + "]", coerceTimestamp(obj["timestamp"])})
			}
		}
	}
	s.Turns = mergeTurns(turns)
	s.Title = firstUserLine(turns)
	s.Usage = usageOf(u)
	return s, nil
}
