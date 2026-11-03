package server_test

// Phase 7c through the daemon: execute_code and vision_analyze
// (daemon/phases/07c-code-vision.md). execute_code needs a Python 3; the
// tests that run scripts skip without one.

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"image"
	"image/color"
	"image/png"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ewtodd/son-of-anton/daemon/internal/fakellm"
)

// python makes a Python 3 available to the daemon, or skips.
func python(t *testing.T) {
	t.Helper()
	py := os.Getenv("SON_OF_ANTON_PYTHON")
	if py == "" {
		py, _ = exec.LookPath("python3")
	}
	if py == "" {
		t.Skip("no Python 3 for execute_code")
	}
	t.Setenv("SON_OF_ANTON_PYTHON", py)
	t.Setenv("VIRTUAL_ENV", "")
	t.Setenv("CONDA_PREFIX", "")
}

func code(id, src string) fakellm.ToolCall {
	b, _ := json.Marshal(map[string]string{"code": src})
	return call(id, "execute_code", string(b))
}

func decodeRow(t *testing.T, row string) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal([]byte(row), &m); err != nil {
		t.Fatalf("not JSON: %s", row)
	}
	return m
}

func TestExecuteCodeCallsTheSessionsTools(t *testing.T) {
	python(t)
	t.Setenv("MY_API_KEY", "do-not-pass")
	cwd := t.TempDir()
	if err := os.WriteFile(filepath.Join(cwd, "notes.txt"), []byte("one\ntwo\nthree\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{
			code("x1", "import json, os\nfrom son_of_anton_tools import read_file, terminal\n"+
				"r = read_file('notes.txt')\nt = terminal('echo from-terminal')\n"+
				"print(json.dumps({'lines': r['total_lines'], 'out': t['output'].strip(), 'exit': t['exit_code'], "+
				"'key': os.environ.get('MY_API_KEY'), 'rpc': bool(os.environ.get('SON_OF_ANTON_RPC_TOKEN')), 'cwd': os.getcwd()}))"),
			code("x2", "print('before')\n1/0"),
			code("x3", "print('x' * 60000)"),
		}},
		fakellm.Turn{Content: "done"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(cwd)
	c.attach(id, "driver")
	c.send(id, "run some code")
	rows := toolRows(c.turn())
	if len(rows) != 3 {
		t.Fatalf("rows = %v", rows)
	}
	ok := decodeRow(t, rows[0])
	var printed map[string]any
	if err := json.Unmarshal([]byte(strings.TrimSpace(str(ok, "output"))), &printed); err != nil {
		t.Fatalf("the script's output = %v", ok)
	}
	if ok["status"] != "success" || ok["exit_code"] != float64(0) || ok["tool_calls_made"] != float64(2) ||
		printed["lines"] != float64(3) || printed["out"] != "from-terminal" || printed["exit"] != float64(0) ||
		printed["key"] != nil || printed["rpc"] != true || printed["cwd"] != cwd {
		t.Fatalf("execute_code = %v, printed %v", ok, printed)
	}
	failed := decodeRow(t, rows[1])
	if failed["status"] != "error" || failed["exit_code"] != float64(1) ||
		!strings.Contains(str(failed, "output"), "before\n--- stderr ---\n") || !strings.Contains(str(failed, "output"), "ZeroDivisionError") {
		t.Fatalf("a failing script = %v", failed)
	}
	long := decodeRow(t, rows[2])
	if long["stdout_truncated"] != true || !strings.Contains(str(long, "output"), "[OUTPUT TRUNCATED - ") ||
		len(str(long, "output")) > 52000 || long["warning"] == nil {
		t.Fatalf("a long output = %.300v", long)
	}
	var desc string
	for _, raw := range fake.Request(0)["tools"].([]any) {
		fn := raw.(map[string]any)["function"].(map[string]any)
		if str(fn, "name") == "execute_code" {
			desc = str(fn, "description")
		}
	}
	if !strings.HasPrefix(desc, "Run a Python script that calls Son of Anton tools") || !strings.Contains(desc, "terminal(command: str") ||
		strings.Contains(desc, "cross_profile") || strings.Contains(desc, "venv's python") {
		t.Fatalf("the schema = %q", desc)
	}
}

func TestADangerousCallFromAScriptAsksTheDriver(t *testing.T) {
	python(t)
	cwd := t.TempDir()
	f := filepath.Join(cwd, "f.txt")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{
			code("x1", "from son_of_anton_tools import terminal\nr = terminal('chmod 777 f.txt')\nprint(r.get('status'), r.get('exit_code'))"),
		}},
		fakellm.Turn{Content: "done"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(cwd)
	c.attach(id, "driver")
	c.send(id, "make it writable")
	req := c.await("approval.request")
	if !strings.Contains(str(req, "subject"), "chmod 777") {
		t.Fatalf("approval = %v", req)
	}
	c.ok("approval.answer", map[string]any{"session": id, "approval_id": str(req, "approval_id"), "choice": "deny"})
	rows := toolRows(c.turn())
	if len(rows) != 1 || !strings.Contains(str(decodeRow(t, rows[0]), "output"), "blocked -1") {
		t.Fatalf("the script saw the denial: %v", rows)
	}
	if fi, _ := os.Stat(f); fi.Mode().Perm() == 0o777 {
		t.Fatal("a denied chmod ran")
	}
}

func pngOf(t *testing.T, w, h int) []byte {
	t.Helper()
	img := image.NewRGBA(image.Rect(0, 0, w, h))
	for y := range h {
		for x := range w {
			img.Set(x, y, color.RGBA{R: 200, A: 255})
		}
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

// visionImage finds the vision request among the fake's and returns its
// image's size, its prompt text and its temperature.
func visionImages(t *testing.T, fake *fakellm.Server) []image.Config {
	t.Helper()
	var out []image.Config
	for i := 0; i < fake.Count(); i++ {
		var body struct {
			Temperature float64 `json:"temperature"`
			Messages    []struct {
				Content json.RawMessage `json:"content"`
			} `json:"messages"`
		}
		if json.Unmarshal(fake.Bodies()[i], &body) != nil || len(body.Messages) != 1 {
			continue
		}
		var parts []struct {
			Type     string `json:"type"`
			Text     string `json:"text"`
			ImageURL struct {
				URL string `json:"url"`
			} `json:"image_url"`
		}
		if json.Unmarshal(body.Messages[0].Content, &parts) != nil || len(parts) != 2 {
			continue
		}
		if !strings.HasPrefix(parts[0].Text, "Fully describe and explain everything about this image, then answer the following question:\n\n") ||
			body.Temperature != 0.1 {
			t.Fatalf("the vision request: %+v (temperature %v)", parts[0], body.Temperature)
		}
		data, ok := strings.CutPrefix(parts[1].ImageURL.URL, "data:image/png;base64,")
		if !ok {
			t.Fatalf("the image part: %.80s", parts[1].ImageURL.URL)
		}
		raw, err := base64.StdEncoding.DecodeString(data)
		if err != nil {
			t.Fatal(err)
		}
		cfg, err := png.DecodeConfig(bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, cfg)
	}
	return out
}

func TestVisionAnalyzeSendsTheImage(t *testing.T) {
	cwd := t.TempDir()
	if err := os.WriteFile(filepath.Join(cwd, "pic.png"), pngOf(t, 100, 50), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cwd, "notes.txt"), []byte("not an image"), 0o644); err != nil {
		t.Fatal(err)
	}
	asks := "Fully describe and explain everything about this image"
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("v1", "vision_analyze", `{"image_url":"pic.png","question":"what is it?"}`)}},
		fakellm.Turn{When: asks, Content: "<think>hmm</think>a red rectangle"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("v2", "vision_analyze", `{"image_url":"pic.png","question":"zoom in","region":[10,10,30,20]}`)}},
		fakellm.Turn{When: asks, Content: "a red corner"},
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{
			call("v3", "vision_analyze", `{"image_url":"notes.txt","question":"q"}`),
			call("v4", "vision_analyze", `{"image_url":"pic.png","question":"q","region":[50,40,40,30]}`),
			call("v5", "vision_analyze", `{"image_url":"gopher://x/y","question":"q"}`),
		}},
		fakellm.Turn{Content: "done"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(cwd)
	c.attach(id, "driver")
	c.send(id, "look at the picture")
	rows := toolRows(c.turn())
	if len(rows) != 5 {
		t.Fatalf("rows = %v", rows)
	}
	first := decodeRow(t, rows[0])
	if first["success"] != true || first["analysis"] != "a red rectangle" || first["scale_note"] != nil {
		t.Fatalf("vision = %v", first)
	}
	crop := decodeRow(t, rows[1])
	if crop["success"] != true || !strings.HasSuffix(str(crop, "analysis"), "a red corner") ||
		!strings.Contains(str(crop, "scale_note"), "offset (10, 10)") {
		t.Fatalf("a crop = %v", crop)
	}
	sizes := visionImages(t, fake)
	if len(sizes) != 2 || sizes[0].Width != 100 || sizes[0].Height != 50 || sizes[1].Width != 20 || sizes[1].Height != 10 {
		t.Fatalf("images sent = %+v; the whole picture, then the 20x10 crop", sizes)
	}
	for i, want := range map[int]string{2: "not a recognized image", 3: "crops to zero area", 4: "Unrecognized image source scheme"} {
		r := decodeRow(t, rows[i])
		if r["success"] != false || !strings.Contains(str(r, "error")+str(r, "analysis"), want) {
			t.Errorf("row %d = %v; want %q", i, r, want)
		}
	}
}

func TestVisionAnalyzeRefusesSecretFiles(t *testing.T) {
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("v1", "vision_analyze", `{"image_url":".env","question":"q"}`)}},
		fakellm.Turn{Content: "done"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	cwd := t.TempDir()
	if err := os.WriteFile(filepath.Join(cwd, ".env"), pngOf(t, 4, 4), 0o600); err != nil {
		t.Fatal(err)
	}
	id := c.create(cwd)
	c.attach(id, "driver")
	c.send(id, "look")
	rows := toolRows(c.turn())
	if r := decodeRow(t, rows[0]); r["success"] != false || !strings.Contains(str(r, "error"), "secret") {
		t.Fatalf("a secret file = %v", r)
	}
	if fake.Count() != 2 {
		t.Fatal("nothing was sent to a vision model")
	}
}
