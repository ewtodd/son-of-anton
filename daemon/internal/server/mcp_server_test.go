package server_test

// Phase 7b through the daemon: MCP servers reached through tool_search,
// tool_describe and tool_call (daemon/phases/07b-mcp.md). The fake servers
// speak the protocol directly: the stdio one is this test binary started
// again (TestFakeMCPServerProcess), the HTTP one an httptest server.

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

var fakePNG = []byte("\x89PNG\r\n\x1a\nfake image bytes")

// mcpTools is the stdio fake's catalog, served over two pages.
var mcpTools = [][]map[string]any{
	{
		{"name": "echo", "description": "Echo the text back to you.", "inputSchema": map[string]any{
			"type": "object", "properties": map[string]any{"text": map[string]any{"type": "string"}}, "required": []string{"text"}}},
		{"name": "shout-loud", "description": "Shout a word.", "inputSchema": map[string]any{"type": "object"}},
		{"name": "hidden_tool", "description": "A hidden secret tool.", "inputSchema": map[string]any{"type": "object"}},
		{"name": "fail", "description": "Always fails.", "inputSchema": map[string]any{"type": "object"}},
	},
	{
		{"name": "big", "description": "Returns a very large result.", "inputSchema": map[string]any{"type": "object"}},
		{"name": "picture", "description": "Returns an image.", "inputSchema": map[string]any{"type": "object"}},
		{"name": "env", "description": "Reports two environment variables.", "inputSchema": map[string]any{"type": "object"}},
	},
}

// mcpAnswer is the fake servers' JSON-RPC: the result (or nil for a
// notification) for one request.
func mcpAnswer(method string, params map[string]any, tools [][]map[string]any, call func(name string, args map[string]any) map[string]any) any {
	switch method {
	case "initialize":
		v, _ := params["protocolVersion"].(string)
		return map[string]any{"protocolVersion": v, "capabilities": map[string]any{"tools": map[string]any{"listChanged": true}},
			"serverInfo": map[string]any{"name": "fake", "version": "1"}}
	case "ping":
		return map[string]any{}
	case "tools/list":
		page := 0
		if c, _ := params["cursor"].(string); c != "" {
			fmt.Sscanf(c, "page%d", &page)
		}
		out := map[string]any{"tools": tools[page]}
		if page+1 < len(tools) {
			out["nextCursor"] = fmt.Sprintf("page%d", page+1)
		}
		return out
	case "tools/call":
		name, _ := params["name"].(string)
		args, _ := params["arguments"].(map[string]any)
		return call(name, args)
	}
	return nil
}

func mcpText(s string) map[string]any {
	return map[string]any{"content": []any{map[string]any{"type": "text", "text": s}}}
}

// TestFakeMCPServerProcess is the stdio fake when SOA_FAKE_MCP is set; it
// does nothing in a normal run.
func TestFakeMCPServerProcess(t *testing.T) {
	if os.Getenv("SOA_FAKE_MCP") != "1" {
		t.Skip("the fake stdio MCP server runs only as a child process")
	}
	fmt.Fprintln(os.Stderr, "fake mcp server up")
	call := func(name string, args map[string]any) map[string]any {
		switch name {
		case "echo":
			s, _ := args["text"].(string)
			return mcpText("echo: " + s)
		case "shout_loud", "shout-loud":
			return mcpText("HEY")
		case "fail":
			r := mcpText("it broke")
			r["isError"] = true
			return r
		case "big":
			return mcpText(strings.Repeat("0123456789", 6000))
		case "picture":
			return map[string]any{"content": []any{map[string]any{"type": "image", "mimeType": "image/png",
				"data": base64.StdEncoding.EncodeToString(fakePNG)}}}
		case "env":
			return mcpText(fmt.Sprintf("GIVEN=%s LEAKY=%s", os.Getenv("GIVEN"), os.Getenv("LEAKY_VAR")))
		}
		r := mcpText("no tool " + name)
		r["isError"] = true
		return r
	}
	in := bufio.NewScanner(os.Stdin)
	in.Buffer(make([]byte, 1<<20), 1<<24)
	out := json.NewEncoder(os.Stdout)
	for in.Scan() {
		var req struct {
			ID     any            `json:"id"`
			Method string         `json:"method"`
			Params map[string]any `json:"params"`
		}
		if json.Unmarshal(in.Bytes(), &req) != nil || req.ID == nil {
			continue // a notification, or not ours
		}
		res := mcpAnswer(req.Method, req.Params, mcpTools, call)
		if res == nil {
			_ = out.Encode(map[string]any{"jsonrpc": "2.0", "id": req.ID, "error": map[string]any{"code": -32601, "message": "no method " + req.Method}})
			continue
		}
		_ = out.Encode(map[string]any{"jsonrpc": "2.0", "id": req.ID, "result": res})
	}
	os.Exit(0)
}

// httpMCP is the HTTP fake: one tool, a bearer token, a session id.
type httpMCP struct {
	mu       sync.Mutex
	auth     []string
	sessions []string
}

func (f *httpMCP) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	f.mu.Lock()
	f.auth = append(f.auth, r.Header.Get("Authorization"))
	f.sessions = append(f.sessions, r.Header.Get("Mcp-Session-Id"))
	f.mu.Unlock()
	if r.Header.Get("Authorization") != "Bearer s3cret-mcp" {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	body, _ := io.ReadAll(r.Body)
	var req struct {
		ID     any            `json:"id"`
		Method string         `json:"method"`
		Params map[string]any `json:"params"`
	}
	if err := json.Unmarshal(body, &req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if req.ID == nil {
		w.WriteHeader(http.StatusAccepted)
		return
	}
	tools := [][]map[string]any{{{"name": "lookup", "description": "Look a key up.", "inputSchema": map[string]any{
		"type": "object", "properties": map[string]any{"key": map[string]any{"type": "string"}}}}}}
	res := mcpAnswer(req.Method, req.Params, tools, func(name string, args map[string]any) map[string]any {
		k, _ := args["key"].(string)
		return mcpText("value of " + k)
	})
	if req.Method == "initialize" {
		w.Header().Set("Mcp-Session-Id", "sess-1")
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": req.ID, "result": res})
}

func mcpConfig(httpURL string) string {
	return fmt.Sprintf(`[mcp_servers.local]
command = %q
args = ["-test.run=^TestFakeMCPServerProcess$"]

[mcp_servers.local.env]
SOA_FAKE_MCP = "1"
GIVEN = "yes"

[mcp_servers.local.tools]
exclude = ["hidden*"]

[mcp_servers.remote]
url = %q

[mcp_servers.remote.headers]
Authorization = "Bearer ${FAKE_MCP_KEY}"

[mcp_servers.broken]
url = "http://127.0.0.1:9/mcp"

[mcp_servers.broken.headers]
Authorization = "Bearer ${NOT_SET_ANYWHERE}"

[mcp_servers.off]
command = "/nonexistent/mcp"
enabled = false
`, os.Args[0], httpURL+"/mcp")
}

func tc(id, name, args string) fakellm.ToolCall { return call(id, name, args) }

func TestMCPToolsThroughTheTrio(t *testing.T) {
	remote := &httpMCP{}
	srv := httptest.NewServer(remote)
	defer srv.Close()
	t.Setenv("FAKE_MCP_KEY", "s3cret-mcp")
	t.Setenv("LEAKY_VAR", "leak")
	t.Setenv("NOT_SET_ANYWHERE", "")
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{
			tc("s1", "tool_search", `{"query":"echo text back"}`),
			tc("s2", "tool_search", `{"query":"hidden secret tool"}`),
			tc("s3", "tool_search", `{"query":"shout"}`),
			tc("s4", "tool_search", `{"query":"zzzz nothing matches"}`),
			tc("d1", "tool_describe", `{"name":"mcp__local__echo"}`),
		}},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{
			tc("c1", "tool_call", `{"name":"mcp__local__echo","arguments":{"text":"hi there"}}`),
			tc("c2", "tool_call", `{"name":"mcp__local__fail","arguments":{}}`),
			tc("c3", "tool_call", `{"name":"mcp__local__big","arguments":{}}`),
			tc("c4", "tool_call", `{"name":"mcp__local__picture","arguments":{}}`),
			tc("c5", "tool_call", `{"name":"mcp__local__env","arguments":{}}`),
			tc("c6", "tool_call", `{"name":"mcp__remote__lookup","arguments":"{\"key\":\"k1\"}"}`),
			tc("c7", "tool_call", `{"name":"mcp__nope__x","arguments":{}}`),
		}},
		fakellm.Turn{Content: "done"},
	)
	h := newHarness(t, fake, mcpConfig(srv.URL))
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "use the mcp tools")
	rows := toolRows(c.turn())
	if len(rows) != 12 {
		t.Fatalf("tool rows = %d: %v", len(rows), rows)
	}

	var searchDesc string
	for _, raw := range fake.Request(0)["tools"].([]any) {
		fn := raw.(map[string]any)["function"].(map[string]any)
		name := str(fn, "name")
		if strings.HasPrefix(name, "mcp__") {
			t.Fatalf("an MCP schema rides the request: %s", name)
		}
		if name == "tool_search" {
			searchDesc = str(fn, "description")
		}
	}
	names := strings.Join(toolNames(t, fake.Request(0)), ",")
	if !strings.Contains(names, "tool_search") || !strings.Contains(names, "tool_describe") || !strings.Contains(names, "tool_call") {
		t.Fatalf("the trio is offered: %s", names)
	}
	if !strings.Contains(searchDesc, "(broken, local, remote)") {
		t.Fatalf("tool_search names the enabled servers: %q", searchDesc)
	}

	decode := func(i int) map[string]any {
		t.Helper()
		var m map[string]any
		if err := json.Unmarshal([]byte(rows[i]), &m); err != nil {
			t.Fatalf("row %d is not JSON: %s", i, rows[i])
		}
		return m
	}
	matchNames := func(m map[string]any) []string {
		var out []string
		list, _ := m["matches"].([]any)
		for _, x := range list {
			out = append(out, str(x.(map[string]any), "name"))
		}
		return out
	}
	s1 := decode(0)
	if got := matchNames(s1); len(got) == 0 || got[0] != "mcp__local__echo" || str(s1["matches"].([]any)[0].(map[string]any), "server") != "local" {
		t.Fatalf("search echo = %v", s1)
	}
	if total, _ := s1["total_available"].(float64); total != 7 {
		t.Fatalf("total_available = %v: 6 local tools after the filter (two pages) plus 1 remote", s1["total_available"])
	}
	for _, n := range matchNames(decode(1)) {
		if strings.Contains(n, "hidden") {
			t.Fatalf("an excluded tool was found: %v", n)
		}
	}
	if got := matchNames(decode(2)); len(got) == 0 || got[0] != "mcp__local__shout_loud" {
		t.Fatalf("names are sanitized: %v", got)
	}
	s4 := decode(3)
	un, _ := s4["unavailable"].([]any)
	if len(matchNames(s4)) != 0 || len(un) != 1 || str(un[0].(map[string]any), "server") != "broken" ||
		!strings.Contains(str(un[0].(map[string]any), "error"), "NOT_SET_ANYWHERE") {
		t.Fatalf("no match names the server that is down, and why: %v", s4)
	}
	d1 := decode(4)
	params, _ := d1["parameters"].(map[string]any)
	props, _ := params["properties"].(map[string]any)
	if str(d1, "name") != "mcp__local__echo" || str(d1, "server") != "local" || props["text"] == nil {
		t.Fatalf("describe = %v", d1)
	}

	wrapped := func(i int, tool string) string {
		t.Helper()
		pre := "<untrusted_tool_result source=\"" + tool + "\">\n"
		if !strings.HasPrefix(rows[i], pre) || !strings.HasSuffix(rows[i], "\n</untrusted_tool_result>") {
			t.Fatalf("row %d is not wrapped as %s: %.200s", i, tool, rows[i])
		}
		return rows[i]
	}
	if r := wrapped(5, "mcp__local__echo"); !strings.Contains(r, `"result":"echo: hi there"`) {
		t.Fatalf("echo = %s", r)
	}
	if r := wrapped(6, "mcp__local__fail"); !strings.Contains(r, `"error":"it broke"`) {
		t.Fatalf("an isError result = %s", r)
	}
	big := wrapped(7, "mcp__local__big")
	m := regexp.MustCompile(`characters omitted; the full result is saved at ([^\]]+)\]`).FindStringSubmatch(big)
	if m == nil || len(big) > 60000 {
		t.Fatalf("a big result is cut (%d chars): %.300s", len(big), big)
	}
	if saved, err := os.ReadFile(m[1]); err != nil || len(saved) != 60000 {
		t.Fatalf("the full result is saved at %s: %d bytes, %v", m[1], len(saved), err)
	}
	pic := wrapped(8, "mcp__local__picture")
	m = regexp.MustCompile(`\[image saved at ([^\]]+)\]`).FindStringSubmatch(pic)
	if m == nil || !strings.HasPrefix(m[1], filepath.Join(h.home, "cache", "mcp")+string(filepath.Separator)) || filepath.Ext(m[1]) != ".png" {
		t.Fatalf("an image = %s", pic)
	}
	if b, err := os.ReadFile(m[1]); err != nil || string(b) != string(fakePNG) {
		t.Fatalf("the saved image: %v", err)
	}
	if r := wrapped(9, "mcp__local__env"); !strings.Contains(r, `"result":"GIVEN=yes LEAKY="`) {
		t.Fatalf("a stdio server gets the config env and not the daemon's: %s", r)
	}
	if r := wrapped(10, "mcp__remote__lookup"); !strings.Contains(r, `"result":"value of k1"`) {
		t.Fatalf("remote lookup with string arguments = %s", r)
	}
	if !strings.Contains(rows[11], "no MCP tool mcp__nope__x; use tool_search to find one") {
		t.Fatalf("an unknown tool = %s", rows[11])
	}

	remote.mu.Lock()
	for i, a := range remote.auth {
		if a != "Bearer s3cret-mcp" {
			t.Errorf("request %d had Authorization %q", i, a)
		}
	}
	last := remote.sessions[len(remote.sessions)-1]
	remote.mu.Unlock()
	if last != "sess-1" {
		t.Fatalf("the session id goes back to the server: %q", last)
	}
	log, _ := os.ReadFile(filepath.Join(h.home, "logs", "mcp-stderr.log"))
	if !strings.Contains(string(log), "starting MCP server 'local' =====") || !strings.Contains(string(log), "fake mcp server up") {
		t.Fatalf("the stderr log:\n%s", log)
	}
}

func TestNoMCPServersNoTrio(t *testing.T) {
	fake := fakellm.New(t, fakellm.Turn{Content: "hi"})
	h := newHarness(t, fake, "[mcp_servers.off]\ncommand = \"/nonexistent\"\nenabled = false\n")
	c := h.dial()
	id := c.create(t.TempDir())
	c.attach(id, "driver")
	c.send(id, "hello")
	c.turn()
	for _, n := range toolNames(t, fake.Request(0)) {
		if n == "tool_search" || n == "tool_describe" || n == "tool_call" {
			t.Fatalf("no enabled MCP server, no %s", n)
		}
	}
}
