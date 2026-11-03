package physics

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// Gitignore is the run directory's .gitignore.
const Gitignore = ".home/\n__pycache__/\n*.root\n*.npy\n*.npz\n*.h5\n*.bin\n*.pkl\n"

// Repo is a run directory's git repository. Its git directory lives outside
// the run directory (the run directory's .git is a file pointing at it): the
// sandboxed worker can write the run directory, and a hook or filter planted
// in a .git inside it would otherwise run on the host when the daemon commits.
type Repo struct {
	Dir, GitDir, Name, Email string
}

func (r Repo) env() []string {
	return append(os.Environ(),
		"GIT_DIR="+r.GitDir, "GIT_WORK_TREE="+r.Dir,
		"GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_SYSTEM=/dev/null", "GIT_CONFIG_NOSYSTEM=1",
		"GIT_TERMINAL_PROMPT=0", "GIT_OPTIONAL_LOCKS=0",
		"GIT_AUTHOR_NAME="+r.Name, "GIT_AUTHOR_EMAIL="+r.Email,
		"GIT_COMMITTER_NAME="+r.Name, "GIT_COMMITTER_EMAIL="+r.Email,
	)
}

func (r Repo) git(args ...string) error {
	cmd := exec.Command("git", append([]string{"-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"}, args...)...)
	cmd.Dir = r.Dir
	cmd.Env = r.env()
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("git %s: %v: %s", strings.Join(args, " "), err, strings.TrimSpace(string(out)))
	}
	return nil
}

// Init makes the repository, with a repo-local identity.
func (r Repo) Init() error {
	cmd := exec.Command("git", "init", "-q", "-b", "main", "--separate-git-dir", r.GitDir, r.Dir)
	cmd.Env = append(os.Environ(), "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_NOSYSTEM=1", "GIT_TERMINAL_PROMPT=0")
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("git init: %v: %s", err, strings.TrimSpace(string(out)))
	}
	if err := r.git("config", "user.name", r.Name); err != nil {
		return err
	}
	return r.git("config", "user.email", r.Email)
}

// Commit commits everything in the run directory (ignored files aside).
func (r Repo) Commit(message string) error {
	if err := r.git("add", "-A"); err != nil {
		return err
	}
	return r.git("commit", "-q", "--allow-empty", "--no-verify", "-m", message)
}
