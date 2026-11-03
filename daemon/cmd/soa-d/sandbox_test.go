package main

// Phase 11b: soa-d sandbox-run. The refusals need no bwrap; the run skips,
// saying why, where bwrap cannot create its namespaces.

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/sandbox/sandboxtest"
)

type sbxEnv struct{ home, run, data, other string }

// sbxHome is a home whose config names the runtime, and a run dir, a data dir
// under physics.data_dirs and another outside it.
func sbxHome(t *testing.T, python string) sbxEnv {
	t.Helper()
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	e := sbxEnv{home: filepath.Join(root, "home"), run: filepath.Join(root, "run"),
		data: filepath.Join(root, "labdata", "set1"), other: filepath.Join(root, "other")}
	for _, d := range []string{e.home, e.run, e.data, e.other} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(e.data, "events.txt"), []byte("DATAFILE"), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := "[physics]\ndata_dirs = [\"" + filepath.Join(root, "labdata") + "\"]\n"
	if python != "" {
		cfg += "python = \"" + python + "\"\n"
	}
	if err := os.WriteFile(filepath.Join(e.home, "config.toml"), []byte(cfg), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SON_OF_ANTON_HOME", "")
	return e
}

func sbxRun(args ...string) (int, string, string) {
	var out, errOut bytes.Buffer
	code := run(append([]string{"sandbox-run"}, args...), &out, &errOut)
	return code, out.String(), errOut.String()
}

func TestSandboxRunRefusesDataOutsideTheConfiguredDirs(t *testing.T) {
	e := sbxHome(t, "/rt/bin/python3")
	code, out, errOut := sbxRun("--home", e.home, "--run", e.run, "--data", e.other, "--", "true")
	if code == 0 || out != "" || !strings.Contains(errOut, "physics.data_dirs") {
		t.Fatalf("code %d, stdout %q, stderr %q", code, out, errOut)
	}
	// A path that climbs out of a data dir is outside it.
	code, _, errOut = sbxRun("--home", e.home, "--run", e.run, "--data", filepath.Join(e.data, "..", "..", "other"), "--", "true")
	if code == 0 || !strings.Contains(errOut, "physics.data_dirs") {
		t.Fatalf("code %d, stderr %q", code, errOut)
	}
}

func TestSandboxRunRefusesWithoutAPython(t *testing.T) {
	e := sbxHome(t, "")
	code, out, errOut := sbxRun("--home", e.home, "--run", e.run, "--", "true")
	if code == 0 || out != "" || !strings.Contains(errOut, "physics.python") {
		t.Fatalf("code %d, stdout %q, stderr %q", code, out, errOut)
	}
}

func TestSandboxRunRefusesAMissingRunDir(t *testing.T) {
	e := sbxHome(t, "/rt/bin/python3")
	code, _, errOut := sbxRun("--home", e.home, "--run", filepath.Join(e.run, "nope"), "--", "true")
	if code == 0 || !strings.Contains(errOut, "--run") {
		t.Fatalf("code %d, stderr %q", code, errOut)
	}
}

func TestSandboxRunNeedsACommand(t *testing.T) {
	e := sbxHome(t, "/rt/bin/python3")
	if code, _, _ := sbxRun("--home", e.home, "--run", e.run); code != 2 {
		t.Fatalf("code %d, want the usage exit 2", code)
	}
}

func TestSandboxRunRunsTheCommandInTheSandbox(t *testing.T) {
	rt := sandboxtest.Runtime(t)
	e := sbxHome(t, filepath.Join(rt, "python3"))
	code, out, errOut := sbxRun("--home", e.home, "--run", e.run, "--data", e.data, "--",
		"bash", "-c", "cat "+filepath.Join(e.data, "events.txt")+"; echo; echo $PATH; echo ok > made.txt; exit 7")
	if code != 7 {
		t.Fatalf("exit %d, want the command's 7 (stderr %q)", code, errOut)
	}
	if out != "DATAFILE\n"+rt+"\n" {
		t.Errorf("stdout = %q", out)
	}
	if b, err := os.ReadFile(filepath.Join(e.run, "made.txt")); err != nil || string(b) != "ok\n" {
		t.Errorf("made.txt: %q, %v", b, err)
	}
	// A bare command name is looked up on the runtime's PATH.
	if code, out, _ := sbxRun("--home", e.home, "--run", e.run, "--", "echo", "hi"); code != 0 || out != "hi\n" {
		t.Errorf("echo: code %d, stdout %q", code, out)
	}
}
