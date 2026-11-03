package tools

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/ewtodd/son-of-anton/daemon/internal/provider"
)

// Vision is the endpoint vision_analyze sends images to.
type Vision struct {
	BaseURL, APIKey, Model string
	Timeout                time.Duration
}

const (
	visionDefaultTimeout  = 120 * time.Second
	visionDownloadTimeout = 30 * time.Second
	visionFailed          = "The image could not be analyzed because the request failed."
)

var (
	schemeRe = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9+.\-]*://`)
	thinkRe  = regexp.MustCompile(`(?s)<think>.*?</think>`)
)

type visionTool struct{}

func (visionTool) Concurrent() bool { return true }

func (visionTool) Schema() provider.Tool {
	return provider.Tool{Type: "function", Function: provider.ToolFunction{
		Name: "vision_analyze",
		Description: "Have a vision model look at an image and tell you about it in text. " +
			"The image may be an http(s) URL, a file path or a data: URL, and you may restrict it to a rectangle. " +
			"The picture goes to the auxiliary.vision model (the main endpoint if none is set); " +
			"you get its written answer and the picture itself stays out of your context. " +
			"Use it for an image you only know by path or address, and to zoom in on a part of one. " +
			"An image that is already attached to the conversation can simply be looked at, with no call.",
		Parameters: json.RawMessage(`{
  "type": "object",
  "properties": {
    "image_url": {"type": "string", "description": "Where the image is: an http(s) URL, a file path, or a data: URL."},
    "question": {"type": "string", "description": "What you want to know about the image. The vision model answers this after describing the image."},
    "region": {
      "type": "array",
      "items": {"type": "integer"},
      "minItems": 4,
      "maxItems": 4,
      "description": "A rectangle [x1, y1, x2, y2] in pixels of the original image. It is cut out before any shrinking, so it keeps full detail. A good pattern is to look at the whole image once, then ask again with a region around something small such as fine print or a control. Values outside the image are pulled back to its edges."
    }
  },
  "required": ["image_url", "question"]
}`),
	}}
}

// visionFail is the result of a call that could not analyze its image.
func visionFail(err error) string {
	return jsonOut(map[string]any{"success": false, "error": "Could not analyze the image: " + err.Error(), "analysis": visionFailed})
}

func (visionTool) Run(ctx context.Context, env *Env, args json.RawMessage) (string, error) {
	var in struct {
		ImageURL any             `json:"image_url"`
		Question any             `json:"question"`
		Region   json.RawMessage `json:"region"`
	}
	if err := json.Unmarshal(args, &in); err != nil {
		return errorResult("invalid arguments: " + err.Error()), nil
	}
	if ctx.Err() != nil {
		return visionFail(errors.New("Interrupted")), nil
	}
	src, _ := in.ImageURL.(string)
	question := ""
	switch q := in.Question.(type) {
	case nil:
	case string:
		question = q
	default:
		b, _ := json.Marshal(q)
		question = string(b)
	}
	if env.Vision == nil {
		return visionFail(errors.New("no vision model has been set up")), nil
	}

	data, mime, err := loadImage(ctx, env, src)
	if err != nil {
		return visionFail(err), nil
	}
	if data, mime, err = normalizeImage(data, mime); err != nil {
		return visionFail(err), nil
	}
	var offset *regionOffset
	if r := bytes.TrimSpace(in.Region); len(r) > 0 && string(r) != "null" {
		var region []any
		if json.Unmarshal(r, &region) != nil {
			region = nil
		}
		if data, offset, err = cropImage(data, mime, region); err != nil {
			return visionFail(err), nil
		}
		mime = "image/png"
	}
	imageURL := dataURL(mime, data)
	var scale *scaleInfo
	if len(imageURL) > visionMaxPayload {
		imageURL, scale = shrinkImage(data, mime, visionShrinkTarget)
		if len(imageURL) > visionMaxPayload {
			return visionFail(fmt.Errorf("Image too big for the vision model: %.1f MB encoded, and the limit is "+
				"%d MB even after shrinking it. Crop or compress it yourself first.",
				float64(len(imageURL))/(1<<20), visionMaxPayload>>20)), nil
		}
	}

	analysis, err := env.Vision.describe(ctx, question, imageURL)
	if err != nil {
		return visionFail(err), nil
	}
	if analysis == "" {
		analysis = visionFailed
	}
	res := map[string]any{"success": true, "analysis": analysis}
	if note := scaleNote(scale, offset); note != "" {
		res["analysis"] = "[" + note + "] " + analysis
		res["scale_note"] = note
	}
	return jsonOut(res), nil
}

// describe asks the vision model about the image. A reply with no content
// falls back to its reasoning, and an empty one is asked again once.
func (v *Vision) describe(ctx context.Context, question, imageURL string) (string, error) {
	timeout := v.Timeout
	if timeout <= 0 {
		timeout = visionDefaultTimeout
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	client := provider.New(v.BaseURL, v.APIKey)
	temperature := 0.1
	req := provider.Request{Model: v.Model, Temperature: &temperature, Messages: []provider.Message{{
		Role: "user",
		Parts: []provider.ContentPart{
			{Type: "text", Text: "Describe this image in full, then answer the question that follows it.\n\nQuestion:\n" + question},
			{Type: "image_url", ImageURL: &provider.ImageURL{URL: imageURL}},
		},
	}}}
	for range 2 {
		resp, err := client.Complete(ctx, req, func(provider.Delta) {})
		if err != nil {
			return "", err
		}
		text := strings.TrimSpace(thinkRe.ReplaceAllString(resp.Message.Content, ""))
		if text == "" {
			text = strings.TrimSpace(resp.Reasoning)
		}
		if text != "" {
			return text, nil
		}
	}
	return "", nil
}

// loadImage reads an image source (a data: URL, an http(s) URL, a local path
// or file:// URI) and checks that it is an image.
func loadImage(ctx context.Context, env *Env, src string) ([]byte, string, error) {
	s := strings.TrimSpace(src)
	if s == "" {
		return nil, "", errors.New("image_url is missing")
	}
	var data []byte
	switch {
	case strings.HasPrefix(s, "data:"):
		header, payload, _ := strings.Cut(s, ",")
		if !strings.Contains(header, ";base64") {
			return nil, "", errors.New("a data: URL has to carry base64 data")
		}
		if len(payload)*3/4 > visionMaxSource {
			return nil, "", errors.New("the data: URL is over the size limit")
		}
		var err error
		if data, err = base64.StdEncoding.DecodeString(payload); err != nil {
			return nil, "", fmt.Errorf("invalid base64 in data: URL: %v", err)
		}
	case strings.HasPrefix(s, "http://"), strings.HasPrefix(s, "https://"):
		var err error
		if data, err = downloadImage(ctx, env, s); err != nil {
			return nil, "", err
		}
	case schemeRe.MatchString(s) && !strings.HasPrefix(strings.ToLower(s), "file://"):
		return nil, "", errors.New("Unrecognized image source scheme. Give an http(s) URL, a file path, " +
			"a file:// URI or a data: URL.")
	default:
		path := s
		if strings.HasPrefix(strings.ToLower(path), "file://") {
			path = path[len("file://"):]
		}
		abs, err := env.Policy.Resolve(env.State.Cwd(), path)
		if err != nil {
			return nil, "", err
		}
		if fi, err := os.Stat(abs); err != nil || !fi.Mode().IsRegular() {
			return nil, "", fmt.Errorf("no file at '%s'", path)
		}
		if err := env.Policy.CheckRead(abs); err != nil {
			return nil, "", err
		}
		f, err := os.Open(abs)
		if err != nil {
			return nil, "", err
		}
		defer func() { _ = f.Close() }()
		if data, err = io.ReadAll(io.LimitReader(f, visionMaxSource+1)); err != nil {
			return nil, "", err
		}
	}
	if len(data) > visionMaxSource {
		return nil, "", errors.New("the file is over the size limit")
	}
	if mime := sniffImage(data); mime != "" {
		return data, mime, nil
	}
	return nil, "", errNotImage
}

// downloadImage fetches an image URL, refusing private addresses at every
// hop unless web.allow_private_urls is set.
func downloadImage(ctx context.Context, env *Env, raw string) ([]byte, error) {
	w := env.Web
	if w == nil {
		w = &Web{}
	}
	u, err := url.Parse(raw)
	if err != nil || u.Hostname() == "" {
		return nil, errors.New("the image URL could not be parsed")
	}
	if !w.AllowPrivate && w.privateHost(ctx, u.Hostname()) {
		return nil, errors.New("refused: the URL is unsafe or private")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, raw, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", browserUA)
	req.Header.Set("Accept", "image/*,*/*;q=0.8")
	resp, err := w.client(visionDownloadTimeout).Do(req)
	if err != nil {
		return nil, unwrapDial(err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	if resp.ContentLength > visionMaxSource {
		return nil, fmt.Errorf("Image is %d bytes; the most allowed is %d", resp.ContentLength, visionMaxSource)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, visionMaxSource+1))
	if err != nil {
		return nil, err
	}
	if len(data) > visionMaxSource {
		return nil, fmt.Errorf("Image is larger than the %d bytes allowed", visionMaxSource)
	}
	return data, nil
}
