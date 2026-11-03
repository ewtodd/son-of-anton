package nixmerge

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/BurntSushi/toml"

	"github.com/ewtodd/son-of-anton/daemon/internal/memory"
)

// tbl is a TOML table that remembers the order its keys were first set in,
// as the Python's dict does: a set of an existing key keeps its place, a new
// key goes last. Values are bool, int64, float64, string, time.Time, []any
// and *tbl.
type tbl struct {
	keys []string
	m    map[string]any
}

func newTbl() *tbl { return &tbl{m: map[string]any{}} }

func (t *tbl) get(k string) (any, bool) { v, ok := t.m[k]; return v, ok }

func (t *tbl) set(k string, v any) {
	if _, ok := t.m[k]; !ok {
		t.keys = append(t.keys, k)
	}
	t.m[k] = v
}

func (t *tbl) del(k string) {
	if _, ok := t.m[k]; !ok {
		return
	}
	delete(t.m, k)
	for i, x := range t.keys {
		if x == k {
			t.keys = append(t.keys[:i:i], t.keys[i+1:]...)
			break
		}
	}
}

func deepCopy(v any) any {
	switch x := v.(type) {
	case *tbl:
		c := newTbl()
		for _, k := range x.keys {
			c.set(k, deepCopy(x.m[k]))
		}
		return c
	case []any:
		c := make([]any, len(x))
		for i, e := range x {
			c[i] = deepCopy(e)
		}
		return c
	}
	return v
}

// truthy is Python's truth value of a decoded value.
func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case int64:
		return x != 0
	case float64:
		return x != 0
	case string:
		return x != ""
	case []any:
		return len(x) > 0
	case *tbl:
		return len(x.keys) > 0
	}
	return true
}

// num reads a bool, int or float as Python compares them.
func num(v any) (i int64, f float64, isFloat, ok bool) {
	switch x := v.(type) {
	case bool:
		if x {
			return 1, 1, false, true
		}
		return 0, 0, false, true
	case int64:
		return x, float64(x), false, true
	case float64:
		return 0, x, true, true
	}
	return 0, 0, false, false
}

// pyEq is Python's ==: 1 == 1.0 == True, dicts ignore order.
func pyEq(a, b any) bool {
	if ai, af, afl, ok := num(a); ok {
		bi, bf, bfl, ok := num(b)
		if !ok {
			return false
		}
		if afl || bfl {
			return af == bf
		}
		return ai == bi
	}
	switch x := a.(type) {
	case nil:
		return b == nil
	case string:
		y, ok := b.(string)
		return ok && x == y
	case time.Time:
		y, ok := b.(time.Time)
		return ok && x.Equal(y) && x.Location().String() == y.Location().String()
	case []any:
		y, ok := b.([]any)
		if !ok || len(x) != len(y) {
			return false
		}
		for i := range x {
			if !pyEq(x[i], y[i]) {
				return false
			}
		}
		return true
	case *tbl:
		y, ok := b.(*tbl)
		if !ok || len(x.keys) != len(y.keys) {
			return false
		}
		for k, v := range x.m {
			w, ok := y.m[k]
			if !ok || !pyEq(v, w) {
				return false
			}
		}
		return true
	}
	return false
}

// ---- reading ----

// parseTOML decodes TOML text, keeping the document order of every table.
func parseTOML(text string) (*tbl, error) {
	var raw map[string]any
	meta, err := toml.Decode(text, &raw)
	if err != nil {
		return nil, err
	}
	order := map[string][]string{}
	seen := map[string]bool{}
	for _, k := range meta.Keys() {
		// Every level counts: an implicit parent (the "a" of [a.b]) is never a
		// key of its own.
		for j := 1; j <= len(k); j++ {
			parent := strings.Join(k[:j-1], "\x00")
			id := parent + "\x01" + k[j-1]
			if !seen[id] {
				seen[id] = true
				order[parent] = append(order[parent], k[j-1])
			}
		}
	}
	return convert(raw, nil, order).(*tbl), nil
}

func convert(v any, path []string, order map[string][]string) any {
	switch x := v.(type) {
	case map[string]any:
		t := newTbl()
		done := map[string]bool{}
		for _, k := range order[strings.Join(path, "\x00")] {
			if _, ok := x[k]; ok && !done[k] {
				done[k] = true
				t.set(k, convert(x[k], append(path[:len(path):len(path)], k), order))
			}
		}
		var rest []string
		for k := range x {
			if !done[k] {
				rest = append(rest, k)
			}
		}
		sort.Strings(rest)
		for _, k := range rest {
			t.set(k, convert(x[k], append(path[:len(path):len(path)], k), order))
		}
		return t
	case []map[string]any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = convert(e, path, order)
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = convert(e, path, order)
		}
		return out
	}
	return v
}

// readUTF8 reads a file as Python's read_text(encoding="utf-8") does: an
// error is a *fileError carrying Python's message.
func readUTF8(path string) (string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", &fileError{pyOSError(err, path)}
	}
	if !utf8.Valid(b) {
		return "", &fileError{pyDecodeError(b)}
	}
	return string(b), nil
}

type fileError struct{ msg string }

func (e *fileError) Error() string { return e.msg }

// loadTOML reads and parses a TOML file.
func loadTOML(path string) (*tbl, error) {
	text, err := readUTF8(path)
	if err != nil {
		return nil, err
	}
	t, err := parseTOML(text)
	if err != nil {
		return nil, &tomlError{err.Error()}
	}
	return t, nil
}

type tomlError struct{ msg string }

func (e *tomlError) Error() string { return e.msg }

// loadState is the previous Nix output, nil when there is no usable state.
func loadState(path string) *tbl {
	if path == "" {
		return nil
	}
	text, err := readUTF8(path)
	if err != nil {
		return nil
	}
	dec := json.NewDecoder(strings.NewReader(text))
	dec.UseNumber()
	v, err := readJSON(dec)
	if err != nil {
		return nil
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil
	}
	t, _ := v.(*tbl)
	return t
}

func readJSON(dec *json.Decoder) (any, error) {
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	switch x := tok.(type) {
	case json.Delim:
		if x == '{' {
			t := newTbl()
			for dec.More() {
				kt, err := dec.Token()
				if err != nil {
					return nil, err
				}
				v, err := readJSON(dec)
				if err != nil {
					return nil, err
				}
				t.set(kt.(string), v)
			}
			_, err := dec.Token()
			return t, err
		}
		out := []any{}
		for dec.More() {
			v, err := readJSON(dec)
			if err != nil {
				return nil, err
			}
			out = append(out, v)
		}
		_, err := dec.Token()
		return out, err
	case json.Number:
		s := x.String()
		if !strings.ContainsAny(s, ".eE") {
			if n, err := strconv.ParseInt(s, 10, 64); err == nil {
				return n, nil
			}
		}
		f, err := strconv.ParseFloat(s, 64)
		return f, err
	}
	return tok, nil // string, bool, nil
}

// ---- writing TOML ----

func isArrayOfTables(v any) bool {
	l, ok := v.([]any)
	if !ok || len(l) == 0 {
		return false
	}
	for _, e := range l {
		if _, ok := e.(*tbl); !ok {
			return false
		}
	}
	return true
}

func tomlValue(v any) (string, error) {
	switch x := v.(type) {
	case *tbl:
		return "", errors.New("cannot serialize dict to TOML")
	case []any:
		parts := make([]string, len(x))
		for i, e := range x {
			s, err := tomlValue(e)
			if err != nil {
				return "", err
			}
			parts[i] = s
		}
		return "[" + strings.Join(parts, ", ") + "]", nil
	}
	s, ok := memory.TOMLValue(v)
	if !ok {
		return "", fmt.Errorf("cannot serialize %T to TOML", v)
	}
	return s, nil
}

func emitTable(lines *[]string, prefix []string, t *tbl) error {
	for _, k := range t.keys {
		v := t.m[k]
		if _, isTbl := v.(*tbl); isTbl || isArrayOfTables(v) {
			continue
		}
		if l, ok := v.([]any); ok {
			for _, e := range l {
				if _, ok := e.(*tbl); ok {
					return fmt.Errorf("mixed array at %v", append(prefix[:len(prefix):len(prefix)], k))
				}
			}
		}
		s, err := tomlValue(v)
		if err != nil {
			return err
		}
		*lines = append(*lines, memory.TOMLKey(k)+" = "+s)
	}
	for _, k := range t.keys {
		path := append(prefix[:len(prefix):len(prefix)], k)
		header := make([]string, len(path))
		for i, p := range path {
			header[i] = memory.TOMLKey(p)
		}
		switch v := t.m[k].(type) {
		case *tbl:
			*lines = append(*lines, "", "["+strings.Join(header, ".")+"]")
			if err := emitTable(lines, path, v); err != nil {
				return err
			}
		case []any:
			if !isArrayOfTables(v) {
				continue
			}
			for _, e := range v {
				*lines = append(*lines, "", "[["+strings.Join(header, ".")+"]]")
				if err := emitTable(lines, path, e.(*tbl)); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

// dumpTOML is the Python's dump_toml: scalars of a table first, then its
// sub-tables and arrays of tables, each under a header.
func dumpTOML(root *tbl) (string, error) {
	var lines []string
	if err := emitTable(&lines, nil, root); err != nil {
		return "", err
	}
	for len(lines) > 0 && lines[0] == "" {
		lines = lines[1:]
	}
	if len(lines) == 0 {
		return "", nil
	}
	return strings.Join(lines, "\n") + "\n", nil
}

// ---- writing JSON (Python's json.dump(indent=2, sort_keys=True)) ----

func dumpJSON(v any) (string, error) {
	var b strings.Builder
	if err := writeJSON(&b, v, 0); err != nil {
		return "", err
	}
	return b.String() + "\n", nil
}

func writeJSON(b *strings.Builder, v any, depth int) error {
	nl := "\n" + strings.Repeat("  ", depth+1)
	end := "\n" + strings.Repeat("  ", depth)
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		b.WriteString(strconv.FormatBool(x))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case float64:
		b.WriteString(pyFloat(x))
	case string:
		writeJSONString(b, x)
	case *tbl:
		if len(x.keys) == 0 {
			b.WriteString("{}")
			return nil
		}
		keys := append([]string(nil), x.keys...)
		sort.Strings(keys)
		b.WriteString("{")
		for i, k := range keys {
			if i > 0 {
				b.WriteString(",")
			}
			b.WriteString(nl)
			writeJSONString(b, k)
			b.WriteString(": ")
			if err := writeJSON(b, x.m[k], depth+1); err != nil {
				return err
			}
		}
		b.WriteString(end + "}")
	case []any:
		if len(x) == 0 {
			b.WriteString("[]")
			return nil
		}
		b.WriteString("[")
		for i, e := range x {
			if i > 0 {
				b.WriteString(",")
			}
			b.WriteString(nl)
			if err := writeJSON(b, e, depth+1); err != nil {
				return err
			}
		}
		b.WriteString(end + "]")
	default:
		return fmt.Errorf("Object of type %T is not JSON serializable", v)
	}
	return nil
}

// pyFloat is Python's repr(float) as json.dump writes it.
func pyFloat(f float64) string {
	switch {
	case math.IsNaN(f):
		return "NaN"
	case math.IsInf(f, 1):
		return "Infinity"
	case math.IsInf(f, -1):
		return "-Infinity"
	}
	a := math.Abs(f)
	if a != 0 && (a < 1e-4 || a >= 1e16) {
		return strconv.FormatFloat(f, 'e', -1, 64)
	}
	s := strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}

// writeJSONString escapes as json.dump does with ensure_ascii.
func writeJSONString(b *strings.Builder, s string) {
	b.WriteByte('"')
	for _, r := range s {
		switch {
		case r == '"':
			b.WriteString(`\"`)
		case r == '\\':
			b.WriteString(`\\`)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r == '\b':
			b.WriteString(`\b`)
		case r == '\f':
			b.WriteString(`\f`)
		case r < 0x20:
			fmt.Fprintf(b, `\u%04x`, r)
		case r >= 0x10000:
			r -= 0x10000
			fmt.Fprintf(b, `\u%04x\u%04x`, 0xd800+(r>>10), 0xdc00+(r&0x3ff))
		case r > 0x7e && r != 0x7f:
			fmt.Fprintf(b, `\u%04x`, r)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte('"')
}

// writeAtomic is the Python's `tmp.write_text; tmp.replace(path)`; parents are
// created.
func writeAtomic(path, text string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o777); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(text), 0o666); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
