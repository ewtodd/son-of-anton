package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strings"
	"time"

	"golang.org/x/net/html"
)

const (
	browserUA     = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
	browserAccept = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
	browserLang   = "en-US,en;q=0.9"

	searchTimeout = 15 * time.Second
	searchBodyMax = 4 << 20
)

type searchHit struct {
	Title       string `json:"title"`
	URL         string `json:"url"`
	Description string `json:"description"`
	Position    int    `json:"position"`
}

// search answers WebSearch: SearXNG first, then DuckDuckGo when it is set.
// keep, when not nil, says which result URLs may be shown; the engine is then
// asked for more results, and limit applies to those kept.
func (w *Web) search(ctx context.Context, query string, limit int, keep func(string) bool) string {
	var notes, failures []string
	fetch := limit
	if keep != nil {
		fetch = searchFilteredN
	}
	if w.SearXNG != "" {
		hits, err := w.searchSearXNG(ctx, query, fetch)
		switch {
		case err != nil:
			notes = append(notes, "searxng: "+err.Error())
		case len(hits) == 0 && w.DuckDuckGo != "":
			notes = append(notes, "searxng: no results")
		case len(hits) > 0 && !answersQuery(query, hits):
			notes = append(notes, "searxng: results did not match the query")
		default:
			return searchAnswer("searxng", hits, nil, keep, limit)
		}
		failures = append(failures, notes...)
	}
	if w.DuckDuckGo != "" {
		hits, err := w.searchDuckDuckGo(ctx, query, fetch)
		switch {
		case err != nil:
			failures = append(failures, "duckduckgo: "+err.Error())
		case len(hits) > 0 && !answersQuery(query, hits):
			failures = append(failures, "duckduckgo: results did not match the query")
		default:
			return searchAnswer("duckduckgo", hits, notes, keep, limit)
		}
	}
	if len(failures) == 0 {
		return jsonOut(map[string]any{"success": false, "error": "no search engine is set up"})
	}
	return jsonOut(map[string]any{"success": false, "error": strings.Join(failures, "; ")})
}

func searchAnswer(backend string, hits []searchHit, notes []string, keep func(string) bool, limit int) string {
	if keep != nil {
		var kept []searchHit
		for _, h := range hits {
			if keep(h.URL) && len(kept) < limit {
				h.Position = len(kept) + 1
				kept = append(kept, h)
			}
		}
		if len(kept) == 0 && len(hits) > 0 {
			notes = append(notes, fmt.Sprintf("none of the %d results the engine gave passed the domain filter", len(hits)))
		}
		hits = kept
	}
	if hits == nil {
		hits = []searchHit{}
	}
	out := map[string]any{"success": true, "backend": backend, "data": map[string]any{"web": hits}}
	if len(notes) > 0 {
		out["notes"] = notes
	}
	return jsonOut(out)
}

// minQueryOverlap is the share of a query's own words a result must carry for
// the response to count as an answer. Measured against this house's engines
// (2026-11): block pages shared at most 0.14 of their query, real result sets
// never fell below 0.50, so 0.25 sits between the two with room either side.
const minQueryOverlap = 0.25

// queryWordRE is the word splitter for the overlap check.
var queryWordRE = regexp.MustCompile(`[a-z0-9]+`)

// webStopwords are the words every page carries, so that counting them would
// make any page look like an answer.
var webStopwords = func() map[string]bool {
	m := map[string]bool{}
	for _, w := range strings.Fields("a an and are as at be been being but by can could did do does for from had has have how " +
		"i if in into is it its me might must my of on or our shall should so than that the their them then there these they " +
		"this those to was we were what when where which who why will with would you your") {
		m[w] = true
	}
	return m
}()

// matchWords is the set of words text is compared by: lowercased, plurals
// folded, nothing of one letter or in webStopwords.
func matchWords(text string) map[string]bool {
	words := map[string]bool{}
	for _, w := range queryWordRE.FindAllString(strings.ToLower(text), -1) {
		if len(w) < 2 || webStopwords[w] {
			continue
		}
		if len(w) > 3 && strings.HasSuffix(w, "s") && !strings.HasSuffix(w, "ss") {
			w = w[:len(w)-1]
		}
		words[w] = true
	}
	return words
}

// answersQuery reports whether any hit shares enough of the query's words to
// be an answer, as distinct from a page served in place of an answer. An
// engine that has blocked this host often replies 200 with its own front page
// instead of an error (Bing does, and the parser reads the front page as
// results), whereupon every hit shares nothing with the query. Rejecting the
// whole response on that basis turns such a block into no results; individual
// hits are never dropped, since a good result may legitimately word things
// differently from the query. A query with no words left to compare passes,
// there being nothing to judge.
func answersQuery(query string, hits []searchHit) bool {
	words := matchWords(query)
	if len(words) == 0 {
		return true
	}
	for _, h := range hits {
		hit := matchWords(h.Title + " " + h.Description + " " + h.URL)
		shared := 0
		for w := range words {
			if hit[w] {
				shared++
			}
		}
		if float64(shared)/float64(len(words)) >= minQueryOverlap {
			return true
		}
	}
	return false
}

func (w *Web) searchSearXNG(ctx context.Context, query string, limit int) ([]searchHit, error) {
	u, err := url.Parse(strings.TrimSpace(w.SearXNG))
	if err != nil {
		return nil, fmt.Errorf("bad SEARXNG_URL: %w", err)
	}
	if p := strings.TrimRight(u.Path, "/"); !strings.HasSuffix(p, "/search") {
		u.Path = p + "/search"
	}
	q := u.Query()
	q.Set("q", query)
	q.Set("format", "json")
	q.Set("pageno", "1")
	u.RawQuery = q.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	body, status, err := doSearch(req)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, fmt.Errorf("HTTP %d", status)
	}
	var resp struct {
		Results []struct {
			Title   string  `json:"title"`
			URL     string  `json:"url"`
			Content string  `json:"content"`
			Score   float64 `json:"score"`
		} `json:"results"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, fmt.Errorf("invalid JSON: %w", err)
	}
	sort.SliceStable(resp.Results, func(i, j int) bool { return resp.Results[i].Score > resp.Results[j].Score })
	var hits []searchHit
	for _, r := range resp.Results[:min(len(resp.Results), limit)] {
		hits = append(hits, searchHit{Title: r.Title, URL: r.URL, Description: r.Content, Position: len(hits) + 1})
	}
	return hits, nil
}

func doSearch(req *http.Request) ([]byte, int, error) {
	resp, err := (&http.Client{Timeout: searchTimeout}).Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(resp.Body, searchBodyMax))
	return body, resp.StatusCode, err
}

const ddgRefused = "DuckDuckGo turned the query away (rate limit)"

func (w *Web) searchDuckDuckGo(ctx context.Context, query string, limit int) ([]searchHit, error) {
	form := url.Values{"q": {query}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, w.DuckDuckGo, strings.NewReader(form.Encode()))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("User-Agent", browserUA)
	req.Header.Set("Accept", browserAccept)
	req.Header.Set("Accept-Language", browserLang)
	body, status, err := doSearch(req)
	if err != nil {
		return nil, err
	}
	if status == http.StatusAccepted {
		return nil, fmt.Errorf("%s", ddgRefused)
	}
	if status != http.StatusOK {
		return nil, fmt.Errorf("HTTP %d", status)
	}
	doc, err := html.Parse(strings.NewReader(string(body)))
	if err != nil {
		return nil, err
	}
	hits := parseDuckDuckGo(doc)
	if len(hits) == 0 && strings.Contains(string(body), "anomaly") {
		return nil, fmt.Errorf("%s", ddgRefused)
	}
	if len(hits) > limit {
		hits = hits[:limit]
	}
	for i := range hits {
		hits[i].Position = i + 1
	}
	return hits, nil
}

// parseDuckDuckGo reads the results of DuckDuckGo's HTML page, skipping ads.
func parseDuckDuckGo(doc *html.Node) []searchHit {
	var hits []searchHit
	var walk func(n *html.Node)
	walk = func(n *html.Node) {
		if n.Type == html.ElementNode && hasClass(n, "result") {
			if !hasClass(n, "result--ad") {
				if a := findByClass(n, "result__a"); a != nil {
					hits = append(hits, searchHit{Title: collapseSpace(textOf(a)), URL: resultURL(attr(a, "href")),
						Description: collapseSpace(textOf(findByClass(n, "result__snippet")))})
				}
			}
			return
		}
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			walk(c)
		}
	}
	walk(doc)
	return hits
}

// resultURL unwraps DuckDuckGo's redirect link to the page it points at.
func resultURL(href string) string {
	href = strings.TrimSpace(href)
	full := href
	if strings.HasPrefix(full, "//") {
		full = "https:" + full
	}
	if u, err := url.Parse(full); err == nil && (u.Host == "duckduckgo.com" || strings.HasSuffix(u.Host, ".duckduckgo.com")) && strings.HasPrefix(u.Path, "/l/") {
		if t := u.Query().Get("uddg"); t != "" {
			return t
		}
	}
	return href
}

func attr(n *html.Node, key string) string {
	for _, a := range n.Attr {
		if a.Key == key {
			return a.Val
		}
	}
	return ""
}

func hasClass(n *html.Node, class string) bool {
	for _, c := range strings.Fields(attr(n, "class")) {
		if c == class {
			return true
		}
	}
	return false
}

// findByClass is the first element under n with the class (nil: none).
func findByClass(n *html.Node, class string) *html.Node {
	for c := n.FirstChild; c != nil; c = c.NextSibling {
		if c.Type == html.ElementNode && hasClass(c, class) {
			return c
		}
		if f := findByClass(c, class); f != nil {
			return f
		}
	}
	return nil
}

// textOf is the concatenated text under n (nil: "").
func textOf(n *html.Node) string {
	if n == nil {
		return ""
	}
	var b strings.Builder
	var walk func(*html.Node)
	walk = func(n *html.Node) {
		if n.Type == html.TextNode {
			b.WriteString(n.Data)
		}
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			walk(c)
		}
	}
	walk(n)
	return b.String()
}

func collapseSpace(s string) string { return strings.Join(strings.Fields(s), " ") }
