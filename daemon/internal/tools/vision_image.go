package tools

import (
	"bytes"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"image"
	"image/color"
	"image/draw"
	_ "image/gif"
	"image/jpeg"
	"image/png"
	"os/exec"
	"strings"
	"time"

	"golang.org/x/image/bmp"
	xdraw "golang.org/x/image/draw"
	_ "golang.org/x/image/tiff"
	_ "golang.org/x/image/webp"
)

const (
	visionMaxSource    = 50 << 20 // any image source
	visionMaxPayload   = 20 << 20 // base64 bytes no provider accepts above
	visionShrinkTarget = 5 << 20  // base64 bytes a shrunk image aims under
)

var errNotImage = errors.New("source is not a recognized image")

// sniffImage names an image's type by its magic bytes ("" for none).
func sniffImage(data []byte) string {
	h := data[:min(len(data), 64)]
	switch {
	case bytes.HasPrefix(h, []byte("\x89PNG\r\n\x1a\n")):
		return "image/png"
	case bytes.HasPrefix(h, []byte("\xff\xd8\xff")):
		return "image/jpeg"
	case bytes.HasPrefix(h, []byte("GIF87a")), bytes.HasPrefix(h, []byte("GIF89a")):
		return "image/gif"
	case bytes.HasPrefix(h, []byte("BM")):
		return "image/bmp"
	case len(h) >= 12 && string(h[:4]) == "RIFF" && string(h[8:12]) == "WEBP":
		return "image/webp"
	case bytes.HasPrefix(h, []byte("II*\x00")), bytes.HasPrefix(h, []byte("MM\x00*")):
		return "image/tiff"
	case bytes.HasPrefix(h, []byte("\x00\x00\x01\x00")):
		return "image/x-icon"
	}
	if bytes.Contains(bytes.ToLower(data[:min(len(data), 4096)]), []byte("<svg")) {
		return "image/svg+xml"
	}
	return ""
}

// normalizeImage returns the image in a type every vision API accepts: PNG,
// JPEG, GIF and WebP pass through; BMP, TIFF and ICO become PNG; SVG goes
// through rsvg-convert.
func normalizeImage(data []byte, mime string) ([]byte, string, error) {
	switch mime {
	case "image/png", "image/jpeg", "image/gif", "image/webp":
		return data, mime, nil
	case "image/svg+xml":
		path, err := exec.LookPath("rsvg-convert")
		if err == nil {
			cmd := exec.Command(path, "-f", "png")
			cmd.Stdin = bytes.NewReader(data)
			var out bytes.Buffer
			cmd.Stdout = &out
			timer := time.AfterFunc(30*time.Second, func() {
				if cmd.Process != nil {
					_ = cmd.Process.Kill()
				}
			})
			err = cmd.Run()
			timer.Stop()
			if err == nil && out.Len() > 0 {
				return out.Bytes(), "image/png", nil
			}
		}
		return nil, "", errors.New("This is an SVG, which vision models cannot read directly, and no " +
			"SVG rasterizer is installed (tried rsvg-convert, inkscape). Convert " +
			"the SVG to PNG first — e.g. open it in a browser " +
			"and screenshot it — then re-run vision_analyze on the PNG.")
	}
	img, err := decodeImage(data, mime)
	if err == nil {
		var out []byte
		if out, err = encodePNG(img); err == nil {
			return out, "image/png", nil
		}
	}
	return nil, "", fmt.Errorf("Image format '%s' is not supported by the vision API "+
		"and could not be converted to PNG. Convert it to PNG or JPEG and try again.", mime)
}

// decodeImage decodes any type the tool takes (the first frame of a GIF).
func decodeImage(data []byte, mime string) (image.Image, error) {
	if mime == "image/x-icon" {
		return decodeICO(data)
	}
	img, _, err := image.Decode(bytes.NewReader(data))
	return img, err
}

func encodePNG(img image.Image) ([]byte, error) {
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// decodeICO decodes the largest image of an icon file: an embedded PNG, or a
// bitmap without its file header.
func decodeICO(data []byte) (image.Image, error) {
	if len(data) < 6 {
		return nil, errors.New("truncated icon")
	}
	n := int(binary.LittleEndian.Uint16(data[4:]))
	var best []byte
	bestArea := -1
	for i := 0; i < n && 6+16*(i+1) <= len(data); i++ {
		e := data[6+16*i:]
		w, h := int(e[0]), int(e[1])
		if w == 0 {
			w = 256
		}
		if h == 0 {
			h = 256
		}
		size, off := int(binary.LittleEndian.Uint32(e[8:])), int(binary.LittleEndian.Uint32(e[12:]))
		if off < 0 || size < 0 || off+size > len(data) || w*h <= bestArea {
			continue
		}
		best, bestArea = data[off:off+size], w*h
	}
	if best == nil {
		return nil, errors.New("icon has no image")
	}
	if bytes.HasPrefix(best, []byte("\x89PNG")) {
		return png.Decode(bytes.NewReader(best))
	}
	if len(best) < 40 {
		return nil, errors.New("truncated icon bitmap")
	}
	hdr := int(binary.LittleEndian.Uint32(best))
	bits := int(binary.LittleEndian.Uint16(best[14:]))
	palette := 0
	if bits <= 8 {
		colors := int(binary.LittleEndian.Uint32(best[32:]))
		if colors == 0 {
			colors = 1 << bits
		}
		palette = colors * 4
	}
	// An icon's bitmap height counts the mask below the pixels too.
	fixed := bytes.Clone(best)
	binary.LittleEndian.PutUint32(fixed[8:], binary.LittleEndian.Uint32(best[8:])/2)
	file := make([]byte, 14, 14+len(fixed))
	copy(file, "BM")
	binary.LittleEndian.PutUint32(file[2:], uint32(14+len(fixed)))
	binary.LittleEndian.PutUint32(file[10:], uint32(14+hdr+palette))
	return bmp.Decode(bytes.NewReader(append(file, fixed...)))
}

// regionOffset is where a crop started in the original image.
type regionOffset struct{ x, y, w, h int }

// cropImage crops data to region = [x1, y1, x2, y2] in pixels of the original
// (clamped, x2 and y2 exclusive) and returns it as PNG.
func cropImage(data []byte, mime string, region []any) ([]byte, *regionOffset, error) {
	var v [4]int
	ok := len(region) == 4
	for i := 0; ok && i < 4; i++ {
		f, isNum := region[i].(float64)
		ok = isNum
		v[i] = int(f)
	}
	if !ok {
		return nil, nil, errors.New("Invalid region: expected [x1, y1, x2, y2] as four numbers " +
			"(pixel coordinates in the original image).")
	}
	img, err := decodeImage(data, mime)
	if err != nil {
		return nil, nil, fmt.Errorf("Failed to crop region: %v", err)
	}
	b := img.Bounds()
	width, height := b.Dx(), b.Dy()
	clamp := func(n, hi int) int { return max(0, min(n, hi)) }
	cx1, cy1, cx2, cy2 := clamp(v[0], width), clamp(v[1], height), clamp(v[2], width), clamp(v[3], height)
	if cx2 <= cx1 || cy2 <= cy1 {
		return nil, nil, fmt.Errorf("Invalid region [%d, %d, %d, %d]: crops to zero "+
			"area after clamping to the image bounds. The image is "+
			"%dx%d px — pick x1<x2 and y1<y2 inside "+
			"[0, 0, %d, %d].", v[0], v[1], v[2], v[3], width, height, width, height)
	}
	dst := image.NewNRGBA(image.Rect(0, 0, cx2-cx1, cy2-cy1))
	draw.Draw(dst, dst.Bounds(), img, image.Pt(b.Min.X+cx1, b.Min.Y+cy1), draw.Src)
	out, err := encodePNG(dst)
	if err != nil {
		return nil, nil, fmt.Errorf("Failed to crop region: %v", err)
	}
	return out, &regionOffset{cx1, cy1, cx2 - cx1, cy2 - cy1}, nil
}

// scaleInfo is a downscale: the original and the new size.
type scaleInfo struct{ ow, oh, nw, nh int }

// scaleNote tells the model how coordinates map back to the original image
// when the picture it saw was cropped or scaled down; "" for neither.
func scaleNote(s *scaleInfo, c *regionOffset) string {
	var parts []string
	if s != nil {
		fx, fy := 1.0, 1.0
		if s.nw != 0 {
			fx = float64(s.ow) / float64(s.nw)
		}
		if s.nh != 0 {
			fy = float64(s.oh) / float64(s.nh)
		}
		var clause string
		if fmt.Sprintf("%.2f", fx) == fmt.Sprintf("%.2f", fy) {
			clause = fmt.Sprintf("multiply any coordinates you report by %.2f to map back to the original image.", fx)
		} else {
			clause = fmt.Sprintf("multiply any x coordinates you report by %.2f and any y coordinates by %.2f to map back to the original image.", fx, fy)
		}
		parts = append(parts, fmt.Sprintf("Image downscaled from %dx%d to %dx%d for vision; %s", s.ow, s.oh, s.nw, s.nh, clause))
	}
	if c != nil {
		parts = append(parts, fmt.Sprintf("Analysis was performed on a cropped region of the original "+
			"image starting at offset (%d, %d); coordinates are relative to that crop "+
			"origin — add the offset to map back to the full image.", c.x, c.y))
	}
	return strings.Join(parts, " ")
}

func dataURL(mime string, data []byte) string {
	return "data:" + mime + ";base64," + base64.StdEncoding.EncodeToString(data)
}

// shrinkImage scales an image down, halving each side (at least 64 px), until
// its data URL fits under limit: PNG stays PNG, others become JPEG at quality
// 85, 70, 50. It returns the best attempt (which may still be over) and, when
// the size changed, how.
func shrinkImage(data []byte, mime string, limit int) (string, *scaleInfo) {
	img, err := decodeImage(data, mime)
	if err != nil {
		return dataURL(mime, data), nil
	}
	isPNG := mime == "image/png"
	outMime, qualities := "image/jpeg", []int{85, 70, 50}
	if isPNG {
		outMime, qualities = "image/png", []int{0}
	}
	b := img.Bounds()
	ow, oh := b.Dx(), b.Dy()
	w, h := ow, oh
	var best string
	info := func() *scaleInfo {
		if w == ow && h == oh {
			return nil
		}
		return &scaleInfo{ow, oh, w, h}
	}
	for attempt := range 5 {
		if attempt > 0 {
			nw, nh := max(w/2, 64), max(h/2, 64)
			if nw == 64 && w > 0 {
				nh = max(int(float64(h)*64/float64(w)), 64)
			} else if nh == 64 && h > 0 {
				nw = max(int(float64(w)*64/float64(h)), 64)
			}
			if nw == w && nh == h {
				break
			}
			dst := image.NewNRGBA(image.Rect(0, 0, nw, nh))
			xdraw.CatmullRom.Scale(dst, dst.Bounds(), img, img.Bounds(), xdraw.Src, nil)
			img, w, h = dst, nw, nh
		}
		for _, q := range qualities {
			var buf bytes.Buffer
			if isPNG {
				err = png.Encode(&buf, img)
			} else {
				err = jpeg.Encode(&buf, opaque(img), &jpeg.Options{Quality: q})
			}
			if err != nil {
				return dataURL(mime, data), nil
			}
			best = dataURL(outMime, buf.Bytes())
			if len(best) <= limit {
				return best, info()
			}
		}
	}
	return best, info()
}

// opaque draws img over white, for a format without transparency.
func opaque(img image.Image) image.Image {
	dst := image.NewRGBA(img.Bounds())
	draw.Draw(dst, dst.Bounds(), image.NewUniform(color.White), image.Point{}, draw.Src)
	draw.Draw(dst, dst.Bounds(), img, img.Bounds().Min, draw.Over)
	return dst
}
