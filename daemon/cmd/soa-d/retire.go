package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/ewtodd/son-of-anton/daemon/internal/config"
)

// retirePython is the start-up step of a soa-d instance ([daemon]
// import_python): import the home's Python state.db into soa-d.db (sessions
// already there are skipped), then rename it, and its -wal and -shm, to
// *.imported so it is kept but never read again. An error leaves state.db
// where it is; the caller serves anyway. Without the setting, or without a
// state.db, nothing happens.
func retirePython(home string, stderr io.Writer) {
	cfg, err := config.Load(home)
	if err != nil || !cfg.Daemon.ImportPython {
		return
	}
	src := filepath.Join(home, "state.db")
	if _, err := os.Stat(src); err != nil {
		return
	}
	res, err := importSessions(context.Background(), home, home)
	if err != nil {
		fmt.Fprintf(stderr, "soa-d serve: importing %s: %v (left in place)\n", src, err)
		return
	}
	for _, suffix := range []string{"", "-wal", "-shm"} {
		from := src + suffix
		if err := os.Rename(from, from+".imported"); err != nil && !errors.Is(err, os.ErrNotExist) {
			fmt.Fprintf(stderr, "soa-d serve: retiring %s: %v\n", from, err)
			return
		}
	}
	fmt.Fprintf(stderr, "soa-d serve: imported %d sessions (%d messages) from %s; it is now %s.imported\n",
		res.imported, res.messages, src, src)
}
