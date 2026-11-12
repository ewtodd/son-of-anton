package tools

import (
	"fmt"
	"strings"
)

// The module a script imports (son_of_anton_tools) is generated from one
// table: for each tool its Python function, its parameters, what it returns
// and a sentence about it. The same table gives the Python function and the
// line the schema shows the model, so the two cannot drift apart.

// sandboxTools are the tools a script may call, in the order the schema
// lists them.
var sandboxTools = []string{"WebSearch", "WebFetch", "Read", "Write", "Glob", "Grep", "Edit", "Bash"}

// scriptParam is one parameter of a script-side function. def is the Python
// literal of its default; empty means the parameter is required. key is the
// tool's own name for it when that is no Python name ("-i"), else "".
type scriptParam struct{ name, typ, def, key string }

type scriptTool struct {
	fn      string // the Python function
	params  []scriptParam
	about   string // what the function does
	returns string // the dict it returns
}

var scriptTools = map[string]scriptTool{
	"WebSearch": {
		fn:      "web_search",
		params:  []scriptParam{{"query", "str", "", ""}, {"allowed_domains", "list", "None", ""}, {"blocked_domains", "list", "None", ""}},
		about:   "Search the web, five results at most.",
		returns: `{"data": {"web": [{"url", "title", "description"}, ...]}}`,
	},
	"WebFetch": {
		fn:      "web_fetch",
		params:  []scriptParam{{"url", "str", "", ""}, {"prompt", "str", `""`, ""}},
		about:   "Fetch a page as markdown, unsummarized. A page over 15000 characters keeps its head and tail; the whole text is saved on disk and the path is in the content footer.",
		returns: `{"url", "title", "content"} or {"url", "error"}`,
	},
	"Read": {
		fn:      "read",
		params:  []scriptParam{{"file_path", "str", "", ""}, {"offset", "int", "None", ""}, {"limit", "int", "None", ""}},
		about:   "Read a file's lines, numbered from 1 as cat -n numbers them (2000 unless limit says otherwise).",
		returns: `{"content": "...", "total_lines": N}`,
	},
	"Write": {
		fn:      "write",
		params:  []scriptParam{{"file_path", "str", "", ""}, {"content", "str", "", ""}},
		about:   "Replace a file's whole contents. A file that exists must have been read whole first.",
		returns: `{"bytes_written": N, ...}`,
	},
	"Glob": {
		fn:      "glob",
		params:  []scriptParam{{"pattern", "str", "", ""}, {"path", "str", "None", ""}},
		about:   "List the files matching a glob such as \"**/*.py\", newest first.",
		returns: `{"files": [...]}`,
	},
	"Grep": {
		fn: "grep",
		params: []scriptParam{
			{"pattern", "str", "", ""}, {"path", "str", "None", ""}, {"glob", "str", "None", ""}, {"type", "str", "None", ""},
			{"output_mode", "str", `"files_with_matches"`, ""}, {"ignore_case", "bool", "False", "-i"},
			{"line_numbers", "bool", "True", "-n"}, {"after", "int", "None", "-A"}, {"before", "int", "None", "-B"},
			{"context", "int", "None", "-C"}, {"multiline", "bool", "False", ""}, {"head_limit", "int", "None", ""},
		},
		about:   `Search inside files with a regular expression; output_mode is "files_with_matches", "content" or "count".`,
		returns: `{"files": [...]}, {"content": "..."} or {"counts": [...]}`,
	},
	"Edit": {
		fn:      "edit",
		params:  []scriptParam{{"file_path", "str", "", ""}, {"old_string", "str", "", ""}, {"new_string", "str", "", ""}, {"replace_all", "bool", "False", ""}},
		about:   "Replace old_string with new_string in a file read earlier.",
		returns: `{"success": true, "diff": "..."}`,
	},
	"Bash": {
		fn:      "bash",
		params:  []scriptParam{{"command", "str", "", ""}, {"timeout", "int", "None", ""}, {"description", "str", "None", ""}},
		about:   "Run a shell command and wait for it (timeout in milliseconds); no background mode.",
		returns: `{"output": "...", "exit_code": N}`,
	},
}

// signature is the parameter list as Python writes it.
func (t scriptTool) signature() string {
	parts := make([]string, len(t.params))
	for i, p := range t.params {
		parts[i] = p.name + ": " + p.typ
		if p.def != "" {
			parts[i] += " = " + p.def
		}
	}
	return strings.Join(parts, ", ")
}

// toolDocLines are the schema's lines for the tools, built from the table and
// keyed by tool name.
var toolDocLines = func() map[string]string {
	m := make(map[string]string, len(scriptTools))
	for name, t := range scriptTools {
		m[name] = fmt.Sprintf("  %s(%s) -> dict\n    %s Returns %s", t.fn, t.signature(), t.about, t.returns)
	}
	return m
}()

// stubModule is the source of son_of_anton_tools.py with a function for each
// of the named tools.
func stubModule(names []string) string {
	var b strings.Builder
	b.WriteString(stubPrelude)
	for _, n := range names {
		t, ok := scriptTools[n]
		if !ok {
			continue
		}
		fields := make([]string, len(t.params))
		for i, p := range t.params {
			key := p.key
			if key == "" {
				key = p.name
			}
			fields[i] = fmt.Sprintf("%q: %s", key, p.name)
		}
		fmt.Fprintf(&b, "\n\ndef %s(%s):\n    %q\n    return _call(%q, {%s})\n",
			t.fn, t.signature(), t.about+" Returns "+t.returns, n, strings.Join(fields, ", "))
	}
	return b.String()
}

// stubPrelude is the fixed part of the module: the helpers a script can use
// without importing anything else, and the one-line-per-request bridge to the
// daemon (a Unix socket whose path is in SON_OF_ANTON_RPC_SOCKET; every
// request carries SON_OF_ANTON_RPC_TOKEN).
const stubPrelude = `"""Bridge from a script to Son of Anton's tools."""
import json
import os
import shlex
import socket
import threading
import time

# The daemon answers one request at a time on one connection and does not
# number them, so a call holds the lock from its request to its reply.
_lock = threading.Lock()
_link = None


def json_parse(text):
    """json.loads that tolerates raw control characters and a leading BOM."""
    if isinstance(text, str) and text.startswith("\ufeff"):
        text = text[1:]
    return json.loads(text, strict=False)


def shell_quote(s):
    """Quote s for use inside a shell command line."""
    return shlex.quote(s)


def retry(fn, max_attempts=3, delay=2):
    """Call fn until it returns, waiting delay, 2*delay, 4*delay... between failures."""
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception:
            if attempt == max_attempts - 1:
                raise
            time.sleep(delay * 2 ** attempt)


def _stream():
    global _link
    if _link is None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(os.environ["SON_OF_ANTON_RPC_SOCKET"])
        sock.settimeout(300)
        _link = sock.makefile("rwb")
    return _link


def _call(tool, args):
    request = {"tool": tool, "args": args, "token": os.environ.get("SON_OF_ANTON_RPC_TOKEN", "")}
    with _lock:
        link = _stream()
        link.write(json.dumps(request).encode() + b"\n")
        link.flush()
        reply = link.readline()
    if not reply:
        raise RuntimeError("the daemon closed the connection")
    value = json.loads(reply)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value
`
