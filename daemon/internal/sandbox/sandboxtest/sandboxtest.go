// Package sandboxtest is the test support for the sandbox: a stand-in runtime
// built from the host's own tools, and the skip when bwrap cannot create its
// namespaces where the tests run.
package sandboxtest

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/sandbox"
)

// tools are linked into the stand-in runtime's bin/.
var tools = []string{
	"bash", "sh", "env", "mv", "cat", "ls", "mkdir", "rm", "touch", "sleep", "echo", "pwd", "head",
	"sed", "grep", "printf", "test", "true", "false", "id", "readlink", "dirname", "basename", "tr", "wc",
}

// Runtime returns a directory standing in for the physics runtime's bin/, its
// entries symlinks to the host's tools. It skips the test, saying why, when
// bwrap is missing or cannot create its namespaces here, or when the host's
// tools do not live in the Nix store (the sandbox can only see that).
func Runtime(t *testing.T) string {
	t.Helper()
	if _, err := exec.LookPath("bwrap"); err != nil {
		t.Skip("bwrap is not on PATH, so the sandbox cannot run here")
	}
	// The directory is resolved: the sandbox binds it at its resolved path.
	dir, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	bin := filepath.Join(dir, "bin")
	if err := os.Mkdir(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, name := range tools {
		p, err := exec.LookPath(name)
		if err != nil {
			t.Skipf("no %s on PATH to build a stand-in runtime from", name)
		}
		if !strings.HasPrefix(p, sandbox.StorePath+"/") {
			t.Skipf("%s is %s, outside %s, which is all the sandbox shows a command", name, p, sandbox.StorePath)
		}
		if err := os.Symlink(p, filepath.Join(bin, name)); err != nil {
			t.Fatal(err)
		}
	}
	probe := sandbox.Policy{RunDir: filepath.Join(dir, "probe"), Runtime: bin, Home: filepath.Join(dir, "probe", ".home")}
	if err := os.Mkdir(probe.RunDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := probe.Command(nil, probe.Bash(), "-c", ":"); err != nil {
		t.Skipf("bwrap cannot create its namespaces here: %v", err)
	}
	return bin
}
