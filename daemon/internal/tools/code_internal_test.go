package tools

// Phase 7c internals: the script's environment, its output windows, the RPC
// answers and the image shrinking, tested without a model.

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"image"
	"image/color"
	"image/png"
	"strings"
	"testing"
	"time"
)

func TestScrubEnvDropsSecretsAndKeepsSafeNames(t *testing.T) {
	got := scrubEnv([]string{
		"PATH=/bin", "HOME=/h", "LC_ALL=C", "MY_API_KEY=k", "GITHUB_TOKEN=t", "AUTHOR=a",
		"SON_OF_ANTON_HOME=/soa", "SON_OF_ANTON_PYTHON=/py", "RANDOM_THING=x", "VIRTUAL_ENV=/v",
		"PYTHONPATH=/old",
	}, "/stage", "/stage/rpc.sock", "tok")
	env := map[string]string{}
	for _, kv := range got {
		k, v, _ := strings.Cut(kv, "=")
		env[k] = v
	}
	for _, k := range []string{"MY_API_KEY", "GITHUB_TOKEN", "AUTHOR", "SON_OF_ANTON_PYTHON", "RANDOM_THING"} {
		if _, ok := env[k]; ok {
			t.Errorf("%s reached the script", k)
		}
	}
	for _, k := range []string{"PATH", "HOME", "LC_ALL", "SON_OF_ANTON_HOME", "VIRTUAL_ENV"} {
		if _, ok := env[k]; !ok {
			t.Errorf("%s was dropped", k)
		}
	}
	if env["PYTHONPATH"] != "/stage:/old" || env["SON_OF_ANTON_RPC_SOCKET"] != "/stage/rpc.sock" ||
		env["SON_OF_ANTON_RPC_TOKEN"] != "tok" || env["PYTHONUTF8"] != "1" {
		t.Errorf("env = %v", env)
	}
}

func TestHeadTailKeepsTheEndsAndCountsTheRest(t *testing.T) {
	h := &headTail{maxH: 100, maxT: 150}
	var all []byte
	for i := range 1000 {
		line := []byte(strings.Repeat(string(rune('a'+i%26)), 9) + "\n")
		all = append(all, line...)
		_, _ = h.Write(line)
	}
	text, meta := h.text()
	if meta["stdout_truncated"] != true || meta["stdout_bytes_total"] != len(all) ||
		meta["stdout_bytes_captured"] != 250 || meta["stdout_bytes_omitted"] != len(all)-250 || meta["warning"] == nil {
		t.Fatalf("meta = %v", meta)
	}
	if !strings.HasPrefix(text, string(all[:100])) || !strings.HasSuffix(text, string(all[len(all)-150:])) ||
		!strings.Contains(text, "[output cut: 9,750 of 10,000 bytes omitted]") {
		t.Fatalf("text = %q", text)
	}
	small := &headTail{maxH: 100, maxT: 150}
	_, _ = small.Write([]byte("short"))
	if text, meta := small.text(); text != "short" || meta["stdout_truncated"] != false || meta["warning"] != nil {
		t.Fatalf("small = %q %v", text, meta)
	}
}

func TestRPCAnswers(t *testing.T) {
	calls := 0
	s := &rpcServer{token: "tok", allowed: []string{"terminal", "read_file"}, call: func(_ context.Context, name string, args json.RawMessage) string {
		calls++
		if name == "terminal" && strings.Contains(string(args), "background") {
			t.Errorf("terminal kept background: %s", args)
		}
		return `{"ok": true,
 "n": 1}`
	}}
	answer := func(line string) string { return s.handle(context.Background(), []byte(line)) }
	if got := answer(`{"tool":"terminal","args":{},"token":"nope"}`); !strings.Contains(got, "Unauthorized RPC request") || calls != 0 {
		t.Errorf("wrong token = %s", got)
	}
	if got := answer(`{"tool":"patch","args":{},"token":"tok"}`); !strings.Contains(got, "Tool 'patch' is not available in execute_code. Available: read_file, terminal") {
		t.Errorf("unavailable = %s", got)
	}
	if got := answer(`{"tool":"terminal","args":{"command":"ls","background":true,"notify_on_complete":true},"token":"tok"}`); got != `{"ok":true,"n":1}` {
		t.Errorf("a result is one line: %q", got)
	}
	for range codeMaxToolCalls - 1 {
		answer(`{"tool":"read_file","args":{},"token":"tok"}`)
	}
	if got := answer(`{"tool":"read_file","args":{},"token":"tok"}`); !strings.Contains(got, "Tool call limit reached (50)") || s.made() != codeMaxToolCalls {
		t.Errorf("the 51st call = %s after %d", got, s.made())
	}
	if got := answer(`not json`); !strings.Contains(got, "Invalid RPC request") {
		t.Errorf("garbage = %s", got)
	}
}

func TestOneLineKeepsNonJSONAsAString(t *testing.T) {
	var s string
	if err := json.Unmarshal([]byte(oneLine("plain\ntext")), &s); err != nil || s != "plain\ntext" {
		t.Fatalf("oneLine = %q (%v)", oneLine("plain\ntext"), err)
	}
}

func noisePNG(t *testing.T, w, h int) []byte {
	t.Helper()
	img := image.NewNRGBA(image.Rect(0, 0, w, h))
	var x uint32 = 12345
	for i := range img.Pix {
		x = x*1664525 + 1013904223
		img.Pix[i] = byte(x >> 24)
	}
	b, err := encodePNG(img)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestShrinkImageHalvesUntilItFits(t *testing.T) {
	data := noisePNG(t, 400, 300)
	limit := len(dataURL("image/png", data)) / 3
	url, scale := shrinkImage(data, "image/png", limit)
	if len(url) > limit || !strings.HasPrefix(url, "data:image/png;base64,") {
		t.Fatalf("shrunk to %d bytes (limit %d): %.40s", len(url), limit, url)
	}
	if scale == nil || scale.ow != 400 || scale.oh != 300 || scale.nw >= 400 || scale.nw < 64 {
		t.Fatalf("scale = %+v", scale)
	}
	if note := scaleNote(scale, nil); !strings.Contains(note, "Image downscaled from 400x300") {
		t.Fatalf("note = %q", note)
	}
	if _, scale := shrinkImage(data, "image/png", len(dataURL("image/png", data))); scale != nil {
		t.Fatalf("an image that fits was scaled: %+v", scale)
	}
}

func TestShrinkImageMakesJPEGOfOthers(t *testing.T) {
	var gifish bytes.Buffer
	img := image.NewPaletted(image.Rect(0, 0, 200, 200), color.Palette{color.Black, color.White})
	if err := png.Encode(&gifish, img); err != nil {
		t.Fatal(err)
	}
	url, _ := shrinkImage(gifish.Bytes(), "image/gif", 1<<20)
	if !strings.HasPrefix(url, "data:image/jpeg;base64,") {
		t.Fatalf("url = %.40s", url)
	}
}

func TestNormalizeImageConvertsBMPAndICOToPNG(t *testing.T) {
	// An icon whose one image is a PNG.
	pngData := noisePNG(t, 16, 16)
	ico := make([]byte, 22)
	binary.LittleEndian.PutUint16(ico[2:], 1)
	binary.LittleEndian.PutUint16(ico[4:], 1)
	ico[6], ico[7] = 16, 16
	binary.LittleEndian.PutUint32(ico[14:], uint32(len(pngData)))
	binary.LittleEndian.PutUint32(ico[18:], 22)
	ico = append(ico, pngData...)
	if mime := sniffImage(ico); mime != "image/x-icon" {
		t.Fatalf("sniff = %q", mime)
	}
	out, mime, err := normalizeImage(ico, "image/x-icon")
	if err != nil || mime != "image/png" || sniffImage(out) != "image/png" {
		t.Fatalf("ico: %q %v", mime, err)
	}
	if _, _, err := normalizeImage([]byte("BMxx"), "image/bmp"); err == nil {
		t.Fatal("a broken BMP converted")
	}
}

func TestCropClampsAndReportsTheOffset(t *testing.T) {
	data := noisePNG(t, 100, 50)
	out, off, err := cropImage(data, "image/png", []any{float64(-5), float64(10), float64(500), float64(30)})
	if err != nil || off.x != 0 || off.y != 10 || off.w != 100 || off.h != 20 {
		t.Fatalf("crop = %+v %v", off, err)
	}
	if cfg, err := png.DecodeConfig(bytes.NewReader(out)); err != nil || cfg.Width != 100 || cfg.Height != 20 {
		t.Fatalf("cropped image = %+v %v", cfg, err)
	}
	for _, bad := range [][]any{{1.0, 2.0}, {"a", 1.0, 2.0, 3.0}, {true, 1.0, 2.0, 3.0}} {
		if _, _, err := cropImage(data, "image/png", bad); err == nil || !strings.Contains(err.Error(), "Invalid region: need four") {
			t.Errorf("region %v: %v", bad, err)
		}
	}
}

func TestExecuteCodeTimesOutAndStopsTheScript(t *testing.T) {
	old := codeTimeout
	codeTimeout = 300 * time.Millisecond
	defer func() { codeTimeout = old }()
	if findPython() == "" {
		t.Skip("no Python 3")
	}
	res := runScript(t, context.Background(), "import time\nprint('start', flush=True)\ntime.sleep(60)")
	if res["status"] != "timeout" || !strings.Contains(res["output"].(string), "start") || !strings.Contains(res["error"].(string), "timed out") {
		t.Fatalf("result = %v", res)
	}
}

func TestExecuteCodeStopsWhenTheTurnIsStopped(t *testing.T) {
	if findPython() == "" {
		t.Skip("no Python 3")
	}
	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(300*time.Millisecond, cancel)
	res := runScript(t, ctx, "import time\ntime.sleep(60)")
	if res["status"] != "interrupted" || !strings.HasSuffix(res["output"].(string), "[run interrupted: the user sent a new message]") {
		t.Fatalf("result = %v", res)
	}
}
