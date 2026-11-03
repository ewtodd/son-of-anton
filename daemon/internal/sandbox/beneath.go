package sandbox

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"
)

// FS is how the daemon's own file tools touch the filesystem for a sandboxed
// session. The sandboxed process shares the run directory with the daemon and
// can swap a symlink there at any moment, so no check-then-open is safe. Every
// open here is one openat2 call relative to an open root directory with
// RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS: the kernel refuses, atomically,
// any path that leaves the root, whatever the filesystem looks like at that
// instant. No path string is ever opened directly, and when openat2 is not
// available the operation is refused: there is no fallback to a plain open.
type FS struct {
	read  []string          // roots a read may lie under, in every form they are named
	alias map[string]string // a named root that is a symlink -> its target, which is what is opened
	write string            // the root a write may lie under
}

// FS is the confined file access of this policy: reads beneath RunDir, the
// data directories and the Nix store, writes beneath RunDir.
func (p Policy) FS() *FS {
	f := &FS{alias: map[string]string{}}
	if !p.ReadOnly {
		f.write = resolveRoot(p.RunDir)
	}
	for _, r := range p.ReadRoots() {
		c, real := filepath.Clean(r), resolveRoot(r)
		f.read = append(f.read, c)
		if real != c {
			f.alias[c] = real
			f.read = append(f.read, real)
		}
	}
	return f
}

func resolveRoot(r string) string {
	if real, err := filepath.EvalSymlinks(r); err == nil {
		return real
	}
	return filepath.Clean(r)
}

const resolve = unix.RESOLVE_BENEATH | unix.RESOLVE_NO_MAGICLINKS

// split finds the root path lies under (lexically: the open itself is what
// confines) and the path relative to it.
func split(roots []string, path string) (root, rel string, err error) {
	if !filepath.IsAbs(path) {
		return "", "", fmt.Errorf("%q is not an absolute path", path)
	}
	path = filepath.Clean(path)
	for _, r := range roots {
		if path == r {
			return r, ".", nil
		}
		if strings.HasPrefix(path, r+"/") {
			return r, path[len(r)+1:], nil
		}
	}
	return "", "", fmt.Errorf("%s is outside the paths this session may use (%s)", path, strings.Join(roots, ", "))
}

func openRoot(root string) (int, error) {
	fd, err := unix.Open(root, unix.O_PATH|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return -1, err
	}
	return fd, nil
}

// at opens rel beneath dirfd.
func at(dirfd int, rel string, flags int, mode uint32) (int, error) {
	fd, err := unix.Openat2(dirfd, rel, &unix.OpenHow{Flags: uint64(flags | unix.O_CLOEXEC), Mode: uint64(mode), Resolve: resolve})
	switch {
	case err == nil:
		return fd, nil
	case errors.Is(err, unix.ENOSYS):
		return -1, errors.New("refused: the kernel has no openat2, so files cannot be opened safely beneath the sandbox's roots")
	case errors.Is(err, unix.EXDEV):
		return -1, fmt.Errorf("refused: %s leaves the sandbox's roots (a symlink points out of them)", rel)
	}
	return -1, &os.PathError{Op: "open", Path: rel, Err: err}
}

// open opens path (beneath one of roots) and wraps the descriptor.
func (f *FS) openRead(path string, flags int, mode uint32) (*os.File, error) {
	root, rel, err := split(f.read, path)
	if err != nil {
		return nil, err
	}
	if real, ok := f.alias[root]; ok {
		root = real
	}
	rfd, err := openRoot(root)
	if err != nil {
		return nil, err
	}
	defer unix.Close(rfd)
	fd, err := at(rfd, rel, flags, mode)
	if err != nil {
		return nil, err
	}
	return os.NewFile(uintptr(fd), path), nil
}

// ReadFile reads a file beneath a read root.
func (f *FS) ReadFile(path string) ([]byte, error) {
	file, err := f.openRead(path, unix.O_RDONLY, 0)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	return io.ReadAll(file)
}

// Stat is os.Stat for a path beneath a read root.
func (f *FS) Stat(path string) (os.FileInfo, error) {
	file, err := f.openRead(path, unix.O_PATH, 0)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	return file.Stat()
}

// ReadDir lists a directory beneath a read root.
func (f *FS) ReadDir(path string) ([]os.DirEntry, error) {
	file, err := f.openRead(path, unix.O_RDONLY|unix.O_DIRECTORY, 0)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	return file.ReadDir(-1)
}

// WriteAtomic writes content to path beneath the write root through a temp
// file in the same directory and a renameat, creating the parent directories
// (0755) and keeping an existing file's mode. Every step is relative to a
// directory descriptor opened beneath the root. It returns the bytes written
// and whether a directory had to be created.
func (f *FS) WriteAtomic(path, content string) (int, bool, error) {
	dirfd, base, created, err := f.parent(path)
	if err != nil {
		return 0, false, err
	}
	defer unix.Close(dirfd)

	mode := os.FileMode(0o644)
	switch efd, err := at(dirfd, base, unix.O_PATH, 0); {
	case err == nil:
		var st unix.Stat_t
		serr := unix.Fstat(efd, &st)
		unix.Close(efd)
		if serr != nil {
			return 0, false, serr
		}
		mode = os.FileMode(st.Mode & 0o777)
	case errors.Is(err, unix.ENOENT):
	default:
		return 0, false, err
	}

	var rnd [6]byte
	_, _ = rand.Read(rnd[:])
	tmpName := ".soa-d-write-" + hex.EncodeToString(rnd[:])
	tfd, err := at(dirfd, tmpName, unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW, 0o600)
	if err != nil {
		return 0, false, err
	}
	tmp := os.NewFile(uintptr(tfd), tmpName)
	cleanup := func() { _ = unix.Unlinkat(dirfd, tmpName, 0) }
	if _, err := tmp.WriteString(content); err != nil {
		tmp.Close()
		cleanup()
		return 0, false, err
	}
	if err := tmp.Chmod(mode); err != nil {
		tmp.Close()
		cleanup()
		return 0, false, err
	}
	if err := tmp.Close(); err != nil {
		cleanup()
		return 0, false, err
	}
	if err := unix.Renameat(dirfd, tmpName, dirfd, base); err != nil {
		cleanup()
		return 0, false, &os.PathError{Op: "rename", Path: path, Err: err}
	}
	return len(content), created, nil
}

// parent opens the directory that holds path beneath the write root, creating
// missing directories (0755), and returns its descriptor (the caller closes
// it), the file's name in it, and whether a directory was created.
func (f *FS) parent(path string) (dirfd int, base string, created bool, err error) {
	if f.write == "" {
		return -1, "", false, errors.New("this session may not write")
	}
	root, rel, err := split([]string{f.write}, path)
	if err != nil {
		return -1, "", false, err
	}
	if rel == "." {
		return -1, "", false, fmt.Errorf("%s is the run directory itself", path)
	}
	dirfd, err = openRoot(root)
	if err != nil {
		return -1, "", false, err
	}
	dir, base := filepath.Split(rel)
	for _, comp := range strings.Split(strings.Trim(dir, "/"), "/") {
		if comp == "" {
			continue
		}
		if err := unix.Mkdirat(dirfd, comp, 0o755); err == nil {
			created = true
		} else if !errors.Is(err, unix.EEXIST) {
			unix.Close(dirfd)
			return -1, "", false, &os.PathError{Op: "mkdir", Path: comp, Err: err}
		}
		next, err := at(dirfd, comp, unix.O_PATH|unix.O_DIRECTORY, 0)
		unix.Close(dirfd)
		if err != nil {
			return -1, "", false, err
		}
		dirfd = next
	}
	return dirfd, base, created, nil
}

// Append appends data to path beneath the write root, creating the file (0644)
// and its parent directories. A symlink at the file's own name is refused
// (O_NOFOLLOW), as one in a directory on the way is by the beneath resolution.
func (f *FS) Append(path, data string) error {
	dirfd, base, _, err := f.parent(path)
	if err != nil {
		return err
	}
	defer unix.Close(dirfd)
	fd, err := at(dirfd, base, unix.O_WRONLY|unix.O_CREAT|unix.O_APPEND|unix.O_NOFOLLOW, 0o644)
	if err != nil {
		return err
	}
	file := os.NewFile(uintptr(fd), base)
	defer file.Close()
	_, err = file.WriteString(data)
	return err
}
