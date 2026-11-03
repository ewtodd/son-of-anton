package signal

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const lockScope = "signal-phone"

// HeldError says another live process holds the account.
type HeldError struct{ PID int }

func (e *HeldError) Error() string {
	return fmt.Sprintf("signal: the account is in use by process %d (another gateway)", e.PID)
}

// lockDir is where the per-account lock files live.
func lockDir() (string, error) {
	if d := os.Getenv("SON_OF_ANTON_GATEWAY_LOCK_DIR"); d != "" {
		return d, nil
	}
	state := os.Getenv("XDG_STATE_HOME")
	if state == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		state = filepath.Join(home, ".local", "state")
	}
	return filepath.Join(state, "son-of-anton", "gateway-locks"), nil
}

func identityHash(account string) string {
	sum := sha256.Sum256([]byte(account))
	return hex.EncodeToString(sum[:])[:16]
}

// startTime is field 22 of /proc/<pid>/stat: a process's start time in clock
// ticks since boot, which tells a recycled pid from the original. ok is false
// when it cannot be read.
func startTime(pid int) (int64, bool) {
	b, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return 0, false
	}
	s := string(b)
	i := strings.LastIndexByte(s, ')')
	if i < 0 {
		return 0, false
	}
	fields := strings.Fields(s[i+1:]) // from field 3 on
	if len(fields) < 20 {
		return 0, false
	}
	v, err := strconv.ParseInt(fields[19], 10, 64)
	return v, err == nil
}

func pidAlive(pid int) bool {
	err := syscall.Kill(pid, 0)
	return err == nil || errors.Is(err, syscall.EPERM)
}

// AcquireLock takes the machine-local lock for a Signal account, so two
// processes never serve one account at once. A *HeldError means a live process holds it. The
// release function removes the lock if it is still ours.
func AcquireLock(account string) (release func(), err error) {
	dir, err := lockDir()
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	path := filepath.Join(dir, lockScope+"-"+identityHash(account)+".lock")
	pid := os.Getpid()
	rec := map[string]any{
		"pid": pid, "kind": "son-of-anton-gateway", "argv": os.Args,
		"scope": lockScope, "identity_hash": identityHash(account),
		"metadata": map[string]any{"platform": "signal"}, "updated_at": time.Now().UTC().Format("2006-01-02T15:04:05.000000+00:00"),
	}
	if st, ok := startTime(pid); ok {
		rec["start_time"] = st
	}
	body, err := json.Marshal(rec)
	if err != nil {
		return nil, err
	}
	release = func() {
		var cur struct {
			PID int `json:"pid"`
		}
		if b, err := os.ReadFile(path); err == nil && json.Unmarshal(b, &cur) == nil && cur.PID == pid {
			_ = os.Remove(path)
		}
	}

	for range 3 {
		b, err := os.ReadFile(path)
		if err == nil {
			var cur struct {
				PID       int    `json:"pid"`
				StartTime *int64 `json:"start_time"`
			}
			if json.Unmarshal(b, &cur) == nil && cur.PID > 0 {
				if cur.PID == pid {
					return release, os.WriteFile(path, body, 0o600)
				}
				if holds(cur.PID, cur.StartTime) {
					return nil, &HeldError{PID: cur.PID}
				}
			}
			// Stale: claim it by renaming, so of two starters one wins.
			tomb := path + ".stale"
			if os.Rename(path, tomb) == nil {
				_ = os.Remove(tomb)
			}
		} else if !os.IsNotExist(err) {
			return nil, err
		}
		f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
		if errors.Is(err, os.ErrExist) {
			continue
		}
		if err != nil {
			return nil, err
		}
		_, werr := f.Write(body)
		if cerr := f.Close(); werr == nil {
			werr = cerr
		}
		if werr != nil {
			_ = os.Remove(path)
			return nil, werr
		}
		return release, nil
	}
	return nil, errors.New("signal: could not take the account lock")
}

// holds says whether a lock record names a process that is still the one that
// wrote it: alive, and with the same start time when both are known.
func holds(pid int, recorded *int64) bool {
	if !pidAlive(pid) {
		return false
	}
	if cur, ok := startTime(pid); ok && recorded != nil && cur != *recorded {
		return false
	}
	return true
}
