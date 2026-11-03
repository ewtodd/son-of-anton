package nixmerge

import (
	"errors"
	"fmt"
	"strings"
	"syscall"
)

// pyOSError is how Python words an OSError: "[Errno 2] No such file or
// directory: 'path'".
func pyOSError(err error, path string) string {
	var errno syscall.Errno
	if !errors.As(err, &errno) {
		return err.Error()
	}
	msg := errno.Error()
	if msg != "" {
		msg = strings.ToUpper(msg[:1]) + msg[1:]
	}
	return fmt.Sprintf("[Errno %d] %s: %s", int(errno), msg, pyRepr(path))
}

func pyRepr(s string) string {
	s = strings.ReplaceAll(s, `\`, `\\`)
	if strings.Contains(s, "'") && !strings.Contains(s, `"`) {
		return `"` + s + `"`
	}
	return "'" + strings.ReplaceAll(s, "'", `\'`) + "'"
}

// pyDecodeError is the UnicodeDecodeError message of Python's utf-8 codec
// for the first bad sequence in b.
func pyDecodeError(b []byte) string {
	for i := 0; i < len(b); {
		c := b[i]
		if c < 0x80 {
			i++
			continue
		}
		need := 0
		switch {
		case c >= 0xc2 && c <= 0xdf:
			need = 2
		case c >= 0xe0 && c <= 0xef:
			need = 3
		case c >= 0xf0 && c <= 0xf4:
			need = 4
		default:
			return decodeMsg(b, i, 1, "invalid start byte")
		}
		k := 1
		for j := 1; j < need; j++ {
			if i+j >= len(b) {
				return decodeMsg(b, i, k, "unexpected end of data")
			}
			lo, hi := byte(0x80), byte(0xbf)
			if j == 1 {
				switch c {
				case 0xe0:
					lo = 0xa0
				case 0xed:
					hi = 0x9f
				case 0xf0:
					lo = 0x90
				case 0xf4:
					hi = 0x8f
				}
			}
			if b[i+j] < lo || b[i+j] > hi {
				return decodeMsg(b, i, k, "invalid continuation byte")
			}
			k++
		}
		i += need
	}
	return "invalid UTF-8"
}

func decodeMsg(b []byte, pos, n int, reason string) string {
	if n == 1 {
		return fmt.Sprintf("'utf-8' codec can't decode byte 0x%02x in position %d: %s", b[pos], pos, reason)
	}
	return fmt.Sprintf("'utf-8' codec can't decode bytes in position %d-%d: %s", pos, pos+n-1, reason)
}
