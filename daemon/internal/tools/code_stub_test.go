package tools

import (
	"strings"
	"testing"
)

func TestStubModuleHasEachToolWithItsParameters(t *testing.T) {
	src := stubModule(sandboxTools)
	for _, want := range []string{
		"def web_search(query: str, limit: int = 5):",
		"def web_extract(urls: list, char_limit: int = None):",
		"def read_file(path: str, offset: int = 1, limit: int = 2000):",
		"def write_file(path: str, content: str):",
		`def search_files(pattern: str, target: str = "content", path: str = ".", file_glob: str = None, limit: int = 50, offset: int = 0, output_mode: str = "content", context: int = 0):`,
		"def patch(path: str, old_string: str, new_string: str, replace_all: bool = False):",
		"def terminal(command: str, timeout: int = None, workdir: str = None):",
		`_call("terminal", {"command": command, "timeout": timeout, "workdir": workdir})`,
		"def json_parse(", "def shell_quote(", "def retry(fn, max_attempts=3, delay=2):",
	} {
		if !strings.Contains(src, want) {
			t.Errorf("the module lacks %s", want)
		}
	}
	if few := stubModule([]string{"terminal"}); strings.Contains(few, "def web_search(") {
		t.Error("a tool that was not asked for is in the module")
	}
}
