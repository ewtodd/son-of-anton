package tools

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"
)

const (
	fetchTimeout   = 30 * time.Second
	serviceTimeout = 120 * time.Second
	pageMax        = 10 << 20
	thinPage       = 200 // under this many characters, an HTML page is a script-built shell

	blockedPrivate = "Blocked: URL targets a private or internal network address"
)

var (
	queryUnescape  = url.QueryUnescape
	errPrivateAddr = errors.New(blockedPrivate)
)

type extractResult struct {
	URL     string  `json:"url"`
	Title   string  `json:"title"`
	Content string  `json:"content"`
	Error   *string `json:"error"`
	Via     string  `json:"via"`
}

func failed(u, msg string) extractResult {
	return extractResult{URL: u, Error: &msg}
}

// page is what a reader produced for a URL.
type page struct {
	title, content string
}

// extract reads one URL: directly, then (when the direct fetch was blocked)
// through the extract service and the configured fallbacks.
func (w *Web) extract(ctx context.Context, raw string, budget int) extractResult {
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" {
		return failed(raw, "Invalid URL: expected an http(s) URL")
	}
	if !w.AllowPrivate && w.privateHost(ctx, u.Hostname()) {
		return failed(raw, blockedPrivate)
	}
	pg, blocked, err := w.fetchDirect(ctx, raw)
	via := "direct"
	if err != nil {
		if errors.Is(err, errPrivateAddr) {
			return failed(raw, blockedPrivate)
		}
		if !blocked {
			return failed(raw, "direct: "+err.Error())
		}
		attempts := []string{"direct: " + err.Error() + " (blocked)"}
		pg, via, attempts = w.tryFallbacks(ctx, raw, attempts)
		if via == "" {
			return failed(raw, strings.Join(attempts, "; "))
		}
	}
	content, err := w.fitToBudget(raw, pg.content, budget)
	if err != nil {
		return failed(raw, err.Error())
	}
	return extractResult{URL: raw, Title: pg.title, Content: content, Via: via}
}

// tryFallbacks tries the extract service, then each configured fallback, and
// returns the first page with content and who gave it ("" for none, with the
// attempts extended by why each failed).
func (w *Web) tryFallbacks(ctx context.Context, raw string, attempts []string) (page, string, []string) {
	try := func(name string, f func() (page, error)) (page, bool) {
		pg, err := f()
		if err == nil && strings.TrimSpace(pg.content) == "" {
			err = errors.New("no content")
		}
		if err != nil {
			attempts = append(attempts, name+": "+err.Error())
			return page{}, false
		}
		return pg, true
	}
	if w.ExtractService != "" {
		if pg, ok := try("service", func() (page, error) { return w.viaService(ctx, raw) }); ok {
			return pg, "service", attempts
		}
	}
	for _, name := range w.Fallbacks {
		var f func() (page, error)
		switch name {
		case "wayback":
			if w.Wayback != "" {
				f = func() (page, error) { return w.viaWayback(ctx, raw) }
			}
		case "jina":
			if w.Jina != "" {
				f = func() (page, error) { return w.viaJina(ctx, raw) }
			}
		}
		if f == nil {
			continue
		}
		if pg, ok := try(name, f); ok {
			return pg, name, attempts
		}
	}
	return page{}, "", attempts
}

// fetchDirect GETs the URL and converts it. blocked says that the failure is
// one a fallback may get around.
func (w *Web) fetchDirect(ctx context.Context, raw string) (pg page, blocked bool, err error) {
	body, ctype, status, err := w.get(ctx, raw)
	if err != nil {
		return page{}, false, err
	}
	switch status {
	case 401, 403, 429, 451, 503:
		return page{}, true, fmt.Errorf("HTTP %d", status)
	}
	if status < 200 || status > 299 {
		return page{}, false, fmt.Errorf("HTTP %d", status)
	}
	pg, isHTML, err := convertBody(ctx, body, ctype)
	if err != nil {
		return page{}, false, err
	}
	if isHTML {
		if botCheck(body, pg.title) {
			return page{}, true, errors.New("bot check page")
		}
		if utf8.RuneCountInString(pg.content) < thinPage {
			return page{}, true, errors.New("page has no readable content")
		}
	}
	return pg, false, nil
}

var botMarkers = []string{"challenge-platform", "cf-browser-verification", "Enable JavaScript and cookies to continue"}

func botCheck(body []byte, title string) bool {
	if title == "Just a moment..." || strings.Contains(title, "Attention Required") {
		return true
	}
	for _, m := range botMarkers {
		if bytes.Contains(body, []byte(m)) {
			return true
		}
	}
	return false
}

// get fetches a URL as a browser would, refusing private addresses at every
// hop unless allowed.
func (w *Web) get(ctx context.Context, raw string) (body []byte, ctype string, status int, err error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, raw, nil)
	if err != nil {
		return nil, "", 0, err
	}
	req.Header.Set("User-Agent", browserUA)
	req.Header.Set("Accept", browserAccept)
	req.Header.Set("Accept-Language", browserLang)
	resp, err := w.client(fetchTimeout).Do(req)
	if err != nil {
		return nil, "", 0, unwrapDial(err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err = io.ReadAll(io.LimitReader(resp.Body, pageMax))
	if err != nil {
		return nil, "", 0, err
	}
	return body, resp.Header.Get("Content-Type"), resp.StatusCode, nil
}

// unwrapDial reduces a request error to its cause: the private-address
// refusal, or the transport's own message without the URL.
func unwrapDial(err error) error {
	if errors.Is(err, errPrivateAddr) {
		return errPrivateAddr
	}
	var ue *url.Error
	if errors.As(err, &ue) {
		return ue.Err
	}
	return err
}

// client is an HTTP client whose connections to private addresses fail when
// private addresses are not allowed. The check is on the resolved address at
// connect time, so a redirect or DNS rebinding cannot get around it.
func (w *Web) client(timeout time.Duration) *http.Client {
	tr := http.DefaultTransport.(*http.Transport).Clone()
	if !w.AllowPrivate {
		tr.Proxy = nil
		tr.DialContext = (&net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second, Control: func(_, address string, _ syscall.RawConn) error {
			host, _, err := net.SplitHostPort(address)
			if err != nil {
				return err
			}
			if ip, err := netip.ParseAddr(host); err != nil || privateIP(ip) {
				return errPrivateAddr
			}
			return nil
		}}).DialContext
	}
	return &http.Client{Timeout: timeout, Transport: tr}
}

var cgnat = netip.MustParsePrefix("100.64.0.0/10")

// privateIP says whether ip is loopback, private, link-local, CGNAT,
// unique-local, unspecified or multicast.
func privateIP(ip netip.Addr) bool {
	ip = ip.Unmap()
	return ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() ||
		ip.IsMulticast() || ip.IsUnspecified() || cgnat.Contains(ip) ||
		(ip.Is4() && ip.As4()[0] == 0)
}

// privateHost says whether host is, or resolves to, a private address. A
// name that does not resolve is not private here; the connection check still
// guards the fetch.
func (w *Web) privateHost(ctx context.Context, host string) bool {
	host = strings.ToLower(strings.TrimSuffix(host, "."))
	if host == "localhost" || strings.HasSuffix(host, ".localhost") || host == "metadata.google.internal" {
		return true
	}
	if ip, err := netip.ParseAddr(host); err == nil {
		return privateIP(ip)
	}
	addrs, err := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
	if err != nil {
		return false
	}
	for _, a := range addrs {
		if privateIP(a) {
			return true
		}
	}
	return false
}

// convertBody turns a fetched body into text by its content type.
func convertBody(ctx context.Context, body []byte, ctype string) (pg page, isHTML bool, err error) {
	mt, _, _ := mime.ParseMediaType(ctype)
	if mt == "" {
		mt = strings.ToLower(strings.TrimSpace(strings.SplitN(http.DetectContentType(body), ";", 2)[0]))
	}
	switch {
	case mt == "text/html" || mt == "application/xhtml+xml":
		title, md := htmlToMarkdown(body, ctype)
		return page{title: title, content: md}, true, nil
	case mt == "application/pdf":
		text, err := pdfText(ctx, body)
		return page{content: text}, false, err
	case strings.HasPrefix(mt, "text/") || mt == "application/json" || strings.HasSuffix(mt, "+json") ||
		mt == "application/xml" || strings.HasSuffix(mt, "+xml"):
		return page{content: strings.ToValidUTF8(string(body), "�")}, false, nil
	}
	return page{}, false, fmt.Errorf("unsupported content type %s", mt)
}

func pdfText(ctx context.Context, body []byte) (string, error) {
	path, err := exec.LookPath("pdftotext")
	if err != nil {
		return "", errors.New("PDF extraction needs pdftotext")
	}
	cmd := exec.CommandContext(ctx, path, "-layout", "-", "-")
	cmd.Stdin = bytes.NewReader(body)
	var out bytes.Buffer
	cmd.Stdout = &out
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("pdftotext failed: %w", err)
	}
	return out.String(), nil
}

// viaService asks the extract service.
func (w *Web) viaService(ctx context.Context, raw string) (page, error) {
	payload, _ := json.Marshal(map[string]string{"url": raw})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(w.ExtractService, "/")+"/extract", bytes.NewReader(payload))
	if err != nil {
		return page{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := (&http.Client{Timeout: serviceTimeout}).Do(req)
	if err != nil {
		return page{}, unwrapDial(err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return page{}, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	var out struct {
		Title   string `json:"title"`
		Content string `json:"content"`
		Error   any    `json:"error"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, pageMax)).Decode(&out); err != nil {
		return page{}, errors.New("invalid JSON")
	}
	if out.Content == "" && out.Error != nil && out.Error != "" {
		return page{}, fmt.Errorf("%v", out.Error)
	}
	return page{title: out.Title, content: out.Content}, nil
}

// viaWayback reads the Wayback Machine's closest snapshot, in its original
// form (the id_ flavor, without the archive's toolbar).
func (w *Web) viaWayback(ctx context.Context, raw string) (page, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, w.Wayback+"?"+url.Values{"url": {raw}}.Encode(), nil)
	if err != nil {
		return page{}, err
	}
	body, status, err := doSearch(req)
	if err != nil {
		return page{}, unwrapDial(err)
	}
	if status != http.StatusOK {
		return page{}, fmt.Errorf("HTTP %d", status)
	}
	var out struct {
		Archived struct {
			Closest struct {
				Available bool   `json:"available"`
				URL       string `json:"url"`
				Timestamp string `json:"timestamp"`
			} `json:"closest"`
		} `json:"archived_snapshots"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return page{}, errors.New("invalid JSON")
	}
	snap := out.Archived.Closest
	if !snap.Available || snap.URL == "" {
		return page{}, errors.New("no snapshot")
	}
	snapURL := snap.URL
	if snap.Timestamp != "" {
		snapURL = strings.Replace(snapURL, "/"+snap.Timestamp+"/", "/"+snap.Timestamp+"id_/", 1)
	}
	got, ctype, code, err := w.get(ctx, snapURL)
	if err != nil {
		return page{}, err
	}
	if code < 200 || code > 299 {
		return page{}, fmt.Errorf("snapshot HTTP %d", code)
	}
	pg, _, err := convertBody(ctx, got, ctype)
	return pg, err
}

// viaJina reads the page through Jina's reader, which answers Markdown.
func (w *Web) viaJina(ctx context.Context, raw string) (page, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, w.Jina+raw, nil)
	if err != nil {
		return page{}, err
	}
	req.Header.Set("Accept", "text/plain")
	req.Header.Set("User-Agent", browserUA)
	resp, err := (&http.Client{Timeout: fetchTimeout}).Do(req)
	if err != nil {
		return page{}, unwrapDial(err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return page{}, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, pageMax))
	if err != nil {
		return page{}, err
	}
	return page{content: strings.ToValidUTF8(string(body), "�")}, nil
}

// fitToBudget returns the text whole when it fits. A longer page is saved
// whole under the cache, and a head and tail window of about budget
// characters comes back, its footer naming the saved file.
func (w *Web) fitToBudget(raw, content string, budget int) (string, error) {
	runes := []rune(content)
	if len(runes) <= budget {
		return content, nil
	}
	sum := sha256.Sum256([]byte(raw))
	path := filepath.Join(w.CacheDir, hex.EncodeToString(sum[:])[:16]+".md")
	if err := os.MkdirAll(w.CacheDir, 0o755); err != nil {
		return "", fmt.Errorf("saving the full page: %w", err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		return "", fmt.Errorf("saving the full page: %w", err)
	}
	head := string(runes[:int(float64(budget)*0.7)])
	tail := string(runes[len(runes)-int(float64(budget)*0.3):])
	omitted := len(runes) - utf8.RuneCountInString(head) - utf8.RuneCountInString(tail)
	middle := strings.Count(head, "\n") + 1
	return fmt.Sprintf("%s\n\n[... %d characters omitted ...]\n\n%s\n\n[Truncated: the page has %d characters. The full text is saved at %s; "+
		"the omitted part starts at line %d (read_file with offset=%d).]", head, omitted, tail, len(runes), path, middle, middle), nil
}
