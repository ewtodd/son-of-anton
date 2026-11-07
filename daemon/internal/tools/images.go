package tools

import (
	"bytes"
	"context"
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
	"time"

	"golang.org/x/image/bmp"
	xdraw "golang.org/x/image/draw"
	_ "golang.org/x/image/tiff"
	_ "golang.org/x/image/webp"

	"github.com/ewtodd/son-of-anton/daemon/internal/store"
)

const (
	// imgMaxSource is the most any image source may weigh in bytes.
	imgMaxSource = 50 << 20
	// imgPayloadLimit is the data-URL size above which an image is scaled
	// down before it enters a conversation (a modest request body).
	imgPayloadLimit = 7 << 20
	// imgShrinkTarget is the data-URL size a shrunk image aims under.
	imgShrinkTarget = 5 << 20
)

// SniffImage names an image's type by its magic bytes ("" for none). Text
// that merely looks the part is text: a BMP needs a header, not two letters,
// and <svg> has to open the document, not turn up inside it.
func SniffImage(data []byte) string {
	h := data[:min(len(data), 64)]
	switch {
	case bytes.HasPrefix(h, []byte("\x89PNG\r\n\x1a\n")):
		return "image/png"
	case bytes.HasPrefix(h, []byte("\xff\xd8\xff")):
		return "image/jpeg"
	case bytes.HasPrefix(h, []byte("GIF87a")), bytes.HasPrefix(h, []byte("GIF89a")):
		return "image/gif"
	case bytes.HasPrefix(h, []byte("BM")) && bmpHeader(data):
		return "image/bmp"
	case len(h) >= 12 && string(h[:4]) == "RIFF" && string(h[8:12]) == "WEBP":
		return "image/webp"
	case bytes.HasPrefix(h, []byte("II*\x00")), bytes.HasPrefix(h, []byte("MM\x00*")):
		return "image/tiff"
	case bytes.HasPrefix(h, []byte("\x00\x00\x01\x00")):
		return "image/x-icon"
	}
	if svgRoot(data) {
		return "image/svg+xml"
	}
	return ""
}

// bmpHeader reads a BMP file header as a header: 14 bytes of it, a DIB size
// one of the specs name, and a pixel offset inside the file.
func bmpHeader(data []byte) bool {
	if len(data) < 14+12 { // the smallest DIB there is
		return false
	}
	switch binary.LittleEndian.Uint32(data[14:18]) {
	case 12, 40, 52, 56, 64, 108, 124: // BITMAPCOREHEADER … BITMAPV5HEADER
	default:
		return false
	}
	return binary.LittleEndian.Uint32(data[10:14]) <= uint32(len(data))
}

// svgRoot reports whether data's document element is svg: an XML declaration,
// comments, a doctype and whitespace may lead, and then the first element must
// be svg itself (exactly that name). A stray <svg in prose, JSX or an inline
// shape does not make a file an image.
func svgRoot(data []byte) bool {
	s := bytes.TrimPrefix(data, []byte("\xef\xbb\xbf")) // a BOM may lead the declaration
	s = s[:min(len(s), 4096)]
	for {
		s = bytes.TrimLeft(s, " \t\r\n")
		var i int
		switch {
		case bytes.HasPrefix(s, []byte("<?xml")):
			if i = bytes.Index(s, []byte("?>")); i < 0 {
				return false
			}
			s = s[i+2:]
		case bytes.HasPrefix(s, []byte("<!--")):
			if i = bytes.Index(s, []byte("-->")); i < 0 {
				return false
			}
			s = s[i+4:]
		case bytes.HasPrefix(s, []byte("<!DOCTYPE")), bytes.HasPrefix(s, []byte("<!doctype")):
			if i = bytes.IndexByte(s, '>'); i < 0 {
				return false
			}
			s = s[i+1:]
		default:
			if !bytes.HasPrefix(s, []byte("<svg")) {
				return false
			}
			if len(s) == 4 {
				return false
			}
			// The name goes on with a letter, so <svgfoo is another element;
			// a name ends before a space, a newline, '>' or '/'.
			switch s[4] {
			case ' ', '\t', '\r', '\n', '>', '/':
				return true
			}
			return false
		}
	}
}

// PrepareImage returns an image in a form a chat-completions endpoint takes
// in a message part: PNG, JPEG, GIF and WebP pass through; BMP, TIFF and ICO
// become PNG; SVG goes through rsvg-convert when it is on PATH. An image
// whose data URL would be over the payload limit is scaled down. The note,
// when set, says how what the model will see differs from the original.
func PrepareImage(data []byte, mime string) ([]byte, string, string, error) {
	switch mime {
	case "image/png", "image/jpeg", "image/gif", "image/webp":
		// pass through
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
				return out.Bytes(), "image/png", "", nil
			}
		}
		return nil, "", "", errors.New("models cannot read SVG, and no SVG converter is installed " +
			"(rsvg-convert was not found). Turn the file into a PNG first, for instance by " +
			"screenshotting it in a browser or with a converter.")
	default:
		img, err := decodeImage(data, mime)
		if err == nil {
			var out []byte
			if out, err = encodePNG(img); err == nil {
				data, mime = out, "image/png"
			}
		}
		if err != nil {
			return nil, "", "", fmt.Errorf("'%s' cannot be attached to a message and could not be "+
				"turned into PNG. Save it as PNG or JPEG and try again.", mime)
		}
	}
	if len(dataURL(mime, data)) <= imgPayloadLimit {
		return data, mime, "", nil
	}
	out, outMime, info := shrinkImage(data, mime, imgShrinkTarget)
	return out, outMime, shrinkNote(info), nil
}

// shrinkNote tells the model how coordinates map back to the original image
// when the picture it will see was scaled down; "" when it was not.
func shrinkNote(s *scaleInfo) string {
	if s == nil {
		return ""
	}
	fx, fy := 1.0, 1.0
	if s.nw != 0 {
		fx = float64(s.ow) / float64(s.nw)
	}
	if s.nh != 0 {
		fy = float64(s.oh) / float64(s.nh)
	}
	var clause string
	if fmt.Sprintf("%.2f", fx) == fmt.Sprintf("%.2f", fy) {
		clause = fmt.Sprintf("scale any coordinates you give by %.2f to get original-image positions.", fx)
	} else {
		clause = fmt.Sprintf("scale x coordinates by %.2f and y coordinates by %.2f to get original-image positions.", fx, fy)
	}
	return fmt.Sprintf("Image downscaled from %dx%d to %dx%d before it was attached; %s", s.ow, s.oh, s.nw, s.nh, clause)
}

// imgMaxPixels is the most pixels a source's declared dimensions may name.
// Two letters and a header can claim anything; a decoder otherwise happily
// allocates (or panics on) 2^31 x 2^31, and no picture a model should see
// comes near this.
const imgMaxPixels = 64 << 20

// checkImageSize refuses a source whose declared rectangle is empty or past
// the pixel budget: the decoders place little stock in what a header says.
func checkImageSize(what string, cfg image.Config) error {
	if cfg.Width <= 0 || cfg.Height <= 0 {
		return fmt.Errorf("%s declares %dx%d pixels", what, cfg.Width, cfg.Height)
	}
	if int64(cfg.Width)*int64(cfg.Height) > imgMaxPixels {
		return fmt.Errorf("%s declares %dx%d pixels, past the %d most an image takes in",
			what, cfg.Width, cfg.Height, imgMaxPixels)
	}
	return nil
}

// decodeImage decodes any type an image source may hold (the first frame of
// a GIF). The input bytes are untrusted: the dimensions are read first and
// refused when no picture needs them, and a decoder that still panics on the
// absurd (image.NewRGBA refuses a huge Rectangle) becomes an error.
func decodeImage(data []byte, mime string) (img image.Image, err error) {
	defer func() {
		if r := recover(); r != nil {
			img, err = nil, fmt.Errorf("the %s decode failed: %v", mime, r)
		}
	}()
	if mime == "image/x-icon" {
		return decodeICO(data)
	}
	cfg, _, err := image.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	if err := checkImageSize("the image", cfg); err != nil {
		return nil, err
	}
	img, _, err = image.Decode(bytes.NewReader(data))
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
	full := append(file, fixed...)
	// The bitmap's own header is as untrusted as any file's: read the size
	// before a decoder builds pixels out of the claim.
	cfg, err := bmp.DecodeConfig(bytes.NewReader(full))
	if err != nil {
		return nil, err
	}
	if err := checkImageSize("the icon bitmap", cfg); err != nil {
		return nil, err
	}
	return bmp.Decode(bytes.NewReader(full))
}

// scaleInfo is a downscale: the original and the new size.
type scaleInfo struct{ ow, oh, nw, nh int }

func dataURL(mime string, data []byte) string {
	return "data:" + mime + ";base64," + base64.StdEncoding.EncodeToString(data)
}

// shrinkImage scales an image down, halving each side (at least 64 px), until
// its data URL fits under limit: PNG stays PNG, others become JPEG at quality
// 85, 70, 50. It returns the best attempt (which may still be over) and, when
// the size changed, how.
func shrinkImage(data []byte, mime string, limit int) ([]byte, string, *scaleInfo) {
	img, err := decodeImage(data, mime)
	if err != nil {
		return data, mime, nil
	}
	isPNG := mime == "image/png"
	outMime, qualities := "image/jpeg", []int{85, 70, 50}
	if isPNG {
		outMime, qualities = "image/png", []int{0}
	}
	b := img.Bounds()
	ow, oh := b.Dx(), b.Dy()
	w, h := ow, oh
	var best []byte
	bestMime := mime
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
				return data, mime, nil
			}
			best, bestMime = bytes.Clone(buf.Bytes()), outMime
			if len(dataURL(outMime, buf.Bytes())) <= limit {
				return best, outMime, info()
			}
		}
	}
	if best == nil {
		return data, mime, nil
	}
	return best, bestMime, info()
}

// opaque draws img over white, for a format without transparency.
func opaque(img image.Image) image.Image {
	dst := image.NewRGBA(img.Bounds())
	draw.Draw(dst, dst.Bounds(), image.NewUniform(color.White), image.Point{}, draw.Src)
	draw.Draw(dst, dst.Bounds(), img, img.Bounds().Min, draw.Over)
	return dst
}

// ImageSink stores the images a tool result attaches to the model: each Add
// puts the bytes in the session's media (content-addressed, so the same image
// is stored once) and collects the reference that rides the result's row.
type ImageSink struct {
	store  *store.Store
	images []store.Image
	cap    int
}

// maxImagesPerResult bounds one tool result's attachments, so a single call
// cannot flood the next request.
const maxImagesPerResult = 8

// NewImageSink returns a sink storing images in the session's media.
func NewImageSink(st *store.Store) *ImageSink {
	return &ImageSink{store: st, cap: maxImagesPerResult}
}

// Add stores an image (at most imgMaxSource bytes) and collects its
// reference. It returns the id, which the tool's text result may name.
func (s *ImageSink) Add(ctx context.Context, mime, name string, data []byte) (string, error) {
	if s == nil || s.store == nil {
		return "", errors.New("images cannot be attached here")
	}
	if len(s.images) >= s.cap {
		return "", fmt.Errorf("at most %d images can be attached to one result", s.cap)
	}
	if len(data) > imgMaxSource {
		return "", fmt.Errorf("the image is %d bytes; the most allowed is %d", len(data), imgMaxSource)
	}
	img, err := s.store.PutMedia(ctx, mime, name, data)
	if err != nil {
		return "", err
	}
	s.images = append(s.images, img)
	return img.ID, nil
}

// Images returns the references collected so far.
func (s *ImageSink) Images() []store.Image {
	if s == nil {
		return nil
	}
	return s.images
}
