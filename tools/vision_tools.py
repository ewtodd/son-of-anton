#!/usr/bin/env python3
"""vision_analyze: describe an image with the auxiliary vision model.

The image (http(s) URL, local path, file:// URI or data: URL) is loaded,
normalized to a format vision APIs accept, size-capped, and sent to
``auxiliary.vision`` (default: the main custom endpoint) with the question.
The gateway and the CLI call :func:`vision_analyze_tool` directly to
pre-analyze attached images; compaction's image-shrink recovery reuses
:func:`_resize_image_for_vision`.
"""

import asyncio
import base64
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from son_of_anton_constants import get_son_of_anton_dir
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

# Image HTTP download timeout, and a hard cap on any image source (50 MB) so an
# attacker-hosted multi-gigabyte file or decompression bomb never reaches memory.
_VISION_DOWNLOAD_TIMEOUT = 30.0
_VISION_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024

# One turn can fan out many vision calls at once (every frame of a clip). Their
# CPU-bound encode/resize bursts run on this executor, bounded to the usable
# cores, so excess encodes queue instead of starving the event loop; the LLM
# calls stay outside it and fully concurrent. A thread pool rather than an
# asyncio primitive because each tool call runs on its own per-thread loop.
_vision_cpu_executor = ThreadPoolExecutor(
    max_workers=len(os.sched_getaffinity(0)),
    thread_name_prefix="vision-encode",
)


async def _run_encode_on_cpu_executor(fn, *args, **kwargs):
    """Run a sync encode/resize callable on the bounded vision CPU executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _vision_cpu_executor, functools.partial(fn, *args, **kwargs)
    )


def _detect_image_mime_type_from_bytes(data: bytes) -> Optional[str]:
    """Magic-byte MIME sniff on raw bytes (authoritative; no extension trust).

    Returns ``None`` for anything without a recognized image header — including
    SVG, which has no magic bytes and is sniffed separately.
    """
    header = data[:64]
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


# Explicit URL scheme, e.g. "ftp://", "s3://".
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


async def _resolve_image_source(src: str) -> tuple[bytes, str]:
    """Load an image source to ``(bytes, mime)``, raising ``ValueError`` when it can't.

    Every source passes the same ingest cap and type check. The cap is the
    50 MB download budget, not the 20 MB payload cap: an oversized image must
    still reach the resizer.
    """
    if not isinstance(src, str) or not src.strip():
        raise ValueError("image_url is required")
    s = src.strip()
    if s.startswith("data:"):
        header, _, payload = s.partition(",")
        if ";base64" not in header:
            raise ValueError("data: URL must be base64-encoded")
        if (len(payload) * 3) // 4 > _VISION_MAX_DOWNLOAD_BYTES:
            raise ValueError("data: URL exceeds size limit")
        try:
            data = base64.b64decode(payload, validate=True)
        except Exception as exc:
            raise ValueError(f"invalid base64 in data: URL: {exc}")
    elif s.startswith(("http://", "https://")):
        from tools.url_safety import is_safe_url

        # Unsafe URLs are refused before any network I/O; redirects are
        # re-checked inside _download_image.
        if not is_safe_url(s):
            raise ValueError("blocked: unsafe or private URL")
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as tf:
            tmp = Path(tf.name)
        try:
            await _download_image(s, tmp)
            data = await asyncio.to_thread(tmp.read_bytes)
        finally:
            tmp.unlink(missing_ok=True)
    elif _SCHEME_RE.match(s) and not s.lower().startswith("file://"):
        raise ValueError(
            "Unrecognized image source scheme. Use an http(s) URL, a local "
            "file path, a file:// URI, or a data: URL."
        )
    else:
        # Everything else is a filesystem path, including bare relative names.
        candidate = s[len("file://"):] if s.lower().startswith("file://") else s
        p = Path(os.path.expanduser(candidate))
        try:
            host_target = p.resolve()
        except Exception:  # noqa: BLE001 — unresolved path: let is_file() fail
            host_target = p
        if not host_target.is_file():
            raise ValueError(f"media file not found: '{p}'")
        # Refuse secret-bearing files (.env, auth.json, ...) explicitly rather
        # than relying on the magic-byte sniff to reject them.
        from agent.file_safety import raise_if_read_blocked

        raise_if_read_blocked(str(host_target))
        data = await asyncio.to_thread(host_target.read_bytes)

    if len(data) > _VISION_MAX_DOWNLOAD_BYTES:
        raise ValueError("media exceeds size limit")
    mime = _detect_image_mime_type_from_bytes(data)
    if mime is not None:
        return data, mime
    if b"<svg" in data[:4096].lower():
        # Rasterized to PNG by _normalize_to_supported_image before embedding.
        return data, "image/svg+xml"
    raise ValueError("source is not a recognized image")


# Media types vision APIs accept for inline base64 images. Anything else (SVG,
# BMP, TIFF, ...) is normalized to PNG first: an unsupported media type baked
# into immutable history would be rejected again on every later turn.
_SUPPORTED_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)


def _rasterize_svg_to_png(svg_path: Path, out_path: Path) -> bool:
    """Best-effort SVG → PNG with a system rasterizer (rsvg-convert, inkscape)."""
    for cmd in (
        ["rsvg-convert", "-o", str(out_path), str(svg_path)],
        ["inkscape", str(svg_path), "--export-type=png",
         f"--export-filename={out_path}"],
    ):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(
                    cmd, check=True, capture_output=True, timeout=30,
                    stdin=subprocess.DEVNULL,
                )
                if out_path.exists() and out_path.stat().st_size > 0:
                    return True
            except Exception:
                continue
    return False


def _normalize_to_supported_image(
    image_path: Path, detected_mime: str
) -> tuple[Optional[Path], Optional[str], Optional[str]]:
    """Ensure an image is in a vision-API-supported format.

    Returns ``(path, mime, error)``: the input unchanged when already
    supported, ``(new_png_path, "image/png", None)`` after conversion (a temp
    file the CALLER must clean up), or ``(None, None, message)`` when it can't
    be converted. SVG is rasterized; other formats Pillow reads are re-encoded.
    """
    if detected_mime in _SUPPORTED_MEDIA_TYPES:
        return image_path, detected_mime, None

    out_dir = get_son_of_anton_dir("cache/vision", "temp_vision_images")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"converted_{uuid.uuid4()}.png"

    if detected_mime == "image/svg+xml":
        if _rasterize_svg_to_png(image_path, out_path):
            return out_path, "image/png", None
        return (
            None,
            None,
            "This is an SVG, which vision models cannot read directly, and no "
            "SVG rasterizer is installed (tried rsvg-convert, inkscape). Convert "
            "the SVG to PNG first — e.g. open it in a browser "
            "and screenshot it — then re-run vision_analyze on the PNG.",
        )

    try:
        from PIL import Image as _PILImage
        with _PILImage.open(image_path) as _img:
            if _img.mode not in ("RGB", "RGBA", "L"):
                _img = _img.convert("RGBA")
            _img.save(out_path, format="PNG")
        if out_path.exists() and out_path.stat().st_size > 0:
            return out_path, "image/png", None
    except Exception as _exc:
        logger.warning("Failed to normalize %s image to PNG: %s",
                       detected_mime, _exc)
    return (
        None,
        None,
        f"Image format {detected_mime!r} is not supported by the vision API "
        f"and could not be converted to PNG (install Pillow for raster "
        f"conversion). Convert it to PNG or JPEG and try again.",
    )


def _is_retryable_download_error(error: Exception) -> bool:
    """True only for transient download failures: 429, 5xx, transport errors.

    Other 4xx can't change on retry; PermissionError (SSRF guard) and
    ValueError (too large, blocked redirect) are deterministic.
    """
    if isinstance(error, (PermissionError, ValueError)):
        return False
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if 400 <= status < 500 and status != 429:
            return False
    return True


async def _download_image(image_url: str, destination: Path) -> Path:
    """Stream an image URL to *destination* under the size cap, with SSRF guards.

    The body goes chunk-by-chunk through a temp file so the cap bounds memory
    (Content-Length is only an early rejection; servers can omit or lie about
    it). Transient failures retry with 2s/4s backoff, up to three attempts.
    """
    from tools.url_safety import (
        async_is_safe_url,
        create_ssrf_safe_async_client,
        redirect_target_from_response,
    )
    from utils import atomic_replace

    destination.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = _VISION_MAX_DOWNLOAD_BYTES

    async def _ssrf_redirect_guard(response):
        # Re-validate each redirect target: a public URL that 302-redirects to
        # http://169.254.169.254/ would otherwise bypass the pre-flight check.
        redirect_url = redirect_target_from_response(response)
        if redirect_url and not await async_is_safe_url(redirect_url):
            raise ValueError(
                f"Blocked redirect to private/internal address: {redirect_url}"
            )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            # The client also validates DNS at TCP connect time.
            async with create_ssrf_safe_async_client(
                timeout=_VISION_DOWNLOAD_TIMEOUT,
                follow_redirects=True,
                event_hooks={"response": [_ssrf_redirect_guard]},
            ) as client:
                async with client.stream("GET", image_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "image/*,*/*;q=0.8",
                }) as response:
                    response.raise_for_status()
                    cl = response.headers.get("content-length")
                    if cl:
                        try:
                            declared_size = int(cl)
                        except ValueError:
                            declared_size = None
                        if declared_size is not None and declared_size > max_bytes:
                            raise ValueError(
                                f"Image too large ({declared_size} bytes, max {max_bytes})"
                            )
                    tmp_destination = destination.with_name(
                        f".{destination.name}.{uuid.uuid4().hex}.tmp"
                    )
                    bytes_written = 0
                    try:
                        with tmp_destination.open("wb") as f:
                            async for chunk in response.aiter_bytes():
                                if not chunk:
                                    continue
                                bytes_written += len(chunk)
                                if bytes_written > max_bytes:
                                    raise ValueError(
                                        f"Image too large ({bytes_written} bytes, max {max_bytes})"
                                    )
                                f.write(chunk)
                        atomic_replace(tmp_destination, destination)
                    except Exception:
                        tmp_destination.unlink(missing_ok=True)
                        raise
            return destination
        except Exception as e:
            if not _is_retryable_download_error(e) or attempt >= max_retries - 1:
                logger.error(
                    "Image download failed after %s attempt(s): %s",
                    attempt + 1,
                    str(e)[:100],
                    exc_info=True,
                )
                raise
            wait_time = 2 ** (attempt + 1)
            logger.warning("Image download failed (attempt %s/%s): %s", attempt + 1, max_retries, str(e)[:50])
            logger.warning("Retrying in %ss...", wait_time)
            await asyncio.sleep(wait_time)


def _determine_mime_type(image_path: Path) -> str:
    """MIME type from the file extension (image/jpeg when unknown)."""
    extension = image_path.suffix.lower()
    mime_types = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.bmp': 'image/bmp',
        '.webp': 'image/webp',
        '.svg': 'image/svg+xml'
    }
    return mime_types.get(extension, 'image/jpeg')


def _image_to_base64_data_url(image_path: Path, mime_type: Optional[str] = None) -> str:
    """Encode an image file as a ``data:<mime>;base64,...`` URL."""
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    mime = mime_type or _determine_mime_type(image_path)
    return f"data:{mime};base64,{encoded}"


# Absolute hard ceiling for vision API payloads (20 MB) — above this, no
# provider accepts the image and we reject outright.
_MAX_BASE64_BYTES = 20 * 1024 * 1024

# Target size when auto-resizing on API failure (5 MB).  After a provider
# rejects an image, we downscale to this target and retry once.
_RESIZE_TARGET_BYTES = 5 * 1024 * 1024


def _is_image_size_error(error: Exception) -> bool:
    """Detect if an API error is related to image or payload size."""
    err_str = str(error).lower()
    return any(hint in err_str for hint in (
        "too large", "payload", "413", "content_too_large",
        "request_too_large", "image_url", "invalid_request",
        "exceeds", "size limit",
    ))


def _crop_image_region(
    image_path: Path,
    region: Any,
    offset_out: Optional[dict] = None,
) -> tuple[Optional[Path], Optional[str], Optional[str]]:
    """Crop ``image_path`` to ``region`` = [x1, y1, x2, y2] (original-image pixels).

    Applied BEFORE :func:`_resize_image_for_vision` so the cropped area gets
    the full downscale resolution budget — a "zoom" into a detail region.
    Coordinates are clamped to the image bounds; a region that clamps to zero
    area (or is inverted/malformed) is rejected with an error naming the
    actual image dimensions so the caller can retry with sensible values.

    Ported from: QwenLM/qwen-code zoom-image.ts (Apache-2.0).

    Returns:
        (cropped_temp_path, out_mime, None) on success — the caller owns
        cleanup of the temp file — or (None, None, error_message) on failure.
    """
    from PIL import Image

    if (
        not isinstance(region, (list, tuple))
        or len(region) != 4
        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in region)
    ):
        return None, None, (
            "Invalid region: expected [x1, y1, x2, y2] as four numbers "
            "(pixel coordinates in the original image)."
        )

    try:
        with Image.open(image_path) as img:
            width, height = img.size
            x1, y1, x2, y2 = (int(v) for v in region)
            # Clamp to image bounds.
            cx1 = max(0, min(x1, width))
            cy1 = max(0, min(y1, height))
            cx2 = max(0, min(x2, width))
            cy2 = max(0, min(y2, height))
            if cx2 <= cx1 or cy2 <= cy1:
                return None, None, (
                    f"Invalid region [{x1}, {y1}, {x2}, {y2}]: crops to zero "
                    f"area after clamping to the image bounds. The image is "
                    f"{width}x{height} px — pick x1<x2 and y1<y2 inside "
                    f"[0, 0, {width}, {height}]."
                )
            cropped = img.crop((cx1, cy1, cx2, cy2))
            if offset_out is not None:
                offset_out["x"] = cx1
                offset_out["y"] = cy1
                offset_out["width"] = cx2 - cx1
                offset_out["height"] = cy2 - cy1
            out_path = image_path.with_name(
                f"{image_path.stem}_region_{uuid.uuid4().hex[:8]}.png"
            )
            if cropped.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                cropped = cropped.convert("RGB")
            cropped.save(out_path, format="PNG")
            return out_path, "image/png", None
    except Exception as exc:
        return None, None, f"Failed to crop region: {exc}"


def _build_scale_note(
    scale_info: Optional[dict],
    crop_offset: Optional[dict],
) -> Optional[str]:
    """Build a coordinate-mapping disclosure note for the analysis result.

    ``scale_info`` (from :func:`_resize_image_for_vision`) carries the
    original and downscaled pixel dimensions when a downscale actually
    happened. ``crop_offset`` (from :func:`_crop_image_region`) carries the
    clamped crop origin when a region zoom was applied. Returns ``None`` when
    neither applies — no note, no noise.
    """
    parts = []
    if scale_info:
        ow, oh = scale_info["orig_width"], scale_info["orig_height"]
        nw, nh = scale_info["new_width"], scale_info["new_height"]
        fx = ow / nw if nw else 1.0
        fy = oh / nh if nh else 1.0
        if f"{fx:.2f}" == f"{fy:.2f}":
            factor_clause = (
                f"multiply any coordinates you report by {fx:.2f} "
                f"to map back to the original image."
            )
        else:
            factor_clause = (
                f"multiply any x coordinates you report by {fx:.2f} and "
                f"any y coordinates by {fy:.2f} to map back to the "
                f"original image."
            )
        parts.append(
            f"Image downscaled from {ow}x{oh} to {nw}x{nh} for vision; "
            f"{factor_clause}"
        )
    if crop_offset:
        parts.append(
            f"Analysis was performed on a cropped region of the original "
            f"image starting at offset ({crop_offset['x']}, "
            f"{crop_offset['y']}); coordinates are relative to that crop "
            f"origin — add the offset to map back to the full image."
        )
    if not parts:
        return None
    return " ".join(parts)


def _resize_image_for_vision(image_path: Path, mime_type: Optional[str] = None,
                              max_base64_bytes: int = _RESIZE_TARGET_BYTES,
                              max_dimension: Optional[int] = None,
                              scale_out: Optional[dict] = None) -> str:
    """Convert an image to a base64 data URL, auto-resizing if too large.

    Progressively downscales with Pillow; if the image can't be opened or
    still exceeds the limit, returns the best attempt and lets the caller
    handle the size check.

    Args:
        max_dimension: If set, images whose longest side exceeds this pixel
            count are forcibly downscaled even if they're under the byte
            budget (providers cap pixels per side independently of bytes).

    Returns the base64 data URL string.
    """
    # Quick file-size estimate: base64 expands by ~4/3, plus data URL header.
    # Skip the expensive full-read + encode if Pillow can resize directly.
    file_size = image_path.stat().st_size
    estimated_b64 = (file_size * 4) // 3 + 100  # ~header overhead
    needs_resize_for_bytes = estimated_b64 > max_base64_bytes

    # Check pixel dimensions even if bytes are fine.
    needs_resize_for_dims = False
    if max_dimension is not None:
        try:
            from PIL import Image as _PILQuick
            with _PILQuick.open(image_path) as _quick_img:
                if max(_quick_img.size) > max_dimension:
                    needs_resize_for_dims = True
        except Exception:
            pass  # can't check; Pillow path below will handle or skip

    if not needs_resize_for_bytes and not needs_resize_for_dims:
        # Small enough — just encode directly.
        data_url = _image_to_base64_data_url(image_path, mime_type=mime_type)
        if len(data_url) <= max_base64_bytes:
            return data_url
    else:
        data_url = None  # defer full encode; try Pillow resize first

    from PIL import Image
    import io as _io

    logger.info("Image file is %.1f MB (estimated base64 %.1f MB, limit %.1f MB, max_dimension=%s), auto-resizing...",
                file_size / (1024 * 1024), estimated_b64 / (1024 * 1024),
                max_base64_bytes / (1024 * 1024), max_dimension)

    mime = mime_type or _determine_mime_type(image_path)
    # Choose output format: JPEG for photos (smaller), PNG for transparency
    pil_format = "PNG" if mime == "image/png" else "JPEG"
    out_mime = "image/png" if pil_format == "PNG" else "image/jpeg"

    try:
        img = Image.open(image_path)
    except Exception as exc:
        logger.info("Pillow cannot open image for resizing: %s", exc)
        if data_url is None:
            data_url = _image_to_base64_data_url(image_path, mime_type=mime_type)
        return data_url  # fall through to size-check in caller
    # Convert RGBA to RGB for JPEG output
    if pil_format == "JPEG" and img.mode in {"RGBA", "P"}:
        img = img.convert("RGB")

    # Strategy: halve dimensions until both base64 fits AND pixel dimensions
    # are within limits, up to 4 rounds.
    # For JPEG, also try reducing quality at each size step.
    # For PNG, quality is irrelevant — only dimension reduction helps.
    quality_steps = (85, 70, 50) if pil_format == "JPEG" else (None,)
    orig_dims = (img.width, img.height)
    prev_dims = (img.width, img.height)
    candidate = None  # will be set on first loop iteration

    def _record_scale(w: int, h: int) -> None:
        """Publish the downscale into ``scale_out`` when dims changed."""
        if scale_out is not None and (w, h) != orig_dims:
            scale_out["orig_width"] = orig_dims[0]
            scale_out["orig_height"] = orig_dims[1]
            scale_out["new_width"] = w
            scale_out["new_height"] = h

    def _dims_ok(w: int, h: int) -> bool:
        """True if both pixel dimensions are within the limit."""
        if max_dimension is None:
            return True
        return max(w, h) <= max_dimension

    for attempt in range(5):
        if attempt > 0:
            # Proportional scaling: halve the longer side and scale the
            # shorter side to preserve aspect ratio (min dimension 64).
            scale = 0.5
            new_w = max(int(img.width * scale), 64)
            new_h = max(int(img.height * scale), 64)
            # Re-derive the scale from whichever dimension hit the floor
            # so both axes shrink by the same factor.
            if new_w == 64 and img.width > 0:
                effective_scale = 64 / img.width
                new_h = max(int(img.height * effective_scale), 64)
            elif new_h == 64 and img.height > 0:
                effective_scale = 64 / img.height
                new_w = max(int(img.width * effective_scale), 64)
            # Stop if dimensions can't shrink further
            if (new_w, new_h) == prev_dims:
                break
            img = img.resize((new_w, new_h), Image.LANCZOS)
            prev_dims = (new_w, new_h)
            logger.info("Resized to %dx%d (attempt %d)", new_w, new_h, attempt)

        for q in quality_steps:
            buf = _io.BytesIO()
            save_kwargs = {"format": pil_format}
            if q is not None:
                save_kwargs["quality"] = q
            img.save(buf, **save_kwargs)
            encoded = base64.b64encode(buf.getvalue()).decode("ascii")
            candidate = f"data:{out_mime};base64,{encoded}"
            if len(candidate) <= max_base64_bytes and _dims_ok(img.width, img.height):
                logger.info("Auto-resized image fits: %.1f MB (quality=%s, %dx%d)",
                            len(candidate) / (1024 * 1024), q,
                            img.width, img.height)
                _record_scale(img.width, img.height)
                return candidate

    # If we still can't get it small enough, return the best attempt
    # and let the caller decide
    if candidate is not None:
        logger.warning("Auto-resize could not fit image under %.1f MB (best: %.1f MB)",
                       max_base64_bytes / (1024 * 1024), len(candidate) / (1024 * 1024))
        _record_scale(img.width, img.height)
        return candidate

    # Shouldn't reach here, but fall back to full encode
    return data_url or _image_to_base64_data_url(image_path, mime_type=mime_type)


async def vision_analyze_tool(
    image_url: str,
    user_prompt: str,
    model: str = None,
    region: Optional[list] = None,
) -> str:
    """Analyze an image (URL, local path, file:// URI or data: URL) with the vision model.

    The image is materialized to a temp file under
    $SON_OF_ANTON_HOME/cache/vision/ (removed afterwards; source files are
    never touched), normalized, optionally cropped to ``region``, and sent at
    full resolution — resized only past the 20 MB ceiling or when the
    provider rejects it as too large.

    Returns a JSON string ``{"success": bool, "analysis": str, ...}``.
    """
    if not isinstance(user_prompt, str):
        user_prompt = str(user_prompt) if user_prompt is not None else ""

    temp_image_path = None

    try:
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return tool_error("Interrupted", success=False)

        logger.info("Analyzing image: %s", image_url[:60])

        data, detected_mime_type = await _resolve_image_source(image_url)
        temp_dir = get_son_of_anton_dir("cache/vision", "temp_vision_images")
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_image_path = temp_dir / f"temp_image_{uuid.uuid4()}.img"
        await asyncio.to_thread(temp_image_path.write_bytes, data)

        # Normalize unsupported formats (SVG, BMP, ...) to PNG. Offloaded — the
        # rasterizers/Pillow are blocking.
        normalized_path, detected_mime_type, _norm_err = await asyncio.to_thread(
            _normalize_to_supported_image, temp_image_path, detected_mime_type,
        )
        if _norm_err or normalized_path is None:
            raise ValueError(_norm_err or "Image normalization failed.")
        if normalized_path != temp_image_path:
            temp_image_path.unlink(missing_ok=True)
            temp_image_path = normalized_path

        # Optional region zoom: crop BEFORE the encode/downscale pipeline so
        # the cropped area gets the full resolution budget.
        _crop_offset: dict = {}
        _scale_info: dict = {}
        if region is not None:
            cropped_path, cropped_mime, crop_err = await asyncio.to_thread(
                _crop_image_region, temp_image_path, region,
                offset_out=_crop_offset,
            )
            if crop_err or cropped_path is None:
                raise ValueError(crop_err or "Region crop failed.")
            temp_image_path.unlink(missing_ok=True)
            temp_image_path = cropped_path
            detected_mime_type = cropped_mime

        image_data_url = await _run_encode_on_cpu_executor(
            _image_to_base64_data_url, temp_image_path, mime_type=detected_mime_type)

        # Hard limit (20 MB) — no provider accepts payloads this large.
        if len(image_data_url) > _MAX_BASE64_BYTES:
            # Try to resize down to 5 MB before giving up.
            image_data_url = await _run_encode_on_cpu_executor(
                _resize_image_for_vision,
                temp_image_path, mime_type=detected_mime_type,
                scale_out=_scale_info)
            if len(image_data_url) > _MAX_BASE64_BYTES:
                raise ValueError(
                    f"Image too large for vision API: base64 payload is "
                    f"{len(image_data_url) / (1024 * 1024):.1f} MB "
                    f"(limit {_MAX_BASE64_BYTES / (1024 * 1024):.0f} MB) "
                    f"even after resizing. Compress or crop the image manually."
                )

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }
        ]

        # The timeout comes from auxiliary.vision.timeout (default 120s:
        # local vision models can take well over 30s).
        call_kwargs = {"task": "vision", "messages": messages, "temperature": 0.1}
        if model:
            call_kwargs["model"] = model
        from agent.auxiliary_client import async_call_llm, extract_content_or_reasoning

        # Try full-size image first; on size-related rejection, downscale and retry.
        try:
            response = await async_call_llm(**call_kwargs)
        except Exception as _api_err:
            if (_is_image_size_error(_api_err)
                    and len(image_data_url) > _RESIZE_TARGET_BYTES):
                logger.info(
                    "API rejected image (%.1f MB, likely too large); "
                    "auto-resizing to ~%.0f MB and retrying...",
                    len(image_data_url) / (1024 * 1024),
                    _RESIZE_TARGET_BYTES / (1024 * 1024),
                )
                image_data_url = await _run_encode_on_cpu_executor(
                    _resize_image_for_vision,
                    temp_image_path, mime_type=detected_mime_type,
                    scale_out=_scale_info)
                messages[0]["content"][1]["image_url"]["url"] = image_data_url
                response = await async_call_llm(**call_kwargs)
            else:
                raise

        # Extract the analysis — fall back to reasoning if content is empty
        analysis = extract_content_or_reasoning(response)

        # Retry once on empty content (reasoning-only response)
        if not analysis:
            logger.warning("Vision LLM returned empty content, retrying once")
            response = await async_call_llm(**call_kwargs)
            analysis = extract_content_or_reasoning(response)

        logger.info("Image analysis completed (%s characters)", len(analysis))

        analysis = analysis or "There was a problem with the request and the image could not be analyzed."
        scale_note = _build_scale_note(
            _scale_info or None, _crop_offset or None,
        )
        result = {
            "success": True,
            "analysis": f"[{scale_note}] {analysis}" if scale_note else analysis,
        }
        if scale_note:
            result["scale_note"] = scale_note
        return json.dumps(result, indent=2, ensure_ascii=False)

    except Exception as e:
        error_msg = f"Error analyzing image: {str(e)}"
        logger.error("%s", error_msg, exc_info=True)

        # Classify vision capability errors so the model can tell the user
        # something clearer than a raw API error.
        err_str = str(e).lower()
        if any(hint in err_str for hint in (
            "402", "insufficient", "payment required", "credits", "billing",
        )):
            analysis = (
                "Insufficient credits or payment required. Please top up your "
                f"API provider account and try again. Error: {e}"
            )
        elif any(hint in err_str for hint in (
            "does not support", "not support image",
            "content_policy", "multimodal",
            "unrecognized request argument", "image input",
        )):
            analysis = (
                f"{model} does not support vision or our request was not "
                f"accepted by the server. Error: {e}"
            )
        elif "invalid_request" in err_str or "image_url" in err_str:
            analysis = (
                "The vision API rejected the image. This can happen when the "
                "image is in an unsupported format, corrupted, or still too "
                "large after auto-resize. Try a smaller JPEG/PNG and retry. "
                f"Error: {e}"
            )
        else:
            analysis = (
                "There was a problem with the request and the image could not "
                f"be analyzed. Error: {e}"
            )

        result = {
            "success": False,
            "error": error_msg,
            "analysis": analysis,
        }
        return json.dumps(result, indent=2, ensure_ascii=False)

    finally:
        # Only the temp copies are deleted, never the caller's source file.
        if temp_image_path and temp_image_path.exists():
            try:
                temp_image_path.unlink()
            except Exception as cleanup_error:
                logger.warning(
                    "Could not delete temporary file: %s", cleanup_error, exc_info=True
                )


def check_vision_requirements() -> bool:
    """True when the vision route (``auxiliary.vision``, else the main model) resolves a client."""
    from agent.auxiliary_client import aux_probe_mode, resolve_vision_provider_client

    try:
        # Probe mode resolves availability without constructing SDK clients.
        with aux_probe_mode():
            return resolve_vision_provider_client()[1] is not None
    except Exception:
        return False


VISION_ANALYZE_SCHEMA = {
    "name": "vision_analyze",
    "description": (
        "Load an image into the conversation so you can see it. Accepts a "
        "URL, local file path, or data URL. When your active model has "
        "native vision, the image is attached to your context directly "
        "and you read the pixels yourself on the next turn — call this "
        "any time the user references an image (filepath in their message, "
        "URL in tool output, screenshot from the browser, etc.). For "
        "non-vision models, falls back to an auxiliary vision model that "
        "returns a text description."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "image_url": {
                "type": "string",
                "description": "Image URL (http/https), local file path, or data: URL to load."
            },
            "question": {
                "type": "string",
                "description": "Your specific question or request about the image. Optional context the model uses on the next turn after seeing the image."
            },
            "region": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 4,
                "maxItems": 4,
                "description": (
                    "Optional [x1, y1, x2, y2] crop region in pixel coordinates "
                    "of the ORIGINAL image, applied before any downscaling so "
                    "the region keeps full resolution. Intended flow: load the "
                    "full image first, then call again with a region to zoom "
                    "into a detail (small text, UI element, fine print). "
                    "Coordinates are clamped to the image bounds."
                )
            }
        },
        "required": ["image_url", "question"]
    }
}


async def _handle_vision_analyze(args: Dict[str, Any], **kw: Any) -> str:
    full_prompt = (
        "Fully describe and explain everything about this image, then answer the "
        f"following question:\n\n{args.get('question', '')}"
    )
    return await vision_analyze_tool(args.get("image_url", ""), full_prompt, region=args.get("region"))


registry.register(
    name="vision_analyze",
    toolset="vision",
    schema=VISION_ANALYZE_SCHEMA,
    handler=_handle_vision_analyze,
    check_fn=check_vision_requirements,
    is_async=True,
    emoji="👁️",
)
