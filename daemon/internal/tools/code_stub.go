package tools

import (
	"fmt"
	"strings"
)

// sandboxTools are the tools a script may call, in the order the schema
// documents them.
var sandboxTools = []string{"web_search", "web_extract", "read_file", "write_file", "search_files", "patch", "terminal"}

// toolStub is one tool's function in the generated module: its name,
// signature, docstring and the arguments it sends.
type toolStub struct{ sig, doc, args string }

// toolStubs are the stubs for soa-d's own tool parameters.
var toolStubs = map[string]toolStub{
	"web_search": {
		"query: str, limit: int = 5",
		`"""Search the web. Returns dict with data.web list of {url, title, description}."""`,
		`{"query": query, "limit": limit}`,
	},
	"web_extract": {
		"urls: list, char_limit: int = None",
		`"""Extract content from URLs (no LLM summarization). Returns dict with results list of {url, title, content, error}. Pages over char_limit (default 15000) are head+tail truncated with the full text stored on disk; the content footer gives the path. content is markdown."""`,
		`{"urls": urls, "char_limit": char_limit}`,
	},
	"read_file": {
		"path: str, offset: int = 1, limit: int = 2000",
		`"""Read a file (1-indexed lines). Returns dict with "content" and "total_lines"."""`,
		`{"path": path, "offset": offset, "limit": limit}`,
	},
	"write_file": {
		"path: str, content: str",
		`"""Write content to a file (always overwrites). Returns dict with status."""`,
		`{"path": path, "content": content}`,
	},
	"search_files": {
		`pattern: str, target: str = "content", path: str = ".", file_glob: str = None, limit: int = 50, offset: int = 0, output_mode: str = "content", context: int = 0`,
		`"""Search file contents (target="content") or find files by name (target="files"). Returns dict with "matches"."""`,
		`{"pattern": pattern, "target": target, "path": path, "file_glob": file_glob, "limit": limit, "offset": offset, "output_mode": output_mode, "context": context}`,
	},
	"patch": {
		"path: str, old_string: str, new_string: str, replace_all: bool = False",
		`"""Targeted find-and-replace. Returns dict with status."""`,
		`{"path": path, "old_string": old_string, "new_string": new_string, "replace_all": replace_all}`,
	},
	"terminal": {
		"command: str, timeout: int = None, workdir: str = None",
		`"""Run a shell command (foreground only). Returns dict with "output" and "exit_code"."""`,
		`{"command": command, "timeout": timeout, "workdir": workdir}`,
	},
}

// toolDocLines are the schema's per-tool documentation lines.
var toolDocLines = map[string]string{
	"web_search": "  web_search(query: str, limit: int = 5) -> dict\n" +
		"    Returns {\"data\": {\"web\": [{\"url\", \"title\", \"description\"}, ...]}}",
	"web_extract": "  web_extract(urls: list[str], char_limit: int = None) -> dict\n" +
		"    Returns {\"results\": [{\"url\", \"title\", \"content\", \"error\"}, ...]} where content is markdown.\n" +
		"    No LLM summarization. Pages over char_limit (default 15000) are head+tail truncated; full text stored on disk (path in the content footer).",
	"read_file": "  read_file(path: str, offset: int = 1, limit: int = 2000) -> dict\n" +
		"    Lines are 1-indexed. Returns {\"content\": \"...\", \"total_lines\": N}",
	"write_file": "  write_file(path: str, content: str) -> dict\n" +
		"    Always overwrites the entire file.",
	"search_files": "  search_files(pattern: str, target=\"content\", path=\".\", file_glob=None, limit=50) -> dict\n" +
		"    target: \"content\" (search inside files) or \"files\" (find files by name). Returns {\"matches\": [...]}",
	"patch": "  patch(path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict\n" +
		"    Replaces old_string with new_string in the file.",
	"terminal": "  terminal(command: str, timeout=None, workdir=None) -> dict\n" +
		"    Foreground only (no background/pty). Returns {\"output\": \"...\", \"exit_code\": N}",
}

// stubModule is the source of son_of_anton_tools.py with a stub for each of
// the named tools.
func stubModule(names []string) string {
	var b strings.Builder
	b.WriteString(stubHeader)
	for _, n := range names {
		s := toolStubs[n]
		fmt.Fprintf(&b, "\ndef %s(%s):\n    %s\n    return _call(%q, %s)\n", n, s.sig, s.doc, n, s.args)
	}
	return b.String()
}

const stubHeader = `"""Auto-generated Son of Anton tools RPC stubs."""
import json, os, socket, shlex, threading, time

_sock = None
# The RPC server handles a single client connection serially and has no
# request-id in the protocol, so concurrent _call() invocations from multiple
# threads (e.g. ThreadPoolExecutor) would race on the shared socket and get
# each other's responses. Serialize the entire send+recv round-trip.
_call_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Convenience helpers (avoid common scripting pitfalls)
# ---------------------------------------------------------------------------

def json_parse(text: str):
    """Parse JSON tolerant of control characters and UTF-8 BOM (strict=False).
    Use this instead of json.loads() when parsing output from terminal()
    or web_extract() that may contain raw tabs/newlines in strings,
    or from tools/files that prepend a UTF-8 BOM."""
    if isinstance(text, str) and text.startswith("\ufeff"):
        text = text[1:]
    return json.loads(text, strict=False)


def shell_quote(s: str) -> str:
    """Shell-escape a string for safe interpolation into commands.
    Use this when inserting dynamic content into terminal() commands:
        terminal(f"echo {shell_quote(user_input)}")
    """
    return shlex.quote(s)


def retry(fn, max_attempts=3, delay=2):
    """Retry a function up to max_attempts times with exponential backoff.
    Use for transient failures (network errors, API rate limits):
        result = retry(lambda: terminal("gh issue list ..."))
    """
    last_err = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < max_attempts - 1:
                time.sleep(delay * (2 ** attempt))
    raise last_err


def _connect():
    """Connect to the parent's RPC server via the transport it picked.

    SON_OF_ANTON_RPC_SOCKET is a filesystem path (Unix domain socket).
    """
    global _sock
    if _sock is None:
        endpoint = os.environ["SON_OF_ANTON_RPC_SOCKET"]
        _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        _sock.connect(endpoint)
        _sock.settimeout(300)
    return _sock

def _call(tool_name, args):
    """Send a tool call to the parent process and return the parsed result."""
    request = json.dumps({
        "tool": tool_name,
        "args": args,
        "token": os.environ.get("SON_OF_ANTON_RPC_TOKEN", ""),
    }) + "\n"
    with _call_lock:
        conn = _connect()
        conn.sendall(request.encode())
        buf = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                raise RuntimeError("Agent process disconnected")
            buf += chunk
            if buf.endswith(b"\n"):
                break
    raw = buf.decode().strip()
    result = json.loads(raw)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
    return result

`
