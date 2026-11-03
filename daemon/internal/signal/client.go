package signal

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand/v2"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

const (
	retryInitial   = 2 * time.Second
	retryMax       = 60 * time.Second
	healthEvery    = 30 * time.Second
	staleAfter     = 120 * time.Second
	rpcTimeout     = 30 * time.Second
	checkTimeout   = 10 * time.Second
	maxSSELineSize = 64 << 20
)

// Client talks to signal-cli's HTTP daemon.
type Client struct {
	url, account string
	hc           *http.Client
}

// NewClient returns a client for the daemon at url (a trailing / is dropped)
// acting as account.
func NewClient(url, account string) *Client {
	return &Client{url: strings.TrimRight(url, "/"), account: account, hc: &http.Client{}}
}

// Check is the daemon's health check.
func (c *Client) Check(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, checkTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.url+"/api/v1/check", nil)
	if err != nil {
		return err
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("signal: health check answered %d", resp.StatusCode)
	}
	return nil
}

// Events calls handle with each event payload of signal-cli's stream, one
// after another, until ctx ends. A broken stream is reopened after 2 s,
// doubling to 60 s with up to 20% jitter, the wait reset by a good connect.
// A stream silent for 120 s gets a health check, and one that fails closes
// the stream.
func (c *Client) Events(ctx context.Context, handle func(data []byte)) error {
	backoff := retryInitial
	for ctx.Err() == nil {
		err := c.stream(ctx, handle, func() { backoff = retryInitial })
		if ctx.Err() != nil {
			break
		}
		log.Printf("signal: event stream ended (%v); reconnecting in %s", err, backoff)
		wait := backoff + time.Duration(rand.Float64()*0.2*float64(backoff))
		select {
		case <-ctx.Done():
		case <-time.After(wait):
		}
		backoff = min(backoff*2, retryMax)
	}
	return ctx.Err()
}

// stream is one connection of the event stream; connected is called when the
// daemon accepts it.
func (c *Client) stream(ctx context.Context, handle func([]byte), connected func()) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.url+"/api/v1/events?account="+url.QueryEscape(c.account), nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "text/event-stream")
	resp, err := c.hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("signal: event stream answered %d", resp.StatusCode)
	}
	connected()
	log.Printf("signal: event stream connected")

	var last atomic.Int64
	last.Store(time.Now().UnixNano())
	go func() {
		t := time.NewTicker(healthEvery)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
			}
			if time.Since(time.Unix(0, last.Load())) <= staleAfter {
				continue
			}
			if err := c.Check(ctx); err != nil {
				log.Printf("signal: the stream is silent and the health check failed (%v); reconnecting", err)
				cancel()
				return
			}
			last.Store(time.Now().UnixNano())
		}
	}()

	r := bufio.NewReaderSize(resp.Body, 64<<10)
	for {
		line, err := readLine(r)
		if err != nil {
			return err
		}
		last.Store(time.Now().UnixNano())
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, ":") || !strings.HasPrefix(line, "data:") {
			continue
		}
		if data := strings.TrimSpace(line[len("data:"):]); data != "" {
			handle([]byte(data))
		}
	}
}

// readLine reads one line of any length up to maxSSELineSize.
func readLine(r *bufio.Reader) (string, error) {
	var b []byte
	for {
		part, err := r.ReadSlice('\n')
		b = append(b, part...)
		if len(b) > maxSSELineSize {
			return "", errors.New("signal: an event line is too long")
		}
		if err == bufio.ErrBufferFull {
			continue
		}
		if err != nil {
			return "", err
		}
		return string(b), nil
	}
}

// rpc calls method and returns the result.
func (c *Client) rpc(ctx context.Context, method string, params map[string]any) (json.RawMessage, error) {
	ctx, cancel := context.WithTimeout(ctx, rpcTimeout)
	defer cancel()
	body, err := json.Marshal(map[string]any{"jsonrpc": "2.0", "method": method, "params": params,
		"id": method + "_" + strconv.FormatInt(time.Now().UnixMilli(), 10)})
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url+"/api/v1/rpc", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("signal: %s answered %d", method, resp.StatusCode)
	}
	var out struct {
		Result json.RawMessage `json:"result"`
		Error  json.RawMessage `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("signal: %s: %w", method, err)
	}
	if len(out.Error) > 0 && string(out.Error) != "null" {
		return nil, fmt.Errorf("signal: %s failed: %s", method, out.Error)
	}
	return out.Result, nil
}

// target adds where a message goes: a group id or a recipient.
func target(params map[string]any, to string) {
	if g, ok := strings.CutPrefix(to, "group:"); ok {
		params["groupId"] = g
	} else {
		params["recipient"] = []string{to}
	}
}

// Send sends text with styles ("start:length:STYLE") to to (a number, or
// "group:<id>") and returns the message's timestamp. Every recipient result
// must be a success.
func (c *Client) Send(ctx context.Context, to, text string, styles []string) (int64, error) {
	params := map[string]any{"account": c.account, "message": text}
	switch len(styles) {
	case 0:
	case 1:
		params["textStyle"] = styles[0]
	default:
		params["textStyles"] = styles
	}
	target(params, to)
	raw, err := c.rpc(ctx, "send", params)
	if err != nil {
		return 0, err
	}
	var res struct {
		Timestamp int64 `json:"timestamp"`
		Results   []struct {
			Type string `json:"type"`
		} `json:"results"`
	}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &res); err != nil {
			return 0, fmt.Errorf("signal: send: %w", err)
		}
	}
	for _, r := range res.Results {
		if r.Type != "" && r.Type != "SUCCESS" {
			return res.Timestamp, fmt.Errorf("signal: send to %s: %s", to, r.Type)
		}
	}
	return res.Timestamp, nil
}

// Typing starts (or with stop, ends) the typing indicator in a chat.
func (c *Client) Typing(ctx context.Context, to string, stop bool) error {
	params := map[string]any{"account": c.account}
	if stop {
		params["stop"] = true
	}
	target(params, to)
	_, err := c.rpc(ctx, "sendTyping", params)
	return err
}

// Attachment fetches an attachment's bytes.
func (c *Client) Attachment(ctx context.Context, id string) ([]byte, error) {
	raw, err := c.rpc(ctx, "getAttachment", map[string]any{"account": c.account, "id": id})
	if err != nil {
		return nil, err
	}
	var enc string
	if json.Unmarshal(raw, &enc) != nil {
		var wrapped struct {
			Data string `json:"data"`
		}
		if err := json.Unmarshal(raw, &wrapped); err != nil {
			return nil, fmt.Errorf("signal: attachment %s: unexpected answer", id)
		}
		enc = wrapped.Data
	}
	if b, err := base64.StdEncoding.DecodeString(enc); err == nil {
		return b, nil
	}
	b, err := base64.RawStdEncoding.DecodeString(strings.TrimRight(enc, "="))
	if err != nil {
		return nil, fmt.Errorf("signal: attachment %s: %w", id, err)
	}
	return b, nil
}
