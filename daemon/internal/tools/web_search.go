package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
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

// search answers web_search: SearXNG first, then DuckDuckGo when it is set.
func (w *Web) search(ctx context.Context, query string, limit int) string {
	var notes, failures []string
	if w.SearXNG != "" {
		hits, err := w.searchSearXNG(ctx, query, limit)
		switch {
		case err != nil:
			notes = append(notes, "searxng: "+err.Error())
		case len(hits) == 0 && w.DuckDuckGo != "":
			notes = append(notes, "searxng: no results")
		default:
			return searchAnswer("searxng", hits, nil)
		}
		failures = append(failures, notes...)
	}
	if w.DuckDuckGo != "" {
		hits, err := w.searchDuckDuckGo(ctx, query, limit)
		if err == nil {
			return searchAnswer("duckduckgo", hits, notes)
		}
		failures = append(failures, "duckduckgo: "+err.Error())
	}
	if len(failures) == 0 {
		return jsonOut(map[string]any{"success": false, "error": "no search engine is set up"})
	}
	return jsonOut(map[string]any{"success": false, "error": strings.Join(failures, "; ")})
}

func searchAnswer(backend string, hits []searchHit, notes []string) string {
	if hits == nil {
		hits = []searchHit{}
	}
	out := map[string]any{"success": true, "backend": backend, "data": map[string]any{"web": hits}}
	if len(notes) > 0 {
		out["notes"] = notes
	}
	return jsonOut(out)
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
