package tools

// The image decoders run on untrusted files. The 54-byte BMP of the daemon
// crash (owner, 2026-11-06) claims 0x7fffffff x 0x7fffffff 24-bit pixels and
// a pixel offset of exactly its length, so it sniffs, checks out linearly and
// then panics the decoder on allocation; an ICO wrapping the same bitmap
// reaches it through the icon path. Both are errors from PrepareImage now,
// wherever they come from — read_file, a chat attachment, a shrunken
// oversize image — never a panic that takes down soa-d.

import (
	"bytes"
	"encoding/binary"
	"testing"
)

// hostileBMP is the crash's BMP: file header, BITMAPINFOHEADER, one plane,
// 24 bpp, and a pixel rectangle no decoder survives.
func hostileBMP() []byte {
	b := make([]byte, 54)
	copy(b, "BM")
	binary.LittleEndian.PutUint32(b[10:], 54) // pixel data starts at 54
	binary.LittleEndian.PutUint32(b[14:], 40) // BITMAPINFOHEADER
	binary.LittleEndian.PutUint32(b[18:], 0x7fffffff)
	binary.LittleEndian.PutUint32(b[22:], 0x7fffffff)
	binary.LittleEndian.PutUint16(b[26:], 1)  // planes
	binary.LittleEndian.PutUint16(b[28:], 24) // bits per pixel
	return b
}

// hostileICO wraps hostileBMP as an icon's one image (the entry's width and
// height are the icon directory's 0 = 256; the bitmap's own header carries
// the absurd dimensions).
func hostileICO() []byte {
	ico := make([]byte, 22)
	binary.LittleEndian.PutUint16(ico[2:], 1) // type: icon
	binary.LittleEndian.PutUint16(ico[4:], 1) // one image
	binary.LittleEndian.PutUint32(ico[14:], 54)
	binary.LittleEndian.PutUint32(ico[18:], 22)
	return append(ico, hostileBMP()...)
}

func TestABadImageIsAnErrorNotAPanic(t *testing.T) {
	for name, tc := range map[string]struct {
		data []byte
		mime string
	}{
		"bmp": {hostileBMP(), "image/bmp"},
		"ico": {hostileICO(), "image/x-icon"},
	} {
		out, mime, note, err := PrepareImage(tc.data, tc.mime)
		if err == nil {
			t.Fatalf("%s: PrepareImage succeeded (%d bytes, %s, %q)", name, len(out), mime, note)
		}
	}
}

func TestShrinkImageLeavesABadImageAlone(t *testing.T) {
	bmp := hostileBMP()
	out, outMime, info := shrinkImage(bmp, "image/bmp", 1<<20)
	if info != nil {
		t.Fatalf("a BMP that does not decode was scaled: %+v", info)
	}
	if outMime != "image/bmp" || !bytes.Equal(out, bmp) {
		t.Fatalf("a BMP that does not decode was rewritten: %s, %d bytes", outMime, len(out))
	}
}

func TestABigButSaneImageStillConverts(t *testing.T) {
	// 6 megapixels: over a phone snapshot, far under the pixel budget —
	// a picture a model may actually be handed.
	data := noisePNG(t, 3000, 2000)
	if _, mime, _, err := PrepareImage(data, "image/png"); err != nil {
		t.Fatalf("a 6-megapixel PNG was refused (%s): %v", mime, err)
	}
}
