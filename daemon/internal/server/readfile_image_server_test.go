package server_test

// Phase 7c through the daemon: execute_code
// (daemon/phases/07c-code-vision.md); execute_code needs a Python 3, so the
// tests that run scripts skip without one. The phase's other tool changed:
// vision_analyze is gone entirely, and images reach the model's own vision
// encoder as message parts (a Read result, a chat attachment) — a
// text-only model gets an error instead.

import (
	"bytes"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
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
			code("x1", "import json, os\nfrom son_of_anton_tools import read, bash\n"+
				"r = read('notes.txt')\nt = bash('echo from-bash')\n"+
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
		printed["lines"] != float64(3) || printed["out"] != "from-bash" || printed["exit"] != float64(0) ||
		printed["key"] != nil || printed["rpc"] != true || printed["cwd"] != cwd {
		t.Fatalf("execute_code = %v, printed %v", ok, printed)
	}
	failed := decodeRow(t, rows[1])
	if failed["status"] != "error" || failed["exit_code"] != float64(1) ||
		!strings.Contains(str(failed, "output"), "before\n--- stderr ---\n") || !strings.Contains(str(failed, "output"), "ZeroDivisionError") {
		t.Fatalf("a failing script = %v", failed)
	}
	long := decodeRow(t, rows[2])
	if long["stdout_truncated"] != true || !strings.Contains(str(long, "output"), "[output cut: ") ||
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
	if !strings.HasPrefix(desc, "Execute a Python 3 script that drives this session's tools") || !strings.Contains(desc, "bash(command: str") ||
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
			code("x1", "from son_of_anton_tools import bash\nr = bash('chmod 777 f.txt')\nprint(r.get('status'), r.get('exit_code'))"),
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

// toolWire returns a request's tool message as the wire holds it, fields and
// all (the role's own ones included).
func toolWire(t *testing.T, fake *fakellm.Server, i int) map[string]any {
	t.Helper()
	for _, m := range rawWire(t, fake, i) {
		if str(m, "role") == "tool" {
			return m
		}
	}
	t.Fatalf("request %d has no tool message", i)
	return nil
}

// toolRowParts decodes a tool message's content from the wire: an array of
// parts when the row carries images, a plain string otherwise.
func toolRowParts(t *testing.T, fake *fakellm.Server, i int) (string, []map[string]any) {
	t.Helper()
	for _, m := range rawWire(t, fake, i) {
		if str(m, "role") != "tool" {
			continue
		}
		if parts, ok := m["content"].([]any); ok {
			text := ""
			var out []map[string]any
			for _, p := range parts {
				pm, _ := p.(map[string]any)
				if str(pm, "type") == "text" {
					text = str(pm, "text")
				}
				out = append(out, pm)
			}
			return text, out
		}
		return str(m, "content"), nil
	}
	t.Fatalf("request %d has no tool message", i)
	return "", nil
}

// decodeDataURL reads a data: image part's bytes.
func decodeDataURL(t *testing.T, url string) []byte {
	t.Helper()
	head, data, ok := strings.Cut(url, ";base64,")
	if !ok || !strings.HasPrefix(head, "data:image/") {
		t.Fatalf("not an image data URL: %.80s", url)
	}
	raw, err := base64.StdEncoding.DecodeString(data)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestReadFileAttachesTheImage(t *testing.T) {
	cwd := t.TempDir()
	if err := os.WriteFile(filepath.Join(cwd, "pic.png"), pngOf(t, 100, 50), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("r1", "Read", `{"file_path":"pic.png"}`)}},
		fakellm.Turn{Content: "done"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(cwd)
	c.attach(id, "driver")
	c.send(id, "look at the picture")
	evs := c.turn()
	rows := toolRows(evs)
	if len(rows) != 1 {
		t.Fatalf("rows = %v", rows)
	}
	res := decodeRow(t, rows[0])
	img := obj(res, "image")
	if res["error"] != nil || !strings.Contains(str(res, "content"), "attached to this result") ||
		img == nil || str(img, "mime") != "image/png" || str(img, "name") != "pic.png" || str(img, "id") == "" {
		t.Fatalf("the read = %v", res)
	}
	text, parts := toolRowParts(t, fake, 1)
	if len(parts) != 2 || !strings.Contains(text, "attached to this result") {
		t.Fatalf("the tool message = %q, %d parts", text, len(parts))
	}
	// The row's own fields ride the image path too: a tool message without
	// its call id and tool name is not the message that was sent.
	if wire := toolWire(t, fake, 1); str(wire, "tool_call_id") != "r1" || str(wire, "name") != "Read" {
		t.Fatalf("the wire tool message lost its fields: %v", wire)
	}
	for _, p := range parts {
		if str(p, "type") != "image_url" {
			continue
		}
		raw := decodeDataURL(t, str(obj(p, "image_url"), "url"))
		cfg, err := png.DecodeConfig(bytes.NewReader(raw))
		if err != nil || cfg.Width != 100 || cfg.Height != 50 {
			t.Fatalf("the attached image = %+v, %v", cfg, err)
		}
	}
	// The stored row carries the image, as the message events told the viewer.
	for _, m := range committed(evs) {
		if str(m, "role") != "tool" {
			continue
		}
		list := objs(m, "images")
		if len(list) != 1 || str(list[0], "id") != str(img, "id") || str(list[0], "mime") != "image/png" {
			t.Fatalf("the row's images = %v", m["images"])
		}
	}
}

// writeHomeVision writes a config whose test-model declares supports_vision.
func writeHomeVision(t *testing.T, home, url, vision string) *harness {
	t.Helper()
	cfg := "[model]\ndefault = \"test-model\"\nprovider = \"custom\"\n\n" +
		"[custom_providers.custom]\nbase_url = \"" + url + "\"\nkey_env = \"SOA_D_TEST_KEY\"\n\n" +
		"[custom_providers.custom.models.\"test-model\"]\nsupports_vision = " + vision + "\nreasoning_efforts = [\"low\", \"high\"]\n\n" +
		"[custom_providers.custom.models.\"other-model\"]\n"
	if err := os.WriteFile(filepath.Join(home, "config.toml"), []byte(cfg), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(home, ".env"), []byte("SOA_D_TEST_KEY=s3cret\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	h := &harness{t: t, home: home}
	h.start()
	t.Cleanup(func() { _ = h.stop() })
	return h
}

func TestReadImageErrorsOnATextOnlyModel(t *testing.T) {
	home, err := os.MkdirTemp("", "soad")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(home) })
	t.Setenv("SOA_D_TEST_KEY", "")
	t.Setenv("SON_OF_ANTON_HOME", "")
	cwd := t.TempDir()
	if err := os.WriteFile(filepath.Join(cwd, "pic.png"), pngOf(t, 8, 8), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("r1", "Read", `{"file_path":"pic.png"}`)}},
		fakellm.Turn{Content: "done"},
	)
	h := writeHomeVision(t, home, fake.URL, "false")
	c := h.dial()
	id := c.create(cwd)
	c.attach(id, "driver")
	c.send(id, "look")
	rows := toolRows(c.turn())
	if len(rows) != 1 {
		t.Fatalf("rows = %v", rows)
	}
	res := decodeRow(t, rows[0])
	if res["error"] == nil || !strings.Contains(str(res, "error"), "no vision") {
		t.Fatalf("a text-only model read = %v", res)
	}
	if content, parts := toolRowParts(t, fake, 1); parts != nil || !strings.Contains(content, "no vision") {
		t.Fatalf("something reached the model as parts: %q", content)
	}
}

// hostileBMP is the 54-byte BMP that took the daemon down (owner, 2026-11-06):
// "BM", pixel offset 54, a 40-byte BITMAPINFOHEADER, planes 1, 24 bpp and a
// claimed rectangle of 0x7fffffff x 0x7fffffff.
func hostileBMP() []byte {
	b := make([]byte, 54)
	copy(b, "BM")
	binary.LittleEndian.PutUint32(b[10:], 54)
	binary.LittleEndian.PutUint32(b[14:], 40)
	binary.LittleEndian.PutUint32(b[18:], 0x7fffffff)
	binary.LittleEndian.PutUint32(b[22:], 0x7fffffff)
	binary.LittleEndian.PutUint16(b[26:], 1)
	binary.LittleEndian.PutUint16(b[28:], 24)
	return b
}

// A Read of a hostile image reaches PrepareImage on the model's turn:
// it must answer the model with an error and leave the daemon serving, not
// panic soa-d (and its session) out of the air.
func TestABadImageFailsTheReadNotTheDaemon(t *testing.T) {
	cwd := t.TempDir()
	if err := os.WriteFile(filepath.Join(cwd, "bad.bmp"), hostileBMP(), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("r1", "Read", `{"file_path":"bad.bmp"}`)}},
		fakellm.Turn{Content: "still here"},
		fakellm.Turn{Content: "done again"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(cwd)
	c.attach(id, "driver")
	c.send(id, "look at the picture")
	rows := toolRows(c.turn())
	if len(rows) != 1 {
		t.Fatalf("rows = %v", rows)
	}
	res := decodeRow(t, rows[0])
	if res["error"] == nil || res["image"] != nil {
		t.Fatalf("the hostile BMP read = %v", res)
	}
	// The daemon made it through: the next turn goes round as usual.
	c.send(id, "again")
	if rows = toolRows(c.turn()); len(rows) != 0 {
		t.Fatalf("the turn after the BMP = %v", rows)
	}
}

// Text that merely looks like an image reads as text (owner, 2026-11-06): a
// file whose bytes start "BM25", markdown or code that mentions <svg>, and
// images.go itself — Read's own source, which holds the string. A real
// .svg reads as its source too, so it stays editable; a session with vision
// is handed the render beside it when rsvg-convert works, and a session
// without sees the source alone and is told nothing is missing.
func TestReadFileReadsTextThatMerelyLooksLikeAnImage(t *testing.T) {
	cwd := t.TempDir()
	src, err := os.ReadFile(filepath.Join("..", "tools", "images.go"))
	if err != nil {
		t.Fatal(err)
	}
	files := map[string]string{
		"BM25.txt": "BM25 weights the query terms.\nIt is not a bitmap.\n",
		"notes.md": "# Plan\n\nDraw the icon as `<svg viewBox=\"0 0 8 8\"/>` in the README.\n",
		"icon.tsx": "export const Icon = () => <svg width=\"12\" height=\"12\" />;\n",
		"page.html": `<html><body>
<p>the shape</p>
<svg viewBox="0 0 8 8"></svg>
</body></html>
`,
		"images.go": string(src),
		"pict.svg": `<?xml version="1.0" encoding="UTF-8"?>
<!-- drawn by hand -->
<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24">
  <rect width="24" height="24" fill="red"/>
</svg>
`,
	}
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(cwd, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	var calls []fakellm.ToolCall
	for i, name := range []string{"BM25.txt", "notes.md", "icon.tsx", "page.html", "images.go", "pict.svg"} {
		calls = append(calls, call(fmt.Sprintf("r%d", i), "Read", fmt.Sprintf(`{"file_path":%q}`, name)))
	}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: calls},
		fakellm.Turn{Content: "done"},
	)
	h := newHarness(t, fake, "")
	c := h.dial()
	id := c.create(cwd)
	c.attach(id, "driver")
	c.send(id, "read these six files")
	rows := toolRows(c.turn())
	if len(rows) != len(files) {
		t.Fatalf("rows = %v", rows)
	}
	for i, name := range []string{"BM25.txt", "notes.md", "icon.tsx", "page.html", "images.go", "pict.svg"} {
		res := decodeRow(t, rows[i])
		if res["error"] != nil {
			t.Fatalf("Read(%s) = %v; plain text, and images.go among it, must read", name, res)
		}
		if !strings.Contains(str(res, "content"), strings.TrimSpace(strings.SplitN(files[name], "\n", 2)[0])) {
			t.Fatalf("Read(%s) did not read the source: %.200v", name, res)
		}
		if name != "pict.svg" && obj(res, "image") != nil {
			t.Fatalf("Read(%s) attached an image: %v", name, res)
		}
	}
	// The .svg: its source is what came back, and, with rsvg-convert on PATH
	// and vision on, the render rides the result too.
	svg := decodeRow(t, rows[5])
	if !strings.Contains(str(svg, "content"), "<svg") {
		t.Fatalf("the .svg did not read as source: %.300v", svg)
	}
	render, _ := exec.LookPath("rsvg-convert")
	if (render != "") != (obj(svg, "image") != nil) {
		t.Fatalf("rsvg-convert %q but the .svg image = %v", render, obj(svg, "image"))
	}
}

// Without vision the .svg still reads as its source: a text file is a text
// file, whatever the model can see.
func TestASVGFileReadsWithoutVision(t *testing.T) {
	home, err := os.MkdirTemp("", "soad")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(home) })
	t.Setenv("SOA_D_TEST_KEY", "")
	t.Setenv("SON_OF_ANTON_HOME", "")
	cwd := t.TempDir()
	svg := `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24">
  <rect width="24" height="24" fill="red"/>
</svg>
`
	if err := os.WriteFile(filepath.Join(cwd, "pict.svg"), []byte(svg), 0o644); err != nil {
		t.Fatal(err)
	}
	fake := fakellm.New(t,
		fakellm.Turn{ToolCalls: []fakellm.ToolCall{call("r1", "Read", `{"file_path":"pict.svg"}`)}},
		fakellm.Turn{Content: "done"},
	)
	h := writeHomeVision(t, home, fake.URL, "false")
	c := h.dial()
	id := c.create(cwd)
	c.attach(id, "driver")
	c.send(id, "look")
	rows := toolRows(c.turn())
	if len(rows) != 1 {
		t.Fatalf("rows = %v", rows)
	}
	res := decodeRow(t, rows[0])
	if res["error"] != nil || res["image"] != nil || !strings.Contains(str(res, "content"), "<svg") {
		t.Fatalf("a .svg without vision = %v", res)
	}
}
