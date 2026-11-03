package agent_test

import (
	"fmt"
	"os"
	"testing"
)

// TestMain gives the package's tests a temp directory of their own and removes
// it afterwards. A session makes a private temp dir that only Close removes,
// and many tests here never close theirs: without this, every run left dozens
// of soa-d-session-* directories in the real /tmp.
func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "soa-d-agent-tests-")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	os.Setenv("TMPDIR", dir)
	code := m.Run()
	os.RemoveAll(dir)
	os.Exit(code)
}
