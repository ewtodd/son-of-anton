package main

// soa-d import: copies the Python fork's sessions out of its state.db into
// soa-d.db (daemon/phases/08a-import.md).

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/config"
	"github.com/ewtodd/son-of-anton/daemon/internal/paths"
	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

func runImport(args []string, stdout, stderr io.Writer) int {
	var from, home, foreignFlag, foreignRoot string
	var force bool
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--force":
			force = true
		case "--from-claude", "--from-codex", "--from-opencode":
			if foreignFlag != "" {
				fmt.Fprint(stderr, usage)
				return 2
			}
			foreignFlag = args[i]
			// The root is optional: a following word that is not a flag.
			if i+1 < len(args) && !strings.HasPrefix(args[i+1], "--") {
				foreignRoot = args[i+1]
				i++
			}
		case "--from", "--home":
			if i+1 >= len(args) {
				fmt.Fprint(stderr, usage)
				return 2
			}
			if args[i] == "--from" {
				from = args[i+1]
			} else {
				home = args[i+1]
			}
			i++
		default:
			fmt.Fprint(stderr, usage)
			return 2
		}
	}
	if (from == "") == (foreignFlag == "") || (force && foreignFlag == "") {
		fmt.Fprint(stderr, usage)
		return 2
	}
	if home == "" {
		var err error
		if home, err = config.Home(); err != nil {
			fmt.Fprintf(stderr, "soa-d import: %v\n", err)
			return 1
		}
	}
	var res importResult
	var err error
	if foreignFlag != "" {
		res, err = importForeign(context.Background(), foreignFlag, foreignRoot, home, force, stderr)
	} else {
		res, err = importSessions(context.Background(), from, home)
	}
	if err != nil {
		fmt.Fprintf(stderr, "soa-d import: %v\n", err)
		return 1
	}
	fmt.Fprintf(stdout, "imported %d sessions (%d messages); %d already present, %d skipped\n",
		res.imported, res.messages, res.present, res.skipped)
	return 0
}

type importResult struct{ imported, messages, present, skipped int }

// pySession is one row of the Python's sessions table.
type pySession struct {
	id, source, key, chatType, chatID, model, prompt, cwd, title, origin string
	started                                                              float64
	ended                                                                sql.NullFloat64
	usage                                                                store.Usage
	context                                                              int
}

func importSessions(ctx context.Context, from, home string) (importResult, error) {
	var res importResult
	src := filepath.Join(from, "state.db")
	if _, err := os.Stat(src); err != nil {
		return res, fmt.Errorf("no state.db in %s", from)
	}
	// Read-only, so the Python's database is never created, migrated or written.
	q := url.Values{"mode": {"ro"}, "_pragma": {"query_only(1)", "busy_timeout(5000)"}}
	pydb, err := sql.Open("sqlite", (&url.URL{Scheme: "file", Path: src, RawQuery: q.Encode()}).String())
	if err != nil {
		return res, err
	}
	defer pydb.Close()

	sessions, err := readPySessions(ctx, pydb)
	if err != nil {
		return res, fmt.Errorf("reading %s: %w", src, err)
	}
	jobs, err := readPyJobs(from)
	if err != nil {
		return res, err
	}
	// The newest Signal session of each chat is the one the chat continues in.
	newest := map[string]string{}
	for _, ps := range sessions { // ordered by start
		if stamp := chatStamp(ps); ps.source == "signal" && stamp != "" {
			newest[stamp] = ps.id
		}
	}

	st, err := store.Open(filepath.Join(home, "soa-d.db"))
	if err != nil {
		return res, err
	}
	defer st.Close()
	for _, ps := range sessions {
		kind, scope, ok := mapSource(ps, jobs)
		if !ok {
			res.skipped++
			continue
		}
		msgs, err := readPyMessages(ctx, pydb, ps.id)
		if err != nil {
			return res, fmt.Errorf("reading session %s: %w", ps.id, err)
		}
		sess := store.Session{
			ID: ps.id, Kind: kind, Source: ps.source, Title: ps.title, TitleSource: titleSource(ps.title),
			Cwd: ps.cwd, Model: ps.model, Scope: scope, Created: unixTime(ps.started),
			Usage: ps.usage, ContextTokens: ps.context,
		}
		if ps.cwd != "" {
			sess.Workspace = paths.Workspace(ps.cwd)
		}
		if ps.ended.Valid {
			sess.Ended = unixTime(ps.ended.Float64)
		}
		if ps.source == "signal" {
			sess.Chat = chatStamp(ps)
			sess.Model = "" // a chat follows the instance's chat model
		}
		present, err := st.ImportSession(ctx, sess, ps.prompt, msgs, store.ImportOpts{
			MakeCurrent: sess.Chat != "" && newest[sess.Chat] == ps.id,
			Foreign:     pyForeign(ps.origin),
		})
		if err != nil {
			return res, fmt.Errorf("importing session %s: %w", ps.id, err)
		}
		if present {
			res.present++
			continue
		}
		res.imported++
		res.messages += len(msgs)
	}
	return res, nil
}

// pyForeign is the foreign session a Python session was imported from, from
// its origin_json, so it is not imported again from the tool's own store.
func pyForeign(origin string) *store.Foreign {
	var o struct {
		ImportedFrom struct {
			Tool      string `json:"tool"`
			SessionID string `json:"foreign_session_id"`
		} `json:"imported_from"`
	}
	if json.Unmarshal([]byte(origin), &o) != nil || o.ImportedFrom.Tool == "" || o.ImportedFrom.SessionID == "" {
		return nil
	}
	return &store.Foreign{Source: o.ImportedFrom.Tool, ID: o.ImportedFrom.SessionID}
}

func titleSource(title string) string {
	if title == "" {
		return ""
	}
	return "user"
}

func unixTime(f float64) time.Time {
	sec := int64(f)
	return time.Unix(sec, int64((f-float64(sec))*1e9))
}

func readPySessions(ctx context.Context, db *sql.DB) ([]pySession, error) {
	// The Python kept prompts in system_prompts, by hash; the sessions column
	// holds only those of a store from before that table.
	prompt := `system_prompt`
	var n int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'system_prompts'`).Scan(&n); err != nil {
		return nil, err
	}
	if n > 0 {
		prompt = `COALESCE(NULLIF(system_prompt, ''), (SELECT p.prompt FROM system_prompts p WHERE p.hash = sessions.system_prompt_hash))`
	}
	rows, err := db.QueryContext(ctx, `SELECT id, source, session_key, chat_type, chat_id, model, `+prompt+`, started_at, ended_at,
		input_tokens, output_tokens, cache_read_tokens, reasoning_tokens, api_call_count, last_prompt_tokens, cwd, title, origin_json
		FROM sessions ORDER BY started_at, id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []pySession
	for rows.Next() {
		var ps pySession
		var key, chatType, chatID, model, prompt, cwd, title, origin sql.NullString
		var in, outTok, cached, reasoning, calls, last sql.NullInt64
		if err := rows.Scan(&ps.id, &ps.source, &key, &chatType, &chatID, &model, &prompt, &ps.started, &ps.ended,
			&in, &outTok, &cached, &reasoning, &calls, &last, &cwd, &title, &origin); err != nil {
			return nil, err
		}
		ps.key, ps.chatType, ps.chatID, ps.model, ps.prompt = key.String, chatType.String, chatID.String, model.String, prompt.String
		ps.cwd, ps.title, ps.origin = cwd.String, title.String, origin.String
		ps.usage = store.Usage{Input: int(in.Int64), Output: int(outTok.Int64), Cached: int(cached.Int64), Reasoning: int(reasoning.Int64), Calls: int(calls.Int64)}
		ps.context = int(last.Int64)
		out = append(out, ps)
	}
	return out, rows.Err()
}

// mapSource gives the soa-d kind and scope for a Python source; ok is false
// for the sources the Python hid. A cron session is the gateway's when its
// job delivers to Signal, or when its job is gone (unknown fails closed).
func mapSource(ps pySession, jobs map[string]pyJob) (kind, scope string, ok bool) {
	switch ps.source {
	case "subagent", "tool", "kanban":
		return "", "", false
	case "oneshot":
		return "oneshot", "common", true
	case "signal":
		return "chat", "gateway", true
	case "cron":
		if strings.Contains(strings.ToLower(ps.key+" "+ps.origin), "signal") {
			return "cron", "gateway", true
		}
		if j, known := jobs[cronJobID(ps.id)]; known && !j.toSignal() {
			return "cron", "common", true
		}
		return "cron", "gateway", true
	}
	return "interactive", "common", true
}

// chatStamp is a Signal session's chat stamp, from its session key or else
// its chat type and id; "" when neither says.
func chatStamp(ps pySession) string {
	if rest, ok := strings.CutPrefix(ps.key, "agent:main:signal:"); ok {
		if r, ok := strings.CutPrefix(rest, "group:group:"); ok {
			return "signal:group:" + r
		}
		return "signal:" + rest
	}
	if ps.chatID == "" {
		return ""
	}
	if ps.chatType == "group" {
		return "signal:group:" + strings.TrimPrefix(ps.chatID, "group:")
	}
	return "signal:" + ps.chatType + ":" + ps.chatID
}

func readPyMessages(ctx context.Context, db *sql.DB, id string) ([]store.Message, error) {
	rows, err := db.QueryContext(ctx, `SELECT role, content, api_content, reasoning, reasoning_content, tool_calls, tool_call_id, tool_name, timestamp
		FROM messages WHERE session_id = ? AND active = 1 AND role IN ('user', 'assistant', 'tool') ORDER BY id`, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []store.Message
	for rows.Next() {
		var m store.Message
		var content, api, reasoning, reasoningContent, calls, callID, name sql.NullString
		var ts float64
		if err := rows.Scan(&m.Role, &content, &api, &reasoning, &reasoningContent, &calls, &callID, &name, &ts); err != nil {
			return nil, err
		}
		m.Content, m.APIContent, m.ToolCallID, m.ToolName = content.String, api.String, callID.String, name.String
		m.Reasoning = reasoning.String
		if m.Reasoning == "" {
			m.Reasoning = reasoningContent.String
		}
		if calls.String != "" {
			if m.ToolCalls, err = convertToolCalls(calls.String); err != nil {
				return nil, err
			}
		}
		m.TS = unixTime(ts)
		out = append(out, m)
	}
	return out, rows.Err()
}

// convertToolCalls re-encodes the Python's tool calls in soa-d's shape,
// dropping the keys soa-d does not use. Arguments stay a string.
func convertToolCalls(raw string) (json.RawMessage, error) {
	var in []struct {
		ID       string `json:"id"`
		Function struct {
			Name      string          `json:"name"`
			Arguments json.RawMessage `json:"arguments"`
		} `json:"function"`
	}
	if err := json.Unmarshal([]byte(raw), &in); err != nil {
		return nil, fmt.Errorf("tool calls: %w", err)
	}
	type fn struct {
		Name      string `json:"name"`
		Arguments string `json:"arguments"`
	}
	type call struct {
		ID       string `json:"id"`
		Type     string `json:"type"`
		Function fn     `json:"function"`
	}
	out := make([]call, 0, len(in))
	for _, c := range in {
		args := string(c.Function.Arguments)
		var s string
		if json.Unmarshal(c.Function.Arguments, &s) == nil {
			args = s
		} else if args == "" || args == "null" {
			args = ""
		}
		out = append(out, call{ID: c.ID, Type: "function", Function: fn{Name: c.Function.Name, Arguments: args}})
	}
	return json.Marshal(out)
}

// pyJob is what the importer needs of a Python cron job: where it delivers.
type pyJob struct {
	Deliver string `json:"deliver"`
	Origin  *struct {
		Platform string `json:"platform"`
	} `json:"origin"`
}

// toSignal says whether the job delivers to Signal (or might: anything but
// local, and an origin that is not another platform's).
func (j pyJob) toSignal() bool {
	deliver := j.Deliver
	if strings.TrimSpace(deliver) == "" {
		deliver = "local"
		if j.Origin != nil {
			deliver = "origin"
		}
	}
	for _, d := range strings.Split(deliver, ",") {
		switch d = strings.ToLower(strings.TrimSpace(d)); d {
		case "", "local":
		case "origin":
			if j.Origin == nil || j.Origin.Platform == "" || j.Origin.Platform == "signal" {
				return true
			}
		default: // signal, signal:<chat>, all
			return true
		}
	}
	return false
}

// readPyJobs reads the Python home's cron/jobs.json, by job id; none is an
// empty map.
func readPyJobs(from string) (map[string]pyJob, error) {
	b, err := os.ReadFile(filepath.Join(from, "cron", "jobs.json"))
	if errors.Is(err, os.ErrNotExist) {
		return map[string]pyJob{}, nil
	}
	if err != nil {
		return nil, err
	}
	b = bytes.TrimPrefix(b, []byte("\xef\xbb\xbf"))
	var file struct {
		Jobs []json.RawMessage `json:"jobs"`
	}
	var list []json.RawMessage
	if err := json.Unmarshal(b, &file); err == nil && file.Jobs != nil {
		list = file.Jobs
	} else if err := json.Unmarshal(b, &list); err != nil {
		return nil, fmt.Errorf("reading %s: %w", filepath.Join(from, "cron", "jobs.json"), err)
	}
	out := map[string]pyJob{}
	for _, raw := range list {
		var j pyJob
		var id struct {
			ID    string `json:"id"`
			JobID string `json:"job_id"`
		}
		if json.Unmarshal(raw, &j) != nil || json.Unmarshal(raw, &id) != nil {
			continue
		}
		if id.ID == "" {
			id.ID = id.JobID
		}
		out[id.ID] = j
	}
	return out, nil
}

// cronJobID is the job id in a Python cron session's id
// (cron_<job id>_<time>), "" when it is not one.
func cronJobID(sessionID string) string {
	rest, ok := strings.CutPrefix(sessionID, "cron_")
	if !ok {
		return ""
	}
	id, _, _ := strings.Cut(rest, "_")
	return id
}
