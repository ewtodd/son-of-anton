package tools

// SniffImage tells an image from text by the bytes alone, and text that
// merely mentions markup or starts the way a bitmap does is text (owner,
// 2026-11-06): read the BMP header as a header, and count SVG as an image
// only when svg is the document's root element. images.go itself, which holds
// the string "<svg", is text.

import (
	_ "embed"
	"encoding/binary"
	"testing"
)

//go:embed images.go
var imagesGO []byte

// tinyBMP is a real 2x2 24-bpp bitmap: a 14-byte file header, a 40-byte
// BITMAPINFOHEADER, two padded pixel rows.
func tinyBMP() []byte {
	b := make([]byte, 14+40+2*8)
	copy(b, "BM")
	binary.LittleEndian.PutUint32(b[2:], uint32(len(b))) // the file's size
	binary.LittleEndian.PutUint32(b[10:], 54)            // pixel data at 54
	binary.LittleEndian.PutUint32(b[14:], 40)            // BITMAPINFOHEADER
	binary.LittleEndian.PutUint32(b[18:], 2)             // width
	binary.LittleEndian.PutUint32(b[22:], 2)             // height
	binary.LittleEndian.PutUint16(b[26:], 1)             // planes
	binary.LittleEndian.PutUint16(b[28:], 24)            // bits per pixel
	return b
}

const svgFile = `<?xml version="1.0" encoding="UTF-8"?>
<!-- drawn by hand -->
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24">
  <rect width="24" height="24" fill="red"/>
</svg>
`

func TestSniffImageTakesRealImagesOnly(t *testing.T) {
	for name, tc := range map[string]struct {
		data []byte
		mime string
	}{
		"real bmp":       {tinyBMP(), "image/bmp"},
		"real svg":       {[]byte(svgFile), "image/svg+xml"},
		"bare svg root":  {[]byte("<svg xmlns=\"http://www.w3.org/2000/svg\"/>"), "image/svg+xml"},
		"bm25 text":      {[]byte("BM25 weights the query terms.\nIt is not a bitmap.\n"), ""},
		"bm alone":       {[]byte("BM"), ""},
		"svg in prose":   {[]byte("# Plan\n\nDraw the icon as `<svg viewBox=\"0 0 8 8\"/>` in the README.\n"), ""},
		"svg in tsx":     {[]byte("export const Icon = () => <svg width=\"12\" height=\"12\" />;\n"), ""},
		"svg in html":    {[]byte("<html><body>\n<p>the shape</p>\n<svg viewBox=\"0 0 8 8\"></svg>\n</body></html>\n"), ""},
		"svg not a root": {[]byte("<svgfoo width=\"8\"/>"), ""},
		"svg uppercase":  {[]byte("<SVG width=\"8\"/>"), ""},
		"svg mid file":   {[]byte("look:\n<svg width=\"8\"/>\n"), ""},
		"images.go":      {imagesGO, ""},
		"c code":         {[]byte("const char *bm = \"BMP\";\nvoid main() {}\n"), ""},
	} {
		if got := SniffImage(tc.data); got != tc.mime {
			t.Errorf("%s: SniffImage = %q, want %q", name, got, tc.mime)
		}
	}
}

func TestATinyRealBMPStillConverts(t *testing.T) {
	out, mime, _, err := PrepareImage(tinyBMP(), "image/bmp")
	if err != nil || mime != "image/png" || string(out[:8]) != "\x89PNG\r\n\x1a\n" {
		t.Fatalf("PrepareImage(2x2 BMP) = (%d bytes, %s, %v)", len(out), mime, err)
	}
}
