package foreign

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	_ "modernc.org/sqlite"
)

// openOpencode opens opencode's database read-only, so a live writer is
// unaffected and nothing here can write to it.
func openOpencode(path string) (*sql.DB, error) {
	if _, err := os.Stat(path); err != nil {
		return nil, fmt.Errorf("opencode database not found: %s", path)
	}
	q := url.Values{"mode": {"ro"}, "_pragma": {"query_only(1)", "busy_timeout(10000)"}}
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	return sql.Open("sqlite", (&url.URL{Scheme: "file", Path: abs, RawQuery: q.Encode()}).String())
}

// ListOpencode lists the ids of the root sessions (no parent: the others are
// subagent transcripts), newest first.
func ListOpencode(db string) ([]string, error) {
	conn, err := openOpencode(db)
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	rows, err := conn.Query(`SELECT id FROM session WHERE parent_id IS NULL ORDER BY time_created DESC`)
	if err != nil {
		if strings.Contains(err.Error(), "no such table") {
			return nil, nil
		}
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

type ocMessage struct {
	id, data string
	created  any
}

// ocString is a text column as a string, "" for NULL.
func ocString(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case []byte:
		return string(x)
	}
	return ""
}

// ocContextTokens is the context fill of one assistant message: its input
// plus what the cache served or took. Output is the reply, not the context.
func ocContextTokens(tokens any) int {
	t, ok := dict(tokens)
	if !ok {
		return 0
	}
	total := intOr0(t["input"])
	if cache, ok := dict(t["cache"]); ok {
		total += intOr0(cache["read"]) + intOr0(cache["write"])
	}
	return total
}

type ocPart struct{ id, data string }

// ocPartText flattens one message's parts to text: text and attachments, and
// a line for tool activity. Reasoning, step boundaries, patches and snapshots
// carry nothing the transcript needs.
func ocPartText(role string, parts []ocPart) string {
	var segments []string
	compacted := false
	for _, p := range parts {
		var data map[string]any
		if json.Unmarshal([]byte(p.data), &data) != nil || data == nil {
			continue
		}
		switch data["type"] {
		case "text":
			if s, ok := str(data, "text"); ok && strip(s) != "" {
				segments = append(segments, strip(s))
			}
		case "file":
			name := data["filename"]
			if !truthy(name) {
				name = data["url"]
			}
			if !truthy(name) {
				name = "file"
			}
			if s, ok := name.(string); ok && s != "" {
				segments = append(segments, "[attached file: "+s+"]")
			}
		case "tool":
			if role != "assistant" {
				continue
			}
			tool := orName(data["tool"], "tool")
			if state, ok := dict(data["state"]); ok && state["status"] == "error" {
				segments = append(segments, "[tool failed: "+tool+"]")
			} else {
				segments = append(segments, "[ran tool: "+tool+"]")
			}
		case "compaction":
			compacted = true
		}
	}
	if compacted && len(segments) == 0 {
		if role == "user" {
			return "[conversation compacted]"
		}
		return ""
	}
	return strings.Join(segments, "\n\n")
}

// ocRevertBounds is how many messages a pending session revert keeps, and the
// part id to cut the last of them at. opencode keeps reverted messages on
// disk until the next prompt; what the user sees is everything before the
// reverted message, or before the reverted part inside it.
func ocRevertBounds(messages []ocMessage, revert any) (int, string) {
	r, ok := dict(revert)
	if !ok {
		return len(messages), ""
	}
	id, _ := str(r, "messageID")
	if id == "" {
		return len(messages), ""
	}
	for i, m := range messages {
		if m.id == id {
			if part, _ := str(r, "partID"); part != "" {
				return i + 1, part
			}
			return i, ""
		}
	}
	return len(messages), ""
}

// ParseOpencode parses one session of opencode's database.
func ParseOpencode(db, id string) (Session, error) {
	conn, err := openOpencode(db)
	if err != nil {
		return Session{}, err
	}
	defer conn.Close()

	var directory, title, revert any
	err = conn.QueryRow(`SELECT directory, title, revert FROM session WHERE id = ?`, id).Scan(&directory, &title, &revert)
	if err == sql.ErrNoRows {
		return Session{}, fmt.Errorf("opencode session not found in %s: %s", db, id)
	}
	if err != nil {
		return Session{}, err
	}
	var u Usage
	var tIn, tOut, tReasoning, tRead, tWrite any
	// A store whose schema predates the totals simply carries none.
	if conn.QueryRow(`SELECT tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write FROM session WHERE id = ?`, id).
		Scan(&tIn, &tOut, &tReasoning, &tRead, &tWrite) == nil {
		u = Usage{Input: intOr0(tIn), Output: intOr0(tOut), Reasoning: intOr0(tReasoning), CacheRead: intOr0(tRead), CacheWrite: intOr0(tWrite)}
	}

	rows, err := conn.Query(`SELECT id, data, time_created FROM message WHERE session_id = ? ORDER BY time_created, id`, id)
	if err != nil {
		return Session{}, err
	}
	var messages []ocMessage
	for rows.Next() {
		var m ocMessage
		var data any
		if err := rows.Scan(&m.id, &data, &m.created); err != nil {
			rows.Close()
			return Session{}, err
		}
		m.data = ocString(data)
		messages = append(messages, m)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return Session{}, err
	}

	var rev any
	if r := ocString(revert); r != "" {
		if json.Unmarshal([]byte(r), &rev) != nil {
			rev = nil
		}
	}
	keep, cut := ocRevertBounds(messages, rev)

	var turns []rawTurn
	for pos, m := range messages[:keep] {
		var data map[string]any
		if json.Unmarshal([]byte(m.data), &data) != nil || data == nil {
			continue
		}
		role, _ := data["role"].(string)
		if role != "user" && role != "assistant" {
			continue
		}
		if role == "assistant" {
			// Each assistant message carries its call's usage; the last one's
			// prompt size is the context a resume restores.
			u.Calls++
			if ctx := ocContextTokens(data["tokens"]); ctx != 0 {
				u.LastPrompt = ctx
			}
		}
		prows, err := conn.Query(`SELECT id, data FROM part WHERE message_id = ? ORDER BY id`, m.id)
		if err != nil {
			return Session{}, err
		}
		var parts []ocPart
		for prows.Next() {
			var p ocPart
			var pd any
			if err := prows.Scan(&p.id, &pd); err != nil {
				prows.Close()
				return Session{}, err
			}
			p.data = ocString(pd)
			if cut == "" || pos != keep-1 || p.id < cut {
				parts = append(parts, p)
			}
		}
		prows.Close()
		text := ocPartText(role, parts)
		if text == "" || (role == "user" && isWrapperText(text)) {
			continue
		}
		turns = append(turns, rawTurn{role, text, coerceTimestamp(m.created)})
	}

	s := Session{Cwd: ocString(directory), ID: id, Turns: mergeTurns(turns), Usage: usageOf(u)}
	s.Title = strip(ocString(title))
	if s.Title == "" {
		s.Title = firstUserLine(turns)
	}
	return s, nil
}
