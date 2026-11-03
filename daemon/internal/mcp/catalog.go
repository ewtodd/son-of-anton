package mcp

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// entry is one tool of a server's catalog.
type entry struct {
	name   string // mcp__<server>__<tool>, sanitized
	server string
	raw    string // the tool's own name, as the server knows it
	desc   string
	schema any
	tokens []string
}

var (
	unsafeName = regexp.MustCompile(`[^A-Za-z0-9_]`)
	wordRE     = regexp.MustCompile(`[A-Za-z0-9]+`)
)

// sanitize makes one part of a tool name safe: anything but letters, digits
// and underscores becomes an underscore.
func sanitize(s string) string { return unsafeName.ReplaceAllString(s, "_") }

// toolName is the name the model sees for a server's tool.
func toolName(server, tool string) string { return "mcp__" + sanitize(server) + "__" + sanitize(tool) }

// matchAny says whether name matches one of the globs (or equals one).
func matchAny(globs []string, name string) bool {
	for _, g := range globs {
		if ok, err := filepath.Match(g, name); (err == nil && ok) || g == name {
			return true
		}
	}
	return false
}

// allowed applies tools.include / tools.exclude to a server's own tool name;
// include wins.
func allowed(cfg Server, raw string) bool {
	if len(cfg.Include) > 0 {
		return matchAny(cfg.Include, raw)
	}
	return !matchAny(cfg.Exclude, raw)
}

// emptySchema is what a tool without parameters describes itself with.
var emptySchema = map[string]any{"type": "object", "properties": map[string]any{}}

// newEntry builds a catalog entry and its search tokens: the name split into
// words, the description and the top-level parameter names.
func newEntry(server, raw, desc string, schema any) entry {
	if m, ok := schema.(map[string]any); !ok || len(m) == 0 {
		schema = emptySchema
	}
	e := entry{name: toolName(server, raw), server: server, raw: raw, desc: desc, schema: schema}
	var params []string
	if m, ok := schema.(map[string]any); ok {
		if props, ok := m["properties"].(map[string]any); ok {
			for k := range props {
				params = append(params, k)
			}
			sort.Strings(params)
		}
	}
	for _, t := range wordRE.FindAllString(e.name+" "+desc+" "+strings.Join(params, " "), -1) {
		e.tokens = append(e.tokens, strings.ToLower(t))
	}
	return e
}

const (
	bm25K1 = 1.5
	bm25B  = 0.75
)

// rank returns the top limit entries for the query: BM25 over the tokens,
// and when nothing scores, entries whose name contains the query.
func rank(catalog []entry, query string, limit int) []entry {
	var q []string
	for _, t := range wordRE.FindAllString(query, -1) {
		q = append(q, strings.ToLower(t))
	}
	if len(catalog) == 0 || len(q) == 0 || limit <= 0 {
		return nil
	}
	total := 0
	df := map[string]int{}
	for _, e := range catalog {
		total += len(e.tokens)
		seen := map[string]bool{}
		for _, t := range e.tokens {
			if !seen[t] {
				seen[t] = true
				df[t]++
			}
		}
	}
	avg := math.Max(float64(total)/float64(len(catalog)), 1)
	type scored struct {
		e entry
		s float64
	}
	var hits []scored
	for _, e := range catalog {
		tf := map[string]int{}
		for _, t := range e.tokens {
			tf[t]++
		}
		s := 0.0
		for _, t := range q {
			n, f := df[t], tf[t]
			if n == 0 || f == 0 {
				continue
			}
			idf := math.Log(1 + (float64(len(catalog)-n)+0.5)/(float64(n)+0.5))
			s += idf * float64(f) * (bm25K1 + 1) / (float64(f) + bm25K1*(1-bm25B+bm25B*float64(len(e.tokens))/avg))
		}
		if s > 0 {
			hits = append(hits, scored{e, s})
		}
	}
	if len(hits) == 0 {
		ql := strings.ToLower(query)
		for _, e := range catalog {
			if strings.Contains(strings.ToLower(e.name), ql) {
				hits = append(hits, scored{e, 0.1})
			}
		}
	}
	sort.SliceStable(hits, func(i, j int) bool { return hits[i].s > hits[j].s })
	out := make([]entry, 0, min(limit, len(hits)))
	for _, h := range hits[:min(limit, len(hits))] {
		out = append(out, h.e)
	}
	return out
}

const (
	maxResultChars = 50000
	headChars      = 35000
	tailChars      = 15000
)

// imageExt is the file extension for an image's MIME type.
func imageExt(mime string) string {
	switch strings.ToLower(strings.TrimSpace(strings.SplitN(mime, ";", 2)[0])) {
	case "image/png":
		return "png"
	case "image/jpeg", "image/jpg":
		return "jpg"
	case "image/gif":
		return "gif"
	case "image/webp":
		return "webp"
	case "image/svg+xml":
		return "svg"
	case "image/bmp":
		return "bmp"
	}
	return "bin"
}

// saveImage writes an image under <dir>, named by the first 16 hex digits of
// the data's SHA-256, and returns the path.
func saveImage(dir, mime string, data []byte) (string, error) {
	sum := sha256.Sum256(data)
	path := filepath.Join(dir, hex.EncodeToString(sum[:])[:16]+"."+imageExt(mime))
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	return path, os.WriteFile(path, data, 0o600)
}

// clip keeps the head and tail of a text over maxResultChars and saves the
// whole to <dir>/<id>.txt, naming the file in the cut.
func clip(dir, id, text string) string {
	r := []rune(text)
	if len(r) <= maxResultChars {
		return text
	}
	path := filepath.Join(dir, id+".txt")
	note := ""
	if err := os.MkdirAll(dir, 0o700); err == nil && os.WriteFile(path, []byte(text), 0o600) == nil {
		note = fmt.Sprintf("; the full result is saved at %s", path)
	}
	return string(r[:headChars]) + fmt.Sprintf("\n\n[... %d characters omitted%s]\n\n", len(r)-headChars-tailChars, note) + string(r[len(r)-tailChars:])
}
