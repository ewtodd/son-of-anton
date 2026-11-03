package tools

import (
	"fmt"
	"strings"
)

// The module a script imports (son_of_anton_tools) is generated from one
// table: for each tool its parameters, what it returns and a sentence about
// it. The same table gives the Python function and the line the schema
// shows the model, so the two cannot drift apart.

// sandboxTools are the tools a script may call, in the order the schema
// lists them.
var sandboxTools = []string{"web_search", "web_extract", "read_file", "write_file", "search_files", "patch", "terminal"}

// scriptParam is one parameter of a script-side function. def is the Python
// literal of its default; empty means the parameter is required.
type scriptParam struct{ name, typ, def string }

type scriptTool struct {
	params  []scriptParam
	about   string // what the function does
	returns string // the dict it returns
}

var scriptTools = map[string]scriptTool{
	"web_search": {
		params:  []scriptParam{{"query", "str", ""}, {"limit", "int", "5"}},
		about:   "Search the web.",
		returns: `{"data": {"web": [{"url", "title", "description"}, ...]}}`,
	},
	"web_extract": {
		params:  []scriptParam{{"urls", "list", ""}, {"char_limit", "int", "None"}},
		about:   "Fetch pages as markdown, without summarizing them. A page longer than char_limit (15000 when unset) keeps its head and tail, the whole text is saved on disk and the path is in the content footer.",
		returns: `{"results": [{"url", "title", "content", "error"}, ...]}`,
	},
	"read_file": {
		params:  []scriptParam{{"path", "str", ""}, {"offset", "int", "1"}, {"limit", "int", "2000"}},
		about:   "Read lines from a file, counting from 1.",
		returns: `{"content": "...", "total_lines": N}`,
	},
	"write_file": {
		params:  []scriptParam{{"path", "str", ""}, {"content", "str", ""}},
		about:   "Replace a file's whole contents.",
		returns: `{"status": ...}`,
	},
	"search_files": {
		params: []scriptParam{
			{"pattern", "str", ""}, {"target", "str", `"content"`}, {"path", "str", `"."`},
			{"file_glob", "str", "None"}, {"limit", "int", "50"}, {"offset", "int", "0"},
			{"output_mode", "str", `"content"`}, {"context", "int", "0"},
		},
		about:   `Search inside files (target="content") or find files by name (target="files").`,
		returns: `{"matches": [...]}`,
	},
	"patch": {
		params:  []scriptParam{{"path", "str", ""}, {"old_string", "str", ""}, {"new_string", "str", ""}, {"replace_all", "bool", "False"}},
		about:   "Replace old_string with new_string in a file.",
		returns: `{"status": ...}`,
	},
	"terminal": {
		params:  []scriptParam{{"command", "str", ""}, {"timeout", "int", "None"}, {"workdir", "str", "None"}},
		about:   "Run a shell command and wait for it; no background mode.",
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

// toolDocLines are the schema's lines for the tools, built from the table.
var toolDocLines = func() map[string]string {
	m := make(map[string]string, len(scriptTools))
	for name, t := range scriptTools {
		m[name] = fmt.Sprintf("  %s(%s) -> dict\n    %s Returns %s", name, t.signature(), t.about, t.returns)
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
			fields[i] = fmt.Sprintf("%q: %s", p.name, p.name)
		}
		fmt.Fprintf(&b, "\n\ndef %s(%s):\n    %q\n    return _call(%q, {%s})\n",
			n, t.signature(), t.about+" Returns "+t.returns, n, strings.Join(fields, ", "))
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
