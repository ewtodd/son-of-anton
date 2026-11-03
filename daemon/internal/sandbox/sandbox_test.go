package sandbox_test

// Phase 11b: the bubblewrap invocation. Args needs no bwrap; Command and the
// run tests need one that can create namespaces (they skip, saying why, where
// it cannot).

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/sandbox"
	"github.com/ewtodd/son-of-anton/daemon/internal/sandbox/sandboxtest"
)

func dirs(t *testing.T) (run, data, runtime string) {
	t.Helper()
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	run, data, runtime = filepath.Join(root, "run"), filepath.Join(root, "data"), filepath.Join(root, "rt", "bin")
	for _, d := range []string{run, data, runtime} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return
}

func index(args []string, want ...string) int {
	for i := 0; i+len(want) <= len(args); i++ {
		if strings.Join(args[i:i+len(want)], "\x00") == strings.Join(want, "\x00") {
			return i
		}
	}
	return -1
}

func TestArgsListWhatTheSpecSays(t *testing.T) {
	run, data, rt := dirs(t)
	tmp := t.TempDir()
	tmp, _ = filepath.EvalSymlinks(tmp)
	p := sandbox.Policy{RunDir: run, Data: []string{data}, Binds: []string{tmp}, Runtime: rt, Home: filepath.Join(run, ".home")}
	args, err := p.Args([]string{"SOA_MODEL=m"}, "/rt/bash", "-c", "x")
	if err != nil {
		t.Fatal(err)
	}
	for _, flag := range []string{"--die-with-parent", "--new-session", "--unshare-all", "--clearenv"} {
		if index(args, flag) < 0 {
			t.Errorf("missing %s in %v", flag, args)
		}
	}
	for _, seq := range [][]string{
		{"--ro-bind", "/nix/store", "/nix/store"},
		{"--proc", "/proc"}, {"--dev", "/dev"}, {"--tmpfs", "/tmp"},
		{"--symlink", rt + "/sh", "/bin/sh"}, {"--symlink", rt + "/env", "/usr/bin/env"},
		{"--ro-bind-try", "/etc/passwd", "/etc/passwd"}, {"--ro-bind-try", "/etc/group", "/etc/group"},
		{"--ro-bind-try", "/etc/localtime", "/etc/localtime"},
		{"--bind", run, run}, {"--ro-bind", data, data}, {"--bind", tmp, tmp},
		{"--setenv", "PATH", rt}, {"--setenv", "HOME", filepath.Join(run, ".home")},
		{"--setenv", "TMPDIR", "/tmp"}, {"--setenv", "LANG", "C.UTF-8"}, {"--setenv", "MPLBACKEND", "Agg"},
		{"--setenv", "SOA_MODEL", "m"}, {"--", "/rt/bash", "-c", "x"},
	} {
		if index(args, seq...) < 0 {
			t.Errorf("missing %v in %v", seq, args)
		}
	}
	// No network namespace escape hatch, and nothing of the host's home.
	for _, bad := range []string{"--share-net", "--unshare-user-try", "--dev-bind", "--bind-try"} {
		if index(args, bad) >= 0 {
			t.Errorf("%s must not appear", bad)
		}
	}
	// The data directory is read-only: no read-write bind of it.
	if index(args, "--bind", data, data) >= 0 {
		t.Error("the data directory is bound read-write")
	}
	// The sandbox's variables beat the caller's.
	args, _ = p.Args([]string{"PATH=/evil"}, "x")
	last := -1
	for i := range args {
		if args[i] == "--setenv" && args[i+1] == "PATH" {
			last = i
		}
	}
	if args[last+2] != rt {
		t.Errorf("PATH = %q, want the runtime", args[last+2])
	}
}

func TestArgsRefuseWhatIsNotSafe(t *testing.T) {
	run, data, rt := dirs(t)
	good := sandbox.Policy{RunDir: run, Data: []string{data}, Runtime: rt, Home: filepath.Join(run, ".home")}
	if _, err := good.Args(nil, "x"); err != nil {
		t.Fatalf("good policy: %v", err)
	}
	mut := func(f func(*sandbox.Policy)) sandbox.Policy {
		p := good
		p.Data = append([]string(nil), good.Data...)
		f(&p)
		return p
	}
	for name, p := range map[string]sandbox.Policy{
		"no run dir":        mut(func(p *sandbox.Policy) { p.RunDir = "" }),
		"relative run dir":  mut(func(p *sandbox.Policy) { p.RunDir = "run" }),
		"root as run dir":   mut(func(p *sandbox.Policy) { p.RunDir = "/" }),
		"missing run dir":   mut(func(p *sandbox.Policy) { p.RunDir = run + "/nope" }),
		"no runtime":        mut(func(p *sandbox.Policy) { p.Runtime = "" }),
		"root as data":      mut(func(p *sandbox.Policy) { p.Data = []string{"/"} }),
		"missing data":      mut(func(p *sandbox.Policy) { p.Data = []string{data + "/nope"} }),
		"data in run dir":   mut(func(p *sandbox.Policy) { p.Data = []string{filepath.Join(run, "d")} }),
		"data holds run":    mut(func(p *sandbox.Policy) { p.Data = []string{filepath.Dir(run)} }),
		"home outside run":  mut(func(p *sandbox.Policy) { p.Home = data }),
		"home sibling name": mut(func(p *sandbox.Policy) { p.Home = run + "-x" }),
		"no home":           mut(func(p *sandbox.Policy) { p.Home = "" }),
	} {
		if name == "data in run dir" {
			if err := os.Mkdir(filepath.Join(run, "d"), 0o755); err != nil {
				t.Fatal(err)
			}
		}
		_, err := p.Args(nil, "x")
		var ue *sandbox.UnavailableError
		if !errors.As(err, &ue) {
			t.Errorf("%s: err = %v, want an UnavailableError", name, err)
		}
	}
	if _, err := good.Args(nil); err == nil {
		t.Error("no command accepted")
	}
}

// A symlinked data path is bound at its target and, since the problem names
// it, at the named path too (phase 11c).
func TestSymlinkedPathsAreBoundAtTheirRealPath(t *testing.T) {
	run, data, rt := dirs(t)
	link := filepath.Join(filepath.Dir(run), "datalink")
	if err := os.Symlink(data, link); err != nil {
		t.Fatal(err)
	}
	p := sandbox.Policy{RunDir: run, Data: []string{link}, Runtime: rt, Home: filepath.Join(run, ".home")}
	args, err := p.Args(nil, "x")
	if err != nil {
		t.Fatal(err)
	}
	if index(args, "--ro-bind", data, data) < 0 || index(args, "--ro-bind", data, link) < 0 {
		t.Errorf("args = %v", args)
	}
}

func TestNoBwrapMeansNoCommand(t *testing.T) {
	run, _, rt := dirs(t)
	t.Setenv("PATH", t.TempDir())
	p := sandbox.Policy{RunDir: run, Runtime: rt, Home: filepath.Join(run, ".home")}
	cmd, err := p.Command(nil, "x")
	var ue *sandbox.UnavailableError
	if cmd != nil || !errors.As(err, &ue) || !strings.Contains(err.Error(), "sandbox is unavailable") || !strings.Contains(err.Error(), "bwrap") {
		t.Fatalf("cmd %v, err %v", cmd, err)
	}
}

func TestCommandRunsInsideTheSandbox(t *testing.T) {
	rt := sandboxtest.Runtime(t)
	root := filepath.Dir(rt)
	run, data := filepath.Join(root, "run"), filepath.Join(root, "data")
	for _, d := range []string{run, data} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	p := sandbox.Policy{RunDir: run, Data: []string{data}, Runtime: rt, Home: filepath.Join(run, ".home")}
	cmd, err := p.Command(nil, p.Bash(), "-c", "echo $PATH $HOME; pwd; cat /etc/hostname 2>&1 | head -1; echo hi > out.txt; echo x > "+data+"/w 2>&1; ls /bin/sh /usr/bin/env")
	if err != nil {
		t.Fatal(err)
	}
	out, _ := cmd.CombinedOutput()
	got := string(out)
	if !strings.HasPrefix(got, rt+" "+filepath.Join(run, ".home")+"\n"+run+"\n") {
		t.Errorf("output = %q", got)
	}
	if b, err := os.ReadFile(filepath.Join(run, "out.txt")); err != nil || string(b) != "hi\n" {
		t.Errorf("out.txt = %q, %v", b, err)
	}
	if _, err := os.Stat(filepath.Join(data, "w")); err == nil {
		t.Error("wrote into the read-only data directory")
	}
}
