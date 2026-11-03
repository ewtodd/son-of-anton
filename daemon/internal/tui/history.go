package tui

import (
	"bytes"
	"io"
	"os"
	"strings"
	"time"
)

const (
	historyTail    = 512 << 10
	historyEntries = 500
)

// ReadHistory reads prompt_toolkit's history file (shared with the Textual
// TUI), oldest entry first: the last 512 KiB at most, the newest 500 entries.
func ReadHistory(path string) []string {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	var data []byte
	if fi, err := f.Stat(); err == nil && fi.Size() > historyTail {
		if _, err := f.Seek(fi.Size()-historyTail, io.SeekStart); err != nil {
			return nil
		}
		if data, err = io.ReadAll(f); err != nil {
			return nil
		}
		// The cut may fall inside an entry: start at the first header after it.
		i := bytes.Index(data, []byte("\n#"))
		if i < 0 {
			return nil
		}
		data = data[i:]
	} else if data, err = io.ReadAll(f); err != nil {
		return nil
	}
	var entries, cur []string
	flush := func() {
		if len(cur) > 0 {
			entries = append(entries, strings.Join(cur, "\n"))
			cur = nil
		}
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(line, "+") {
			cur = append(cur, line[1:])
		} else {
			flush()
		}
	}
	flush()
	if len(entries) > historyEntries {
		entries = entries[len(entries)-historyEntries:]
	}
	return entries
}

// AppendHistory appends one entry in prompt_toolkit's format, creating the
// file 0600.
func AppendHistory(path, entry string) error {
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	var b strings.Builder
	b.WriteString("\n# " + time.Now().Format("2006-01-02 15:04:05.000000") + "\n")
	for _, line := range strings.Split(entry, "\n") {
		b.WriteString("+" + line + "\n")
	}
	_, werr := f.WriteString(b.String())
	if cerr := f.Close(); werr == nil {
		werr = cerr
	}
	return werr
}
