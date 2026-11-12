package tools

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// The file tools reach the filesystem through these. In a sandboxed session
// every open is confined beneath the sandbox's roots by the kernel
// (sandbox.FS), because the sandboxed process can swap a symlink between a
// path check and the open; elsewhere they are the plain os calls.

func envReadFile(env *Env, abs string) ([]byte, error) {
	if env.Sandbox != nil {
		return env.Sandbox.FS().ReadFile(abs)
	}
	return os.ReadFile(abs)
}

func envStat(env *Env, abs string) (os.FileInfo, error) {
	if env.Sandbox != nil {
		return env.Sandbox.FS().Stat(abs)
	}
	return os.Stat(abs)
}

func envReadDir(env *Env, dir string) ([]os.DirEntry, error) {
	if env.Sandbox != nil {
		return env.Sandbox.FS().ReadDir(dir)
	}
	return os.ReadDir(dir)
}

func envWriteAtomic(env *Env, abs, content string) (int, bool, error) {
	if env.Sandbox != nil {
		return env.Sandbox.FS().WriteAtomic(abs, content)
	}
	return writeAtomic(abs, content)
}

// rgCommand is ripgrep for Glob and Grep. In a sandboxed session it runs
// inside the sandbox, by its Nix store path (visible there), so symlinks are
// resolved in the namespace where only the allowed roots exist; if that is
// not possible there is no search, never one on the host.
func rgCommand(ctx context.Context, env *Env, args ...string) (*exec.Cmd, error) {
	rg, err := exec.LookPath("rg")
	if err != nil {
		return nil, errors.New("Glob and Grep run on ripgrep, and rg was not found")
	}
	if env.Sandbox == nil {
		return exec.CommandContext(ctx, "rg", args...), nil
	}
	if real, err := filepath.EvalSymlinks(rg); err == nil {
		rg = real
	}
	if !strings.HasPrefix(rg, "/nix/store/") {
		return nil, errors.New("Glob and Grep cannot run in the sandbox: ripgrep is " + rg + ", outside the Nix store")
	}
	return env.Sandbox.CommandContext(ctx, nil, append([]string{rg}, args...)...)
}
