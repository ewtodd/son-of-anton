package tools

import (
	"strings"
	"testing"
)

// The Python functions are the tools' names in snake case, taking the tools'
// own parameter names; Grep's dashed flags, which Python cannot name, are
// spelled out and sent under the tool's names.
func TestStubModuleHasEachToolWithItsParameters(t *testing.T) {
	src := stubModule(sandboxTools)
	for _, want := range []string{
		"def web_search(query: str, allowed_domains: list = None, blocked_domains: list = None):",
		`def web_fetch(url: str, prompt: str = ""):`,
		"def read(file_path: str, offset: int = None, limit: int = None):",
		"def write(file_path: str, content: str):",
		"def glob(pattern: str, path: str = None):",
		`def grep(pattern: str, path: str = None, glob: str = None, type: str = None, output_mode: str = "files_with_matches", ignore_case: bool = False, line_numbers: bool = True, after: int = None, before: int = None, context: int = None, multiline: bool = False, head_limit: int = None):`,
		`"-i": ignore_case, "-n": line_numbers, "-A": after, "-B": before, "-C": context`,
		"def edit(file_path: str, old_string: str, new_string: str, replace_all: bool = False):",
		"def bash(command: str, timeout: int = None, description: str = None):",
		`_call("Bash", {"command": command, "timeout": timeout, "description": description})`,
		`_call("Read", {"file_path": file_path, "offset": offset, "limit": limit})`,
		"def json_parse(", "def shell_quote(", "def retry(fn, max_attempts=3, delay=2):",
	} {
		if !strings.Contains(src, want) {
			t.Errorf("the module lacks %s", want)
		}
	}
	if few := stubModule([]string{"Bash"}); strings.Contains(few, "def web_search(") {
		t.Error("a tool that was not asked for is in the module")
	}
}
