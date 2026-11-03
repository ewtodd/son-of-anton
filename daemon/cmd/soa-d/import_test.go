package main

// Phase 8a: soa-d import copies the Python fork's sessions out of its
// state.db (daemon/phases/08a-import.md). The fixture is a Python-shaped
// state.db with the columns the importer reads.

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

const pySchema = `
CREATE TABLE sessions (
	id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT, session_key TEXT, chat_id TEXT, chat_type TEXT,
	model TEXT, system_prompt TEXT, parent_session_id TEXT, started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
	input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
	reasoning_tokens INTEGER DEFAULT 0, api_call_count INTEGER DEFAULT 0, last_prompt_tokens INTEGER DEFAULT 0,
	cwd TEXT, title TEXT, title_source TEXT, origin_json TEXT, archived INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0);
CREATE TABLE messages (
	id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT,
	tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL, reasoning TEXT, reasoning_content TEXT,
	active INTEGER NOT NULL DEFAULT 1, compacted INTEGER NOT NULL DEFAULT 0, api_content TEXT, display_kind TEXT);
`

type pyMsg struct {
	role, content, toolCallID, toolCalls, toolName, reasoning string
	active                                                    int
	ts                                                        float64
}

func pyHome(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	db, err := sql.Open("sqlite", filepath.Join(dir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec(pySchema); err != nil {
		t.Fatal(err)
	}
	session := func(id, source, key, chatType, chatID, title string, started float64, msgs ...pyMsg) {
		t.Helper()
		_, err := db.Exec(`INSERT INTO sessions (id, source, session_key, chat_type, chat_id, model, system_prompt, started_at, ended_at,
			input_tokens, output_tokens, cache_read_tokens, reasoning_tokens, api_call_count, last_prompt_tokens, cwd, title)
			VALUES (?, ?, ?, ?, ?, 'auto', ?, ?, ?, 1000, 200, 300, 40, 5, 1234, '/home/u/proj', ?)`,
			id, source, nullable(key), nullable(chatType), nullable(chatID), "the prompt of "+id, started, started+60, nullable(title))
		if err != nil {
			t.Fatal(err)
		}
		for _, m := range msgs {
			_, err := db.Exec(`INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp, reasoning, active, compacted)
				VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`, id, m.role, nullable(m.content), nullable(m.toolCallID), nullable(m.toolCalls),
				nullable(m.toolName), m.ts, nullable(m.reasoning), m.active, 1-m.active)
			if err != nil {
				t.Fatal(err)
			}
		}
	}
	call := `[{"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": "{\"command\": \"ls\"}"}, "call_id": "x", "response_item_id": "y"}]`
	session("20260930_231905_255110cf", "cli", "", "", "", "Fixing the build", 1759270745,
		pyMsg{role: "user", content: "old question", active: 0, ts: 1759270746},
		pyMsg{role: "assistant", content: "old answer", active: 0, ts: 1759270747},
		pyMsg{role: "user", content: "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below.", active: 1, ts: 1759270748},
		pyMsg{role: "assistant", content: "", toolCalls: call, reasoning: "need to look", active: 1, ts: 1759270749},
		pyMsg{role: "tool", content: "a.go\nb.go", toolCallID: "call_1", toolName: "terminal", active: 1, ts: 1759270750},
		pyMsg{role: "session_meta", content: "{}", active: 1, ts: 1759270750.5},
		pyMsg{role: "assistant", content: "two files", active: 1, ts: 1759270751},
	)
	session("20261001_100000_aaaaaaaa", "signal", "agent:main:signal:group:group:gW:+15551111111", "group", "group:gW", "", 1759312800,
		pyMsg{role: "user", content: "hi fiona", active: 1, ts: 1759312801}, pyMsg{role: "assistant", content: "hello", active: 1, ts: 1759312802})
	session("20261002_100000_bbbbbbbb", "signal", "agent:main:signal:group:group:gW:+15551111111", "group", "group:gW", "", 1759399200,
		pyMsg{role: "user", content: "newer chat", active: 1, ts: 1759399201}, pyMsg{role: "assistant", content: "yes", active: 1, ts: 1759399202})
	session("20261003_100000_cccccccc", "opencode", "", "", "", "Imported from opencode: refactor", 1759485600,
		pyMsg{role: "user", content: "refactor it", active: 1, ts: 1759485601}, pyMsg{role: "assistant", content: "done", active: 1, ts: 1759485602})
	session("20261004_100000_dddddddd", "subagent", "", "", "", "", 1759572000,
		pyMsg{role: "user", content: "child work", active: 1, ts: 1759572001})
	session("20261005_100000_eeeeeeee", "cron", "", "", "", "Morning digest · Oct 05 07:00", 1759658400,
		pyMsg{role: "user", content: "digest", active: 1, ts: 1759658401}, pyMsg{role: "assistant", content: "all good", active: 1, ts: 1759658402})
	session("20261006_100000_ffffffff", "oneshot", "", "", "", "", 1759744800,
		pyMsg{role: "user", content: "one shot", active: 1, ts: 1759744801}, pyMsg{role: "assistant", content: "shot", active: 1, ts: 1759744802})
	return dir
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func fileHash(t *testing.T, path string) [32]byte {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return sha256.Sum256(b)
}

func TestImportCopiesThePythonSessions(t *testing.T) {
	src := pyHome(t)
	before := fileHash(t, filepath.Join(src, "state.db"))
	home := t.TempDir()
	code, out, errOut := runIn(t, "import", "--from", src, "--home", home)
	if code != 0 || out != "imported 6 sessions (14 messages); 0 already present, 1 skipped\n" {
		t.Fatalf("exit %d, stdout %q, stderr %q", code, out, errOut)
	}
	if fileHash(t, filepath.Join(src, "state.db")) != before {
		t.Fatal("the Python's state.db was written")
	}
	st, err := store.Open(filepath.Join(home, "soa-d.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	ctx := context.Background()

	cli, err := st.Session(ctx, "20260930_231905_255110cf")
	if err != nil {
		t.Fatal(err)
	}
	if cli.Kind != "interactive" || cli.Source != "cli" || cli.Title != "Fixing the build" || cli.Cwd != "/home/u/proj" || cli.Model != "auto" ||
		cli.ParentID != "" || cli.Scope != "common" || cli.Chat != "" || !cli.Created.Equal(time.Unix(1759270745, 0)) ||
		cli.Usage != (store.Usage{Input: 1000, Output: 200, Cached: 300, Reasoning: 40, Calls: 5}) || cli.ContextTokens != 1234 {
		t.Fatalf("the cli session = %+v", cli)
	}
	if p, err := st.Prompt(ctx, cli.PromptHash); err != nil || p != "the prompt of 20260930_231905_255110cf" {
		t.Fatalf("the stored prompt = %q, %v", p, err)
	}
	msgs, err := st.Messages(ctx, cli.ID)
	if err != nil {
		t.Fatal(err)
	}
	var roles []string
	for _, m := range msgs {
		roles = append(roles, m.Role)
	}
	if strings.Join(roles, ",") != "user,assistant,tool,assistant" || msgs[0].Seq != 1 ||
		!strings.HasPrefix(msgs[0].Content, "[CONTEXT COMPACTION — REFERENCE ONLY]") || msgs[1].Reasoning != "need to look" ||
		!msgs[2].TS.Equal(time.Unix(1759270750, 0)) || msgs[2].ToolCallID != "call_1" || msgs[2].ToolName != "terminal" || msgs[3].Content != "two files" {
		t.Fatalf("the active rows only, in order: %+v", msgs)
	}
	var calls []map[string]any
	if err := json.Unmarshal(msgs[1].ToolCalls, &calls); err != nil || len(calls) != 1 {
		t.Fatalf("tool calls = %s", msgs[1].ToolCalls)
	}
	fn, _ := calls[0]["function"].(map[string]any)
	if calls[0]["id"] != "call_1" || calls[0]["type"] != "function" || fn["name"] != "terminal" || fn["arguments"] != `{"command": "ls"}` ||
		calls[0]["call_id"] != nil {
		t.Fatalf("tool calls in soa-d's shape: %s", msgs[1].ToolCalls)
	}

	stamp := "signal:group:gW:+15551111111"
	for _, id := range []string{"20261001_100000_aaaaaaaa", "20261002_100000_bbbbbbbb"} {
		s, err := st.Session(ctx, id)
		if err != nil || s.Kind != "chat" || s.Scope != "gateway" || s.Chat != stamp {
			t.Fatalf("a Signal session = %+v, %v", s, err)
		}
	}
	if cur, err := st.ChatSession(ctx, stamp); err != nil || cur != "20261002_100000_bbbbbbbb" {
		t.Fatalf("the chat continues in its newest session: %q, %v", cur, err)
	}
	if s, _ := st.Session(ctx, "20261003_100000_cccccccc"); s.Kind != "interactive" || s.Source != "opencode" {
		t.Fatalf("a foreign session = %+v", s)
	}
	if s, _ := st.Session(ctx, "20261005_100000_eeeeeeee"); s.Kind != "cron" {
		t.Fatalf("a cron session = %+v", s)
	}
	if s, _ := st.Session(ctx, "20261006_100000_ffffffff"); s.Kind != "oneshot" {
		t.Fatalf("a oneshot session = %+v", s)
	}
	if _, err := st.Session(ctx, "20261004_100000_dddddddd"); err == nil {
		t.Fatal("a subagent session is not imported")
	}
	if hits, _ := st.SearchMessages(ctx, store.MessageQuery{Query: "refactor", Caller: "common"}); len(hits) == 0 {
		t.Fatal("imported rows are searchable")
	}
	if hits, _ := st.SearchMessages(ctx, store.MessageQuery{Query: "fiona", Caller: "common"}); len(hits) != 0 {
		t.Fatal("imported Signal rows stay behind the wall")
	}
	if list, _ := st.ListReadable(ctx, "common", []string{"interactive", "chat"}, 50); len(list) != 2 {
		t.Fatalf("the TUI lists the two interactive sessions only: %d", len(list))
	}

	code, out, _ = runIn(t, "import", "--from", src, "--home", home)
	if code != 0 || out != "imported 0 sessions (0 messages); 6 already present, 1 skipped\n" {
		t.Fatalf("a second run: exit %d, %q", code, out)
	}
}

func TestImportFailsCleanly(t *testing.T) {
	code, _, errOut := runIn(t, "import", "--from", t.TempDir(), "--home", t.TempDir())
	if code != 1 || !strings.Contains(errOut, "state.db") {
		t.Fatalf("a home without state.db: exit %d, %q", code, errOut)
	}
}
