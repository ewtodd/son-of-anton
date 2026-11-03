// Package cron is the job store and the scheduling rules of soa-d's cron
// (daemon/phases/06-cron.md). The jobs file is the Python's cron/jobs.json,
// shared with `son-of-anton cron`: same format, same locks, and every field
// this package does not know survives each write.
package cron

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/BurntSushi/toml"
)

// Job is a record as stored; unknown fields survive every write.
type Job map[string]any

// ErrNoChange is what an Update function returns to leave the file as it is.
var ErrNoChange = errors.New("cron: no change")

const lockWait = 30 * time.Second

// errBusy means another process holds a lock.
var errBusy = errors.New("cron: locked by another process")

// Dir is <home>/cron.
func Dir(home string) string { return filepath.Join(home, "cron") }

// ensureDirs makes cron/ and cron/output/ (0700).
func ensureDirs(home string) error {
	for _, d := range []string{Dir(home), filepath.Join(Dir(home), "output")} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			return err
		}
		if err := os.Chmod(d, 0o700); err != nil {
			return err
		}
	}
	return nil
}

// lockFile takes an exclusive flock on cron/<name>, waiting up to wait.
func lockFile(home, name string, wait time.Duration) (release func(), err error) {
	if err := ensureDirs(home); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(filepath.Join(Dir(home), name), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	deadline := time.Now().Add(wait)
	for {
		err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			return func() {
				_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
				_ = f.Close()
			}, nil
		}
		if !errors.Is(err, syscall.EWOULDBLOCK) && !errors.Is(err, syscall.EINTR) {
			_ = f.Close()
			return nil, err
		}
		if time.Now().After(deadline) {
			_ = f.Close()
			return nil, fmt.Errorf("%w (waited %s for cron/%s)", errBusy, wait, name)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// TryTickLock takes cron/.tick.lock without blocking. ok is false when
// another scheduler holds it.
func TryTickLock(home string) (release func(), ok bool, err error) {
	release, err = lockFile(home, ".tick.lock", 0)
	if errors.Is(err, errBusy) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	return release, true, nil
}

// Load reads cron/jobs.json under the jobs lock.
func Load(home string) ([]Job, error) {
	release, err := lockFile(home, ".jobs.lock", lockWait)
	if err != nil {
		return nil, err
	}
	defer release()
	return readJobs(home)
}

// Update reads the jobs, passes them to f and writes what f returns, all
// under the jobs lock. An error from f writes nothing and is returned;
// ErrNoChange is returned as nil.
func Update(home string, f func([]Job) ([]Job, error)) error {
	release, err := lockFile(home, ".jobs.lock", lockWait)
	if err != nil {
		return err
	}
	defer release()
	jobs, err := readJobs(home)
	if err != nil {
		return err
	}
	jobs, err = f(jobs)
	if errors.Is(err, ErrNoChange) {
		return nil
	}
	if err != nil {
		return err
	}
	return writeJobs(home, jobs)
}

// readJobs parses the file: a BOM, a bare list and records keyed job_id are
// accepted. A missing file is no jobs.
func readJobs(home string) ([]Job, error) {
	b, err := os.ReadFile(filepath.Join(Dir(home), "jobs.json"))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("cron: reading the jobs file: %w", err)
	}
	b = bytes.TrimPrefix(b, []byte("\xef\xbb\xbf"))
	var top any
	if err := json.Unmarshal(b, &top); err != nil {
		return nil, fmt.Errorf("cron: the jobs file is corrupted: %w", err)
	}
	var list []any
	switch v := top.(type) {
	case map[string]any:
		if raw, ok := v["jobs"]; ok && raw != nil {
			if list, ok = raw.([]any); !ok {
				return nil, errors.New("cron: the jobs file is corrupted: jobs is not a list")
			}
		}
	case []any:
		list = v
	default:
		return nil, errors.New("cron: the jobs file is corrupted: expected {\"jobs\": [...]}")
	}
	jobs := make([]Job, 0, len(list))
	for _, x := range list {
		m, ok := x.(map[string]any)
		if !ok {
			return nil, errors.New("cron: the jobs file is corrupted: a job is not an object")
		}
		if id, _ := m["id"].(string); id == "" {
			if old, ok := m["job_id"].(string); ok && old != "" {
				m["id"] = old
				delete(m, "job_id")
			}
		}
		jobs = append(jobs, Job(m))
	}
	return jobs, nil
}

// writeJobs writes {"jobs": [...], "updated_at": now} the way the Python
// does: two-space indents, text unescaped, a temp file in cron/ renamed into
// place, mode 0600.
func writeJobs(home string, jobs []Job) error {
	if jobs == nil {
		jobs = []Job{}
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	file := map[string]any{"jobs": jobs, "updated_at": ISO(time.Now().In(Zone(home)))}
	if err := enc.Encode(file); err != nil {
		return err
	}
	out := bytes.TrimRight(buf.Bytes(), "\n")
	tmp, err := os.CreateTemp(Dir(home), ".jobs_*.tmp")
	if err != nil {
		return err
	}
	name := tmp.Name()
	fail := func(err error) error {
		_ = tmp.Close()
		_ = os.Remove(name)
		return err
	}
	if _, err := tmp.Write(out); err != nil {
		return fail(err)
	}
	if err := tmp.Sync(); err != nil {
		return fail(err)
	}
	if err := tmp.Close(); err != nil {
		return fail(err)
	}
	if err := os.Chmod(name, 0o600); err != nil {
		return fail(err)
	}
	if err := os.Rename(name, filepath.Join(Dir(home), "jobs.json")); err != nil {
		return fail(err)
	}
	return nil
}

// Zone is the configured time zone: SON_OF_ANTON_TIMEZONE, else the
// config.toml timezone, else the host's.
func Zone(home string) *time.Location {
	name := strings.TrimSpace(os.Getenv("SON_OF_ANTON_TIMEZONE"))
	if name == "" {
		var c struct {
			Timezone string `toml:"timezone"`
		}
		if _, err := toml.DecodeFile(filepath.Join(home, "config.toml"), &c); err == nil {
			name = strings.TrimSpace(c.Timezone)
		}
	}
	if name != "" {
		if loc, err := time.LoadLocation(name); err == nil {
			return loc
		}
	}
	return time.Local
}

// ISO is the Python's isoformat: seconds, microseconds when not zero, and a
// numeric offset.
func ISO(t time.Time) string { return t.Format("2006-01-02T15:04:05.999999-07:00") }

// ParseISO reads the forms the Python's fromisoformat takes (a trailing Z
// too). A time without an offset is in loc.
func ParseISO(s string, loc *time.Location) (time.Time, error) {
	s = strings.TrimSpace(s)
	for _, layout := range []string{
		"2006-01-02T15:04:05.999999999-07:00", "2006-01-02T15:04-07:00", "2006-01-02 15:04:05.999999999-07:00", "2006-01-02 15:04-07:00",
	} {
		if t, err := time.Parse(layout, strings.Replace(s, "Z", "+00:00", 1)); err == nil {
			return t, nil
		}
	}
	for _, layout := range []string{
		"2006-01-02T15:04:05.999999999", "2006-01-02T15:04", "2006-01-02 15:04:05.999999999", "2006-01-02 15:04", "2006-01-02T15", "2006-01-02",
	} {
		if t, err := time.ParseInLocation(layout, s, loc); err == nil {
			return t, nil
		}
	}
	return time.Time{}, fmt.Errorf("invalid isoformat string: %q", s)
}
