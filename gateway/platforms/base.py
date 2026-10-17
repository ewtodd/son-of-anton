"""
Base platform adapter interface.

The Signal adapter inherits from this and implements the required methods.
"""

import asyncio
import inspect
import logging
import os
import random
import re
import threading
import time
import uuid
from abc import ABC, abstractmethod
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


# Audio file extensions Son of Anton recognizes for native audio delivery.
_AUDIO_EXTS = frozenset({".ogg", ".opus", ".mp3", ".m2a", ".wav", ".m4a", ".flac"})
_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS = 30.0
# Delivery-time history is best-effort dedup metadata, not canonical state:
# fail open rather than withholding a legitimate attachment.
_HISTORY_MEDIA_LOOKUP_TIMEOUT_SECONDS = 5.0
# Timed-out reads cannot be cancelled while SQLite/Python code is already
# running. Isolate and cap them so wedged best-effort dedup work cannot consume
# the shared asyncio executor or create an unbounded number of worker threads.
_HISTORY_MEDIA_LOOKUP_MAX_WORKERS = 2
_HISTORY_MEDIA_LOOKUP_ADMISSION = threading.BoundedSemaphore(
    _HISTORY_MEDIA_LOOKUP_MAX_WORKERS
)


def _thread_metadata_for_source(source, reply_to_message_id: str | None = None) -> dict | None:
    """Build platform-aware thread metadata for adapter sends.

    Most platforms route threaded sends with a generic ``thread_id`` metadata
    value.
    """
    thread_id = getattr(source, "thread_id", None)
    return {"thread_id": thread_id} if thread_id is not None else None


def _mark_notify_metadata(metadata: dict | None) -> dict:
    """Clone metadata and mark a user-visible reply as notify-worthy."""
    notify_metadata = dict(metadata) if metadata else {}
    notify_metadata["notify"] = True
    return notify_metadata


def _reply_anchor_for_event(event) -> str | None:
    """Return reply_to id for platforms that need reply semantics."""
    return getattr(event, "message_id", None)


def should_send_media_as_audio(platform, ext: str, is_voice: bool = False) -> bool:
    """Return True when a media file should use the platform's audio sender.

    Every recognized audio extension routes through the audio sender.
    """
    normalized_ext = (ext or "").lower()
    if normalized_ext not in _AUDIO_EXTS:
        return False
    return True


def _custom_unit_to_cp(s: str, budget: int, len_fn) -> int:
    """Return the largest codepoint offset *n* such that ``len_fn(s[:n]) <= budget``.

    Used by :meth:`BasePlatformAdapter.truncate_message` when *len_fn* measures
    length in units different from Python codepoints (e.g. UTF-16 code units).
    Falls back to binary search which is O(log n) calls to *len_fn*.
    """
    if len_fn(s) <= budget:
        return len(s)
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len_fn(s[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable, Awaitable, Tuple, Union
from enum import Enum

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource, build_session_key
from son_of_anton_constants import get_son_of_anton_dir, get_son_of_anton_home


def safe_url_for_log(url: str, max_len: int = 80) -> str:
    """Return a URL string safe for logs (no query/fragment/userinfo)."""
    if max_len <= 0:
        return ""

    if url is None:
        return ""

    raw = str(url)
    if not raw:
        return ""

    try:
        parsed = urlsplit(raw)
    except Exception:
        return raw[:max_len]

    if parsed.scheme and parsed.netloc:
        # Strip potential embedded credentials (user:pass@host).
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        base = f"{parsed.scheme}://{netloc}"
        path = parsed.path or ""
        if path and path != "/":
            basename = path.rsplit("/", 1)[-1]
            safe = f"{base}/.../{basename}" if basename else f"{base}/..."
        else:
            safe = base
    else:
        safe = raw

    if len(safe) <= max_len:
        return safe
    if max_len <= 3:
        return "." * max_len
    return f"{safe[:max_len - 3]}..."


async def _ssrf_redirect_guard(response):
    """Re-validate each redirect target to prevent redirect-based SSRF.

    Without this, an attacker can host a public URL that 302-redirects to
    http://169.254.169.254/ and bypass the pre-flight is_safe_url() check.

    Must be async because httpx.AsyncClient awaits response event hooks.
    """
    from tools.url_safety import is_safe_url, redirect_target_from_response
    redirect_url = redirect_target_from_response(response)
    if redirect_url and not is_safe_url(redirect_url):
        raise ValueError(
            f"Blocked redirect to private/internal address: {safe_url_for_log(redirect_url)}"
        )


# ---------------------------------------------------------------------------
# Image cache utilities
#
# When users send images on messaging platforms, we download them to a local
# cache directory so they can be analyzed by the vision tool (which accepts
# local file paths). This avoids issues with ephemeral platform URLs
# (e.g. platform file URLs expire after ~1 hour).
# ---------------------------------------------------------------------------

IMAGE_CACHE_DIR = get_son_of_anton_dir("cache/images", "image_cache")

# Inbound image / audio payloads are buffered fully into memory before they
# are cached, so a hostile upload or remote URL is capped at 128 MiB.
INBOUND_MEDIA_MAX_BYTES = 128 * 1024 * 1024


def validate_inbound_media_size(size: int, *, media_type: str = "media") -> None:
    """Raise ``ValueError`` if an inbound media payload exceeds the cap."""
    if size > INBOUND_MEDIA_MAX_BYTES:
        raise ValueError(
            f"Inbound {media_type} payload is too large "
            f"({size} bytes > {INBOUND_MEDIA_MAX_BYTES} bytes)"
        )


async def _read_httpx_body_with_limit(response, *, media_type: str) -> bytes:
    """Read an httpx streaming response body without exceeding the media cap.

    Rejects early on an oversized ``Content-Length`` header, then re-checks
    the running total as chunks arrive so a lying/absent header can't smuggle
    an unbounded body past the cap.
    """
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            logger.debug(
                "Ignoring invalid Content-Length for inbound %s: %r",
                media_type, content_length,
            )
        else:
            validate_inbound_media_size(declared_size, media_type=media_type)

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        validate_inbound_media_size(total, media_type=media_type)
        chunks.append(chunk)
    return b"".join(chunks)


def get_image_cache_dir() -> Path:
    """Return the image cache directory, creating it if it doesn't exist."""
    d = get_son_of_anton_dir("cache/images", "image_cache")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _looks_like_image(data: bytes) -> bool:
    """Return True if *data* starts with a known image magic-byte sequence."""
    if len(data) < 4:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return True
    if data[:2] == b"BM":
        return True
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return True
    return False


def cache_image_from_bytes(data: bytes, ext: str = ".jpg") -> str:
    """
    Save raw image bytes to the cache and return the absolute file path.

    Args:
        data: Raw image bytes.
        ext:  File extension including the dot (e.g. ".jpg", ".png").

    Returns:
        Absolute path to the cached image file as a string.

    Raises:
        ValueError: If *data* does not look like a valid image (e.g. an HTML
            error page returned by the upstream server).
    """
    validate_inbound_media_size(len(data), media_type="image")
    if not _looks_like_image(data):
        snippet = data[:80].decode("utf-8", errors="replace")
        raise ValueError(
            f"Refusing to cache non-image data as {ext} "
            f"(starts with: {snippet!r})"
        )
    cache_dir = get_image_cache_dir()
    filename = f"img_{uuid.uuid4().hex[:12]}{ext}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


async def cache_image_from_url(url: str, ext: str = ".jpg", retries: int = 2) -> str:
    """
    Download an image from a URL and save it to the local cache.

    Retries on transient failures (timeouts, 429, 5xx) with exponential
    backoff so a single slow CDN response doesn't lose the media.

    Args:
        url: The HTTP/HTTPS URL to download from.
        ext: File extension including the dot (e.g. ".jpg", ".png").
        retries: Number of retry attempts on transient failures.

    Returns:
        Absolute path to the cached image file as a string.

    Raises:
        ValueError: If the URL targets a private/internal network (SSRF protection).
    """
    from tools.url_safety import create_ssrf_safe_async_client, is_safe_url
    if not is_safe_url(url):
        raise ValueError(f"Blocked unsafe URL (SSRF protection): {safe_url_for_log(url)}")

    import httpx
    _log = logging.getLogger(__name__)

    async with create_ssrf_safe_async_client(
        timeout=30.0,
        follow_redirects=True,
        event_hooks={"response": [_ssrf_redirect_guard]},
    ) as client:
        for attempt in range(retries + 1):
            try:
                async with client.stream(
                    "GET",
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; SonOfAntonAgent/1.0)",
                        "Accept": "image/*,*/*;q=0.8",
                    },
                ) as response:
                    response.raise_for_status()
                    content = await _read_httpx_body_with_limit(
                        response, media_type="image",
                    )
                return cache_image_from_bytes(content, ext)
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 429:
                    raise
                if attempt < retries:
                    wait = 1.5 * (attempt + 1)
                    _log.debug(
                        "Media cache retry %d/%d for %s (%.1fs): %s",
                        attempt + 1,
                        retries,
                        safe_url_for_log(url),
                        wait,
                        exc,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise


def _cleanup_cache_dir(cache_dir: Path, max_age_hours: int) -> int:
    """
    Delete files in *cache_dir* older than *max_age_hours*.

    Shared implementation behind every ``cleanup_*_cache`` helper — one loop,
    not N copies.  Returns the number of files removed.
    """
    import time

    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for f in cache_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def cleanup_image_cache(max_age_hours: int = 24) -> int:
    """
    Delete cached images older than *max_age_hours*.

    Returns the number of files removed.
    """
    return _cleanup_cache_dir(get_image_cache_dir(), max_age_hours)


# ---------------------------------------------------------------------------
# Audio cache utilities
#
# Same pattern as image cache -- voice messages from platforms are downloaded
# here so the agent can reference them by local file path.
# ---------------------------------------------------------------------------

AUDIO_CACHE_DIR = get_son_of_anton_dir("cache/audio", "audio_cache")


def get_audio_cache_dir() -> Path:
    """Return the audio cache directory, creating it if it doesn't exist."""
    d = get_son_of_anton_dir("cache/audio", "audio_cache")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sniff_audio_ext(data: bytes, fallback_ext: str) -> str:
    """Prefer a container-matching extension when audio magic bytes are obvious.

    Thin wrapper around the shared sniffer in ``tools.audio_container``.
    """
    from tools.audio_container import sniff_audio_ext

    return sniff_audio_ext(data, fallback_ext)


def cache_audio_from_bytes(data: bytes, ext: str = ".ogg") -> str:
    """
    Save raw audio bytes to the cache and return the absolute file path.

    Args:
        data: Raw audio bytes.
        ext:  File extension including the dot (e.g. ".ogg", ".mp3").

    Returns:
        Absolute path to the cached audio file as a string.
    """
    validate_inbound_media_size(len(data), media_type="audio")
    cache_dir = get_audio_cache_dir()
    sniffed_ext = _sniff_audio_ext(data, ext)
    filename = f"audio_{uuid.uuid4().hex[:12]}{sniffed_ext}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


def cleanup_audio_cache(max_age_hours: int = 24) -> int:
    """
    Delete cached audio files older than *max_age_hours*.

    Returns the number of files removed.
    """
    return _cleanup_cache_dir(get_audio_cache_dir(), max_age_hours)


# ---------------------------------------------------------------------------
# Document cache utilities
#
# Same pattern as image/audio cache -- documents from platforms are downloaded
# here so the agent can reference them by local file path.
# ---------------------------------------------------------------------------

DOCUMENT_CACHE_DIR = get_son_of_anton_dir("cache/documents", "document_cache")

_SON_OF_ANTON_HOME = get_son_of_anton_home()
# Media the gateway cached is always deliverable, in either cache layout.
MEDIA_DELIVERY_SAFE_ROOTS = (
    IMAGE_CACHE_DIR,
    AUDIO_CACHE_DIR,
    DOCUMENT_CACHE_DIR,
    _SON_OF_ANTON_HOME / "image_cache",
    _SON_OF_ANTON_HOME / "audio_cache",
    _SON_OF_ANTON_HOME / "document_cache",
    _SON_OF_ANTON_HOME / "cache" / "images",
    _SON_OF_ANTON_HOME / "cache" / "audio",
    _SON_OF_ANTON_HOME / "cache" / "documents",
)

# Prefixes that hold credentials, system state, or process introspection and
# must never be uploaded as a gateway attachment. The cache roots above still
# win, so cached media under /var/lib (a service's state dir) is deliverable.
_MEDIA_DELIVERY_DENIED_PREFIXES = (
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/root",
    "/boot",
    "/var/log",
    "/var/lib",
    "/var/run",
)

# Within $HOME we additionally deny common credential / config directories.
_MEDIA_DELIVERY_DENIED_HOME_SUBPATHS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".config",
    ".azure",
    ".gcloud",
)

# Credential stores at the SON_OF_ANTON_HOME root. Enumerated per file rather
# than denying the whole tree, so logs/ and agent-written files under it stay
# deliverable.
_HOME_CREDENTIAL_PATHS = (
    ".env",
    "auth.json",
    "auth.lock",
    "credentials",
    "config.toml",
    "pairing",
)


def _media_delivery_denied_paths() -> List[Path]:
    """Return absolute denylist paths under which delivery is never allowed."""
    denied = [Path(p) for p in _MEDIA_DELIVERY_DENIED_PREFIXES]
    home = Path(os.path.expanduser("~"))
    for sub in _MEDIA_DELIVERY_DENIED_HOME_SUBPATHS:
        denied.append(home / sub)
    for rel in _HOME_CREDENTIAL_PATHS:
        denied.append(_SON_OF_ANTON_HOME / rel)
    return denied


def _path_under_denied_prefix(resolved: Path) -> bool:
    """Return True if ``resolved`` lives under a deny-listed system path.

    One narrow exception: when a denied prefix IS the running user's own home,
    the home itself is not treated as denied. ``/root`` is on the system-path
    denylist so that a non-root gateway can't deliver another user's home, but
    on a root-run gateway ``$HOME=/root`` and the operator's own deliverables
    live directly under it. The credential sub-directories inside home
    (``~/.ssh``, ``~/.aws``, ...) and Son of Anton secrets are *separate,
    more-specific* denied paths, so they stay blocked regardless.
    """
    try:
        home = Path(os.path.expanduser("~")).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        home = None
    for denied in _media_delivery_denied_paths():
        try:
            resolved_denied = denied.expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if not (_path_is_within(resolved, resolved_denied) or resolved == resolved_denied):
            continue
        # Allow the running user's own home tree; its credential sub-dirs are
        # caught by their own (more-specific) denylist entries above.
        if home is not None and resolved_denied == home:
            continue
        return True
    return False


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_media_delivery_path(path: str) -> Optional[str]:
    """Return a safe absolute file path for native media delivery, else None.

    Accepts an existing regular file under a Son of Anton media cache, or any
    existing regular file that isn't under the credential / system-path
    denylist. Symlinks are resolved before any containment / denylist check.
    """
    if not path:
        return None

    candidate = str(path).strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "`\"'":
        candidate = candidate[1:-1].strip()
    candidate = candidate.lstrip("`\"'").rstrip("`\"',.;:)}]")
    if not candidate:
        return None

    try:
        expanded = Path(os.path.expanduser(candidate))
    except (OSError, RuntimeError, ValueError):
        # expanduser raises ValueError("embedded null byte") for a ~\x00 path.
        return None
    if not expanded.is_absolute():
        return None

    try:
        resolved = expanded.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None

    if not resolved.is_file():
        return None

    for root in MEDIA_DELIVERY_SAFE_ROOTS:
        try:
            resolved_root = Path(root).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if _path_is_within(resolved, resolved_root):
            return str(resolved)

    if _path_under_denied_prefix(resolved):
        return None
    return str(resolved)


# Neutralise control chars and the Unicode line separators (NEL, LS, PS) that
# str.splitlines() / log aggregators treat as breaks, so a model-emitted path
# can't forge a second log line. Truncated to keep records bounded.
_LOG_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")


def _log_safe_path(path: str) -> str:
    """Return a single-line, length-bounded path for log output."""
    return _LOG_UNSAFE_CHARS.sub("?", str(path))[:200]


# ---------------------------------------------------------------------------
# Media-delivery extension allowlist — the single source of truth for both
# extractors that turn response text into native attachments
# (``extract_media()`` for ``MEDIA:<path>`` tags, ``extract_local_files()`` for
# bare paths). Building the cleanup regexes from the same set means a tag is
# only stripped when its extension is one we can deliver, so an
# unknown-extension path survives in the body instead of vanishing.
# ---------------------------------------------------------------------------

MEDIA_DELIVERY_EXTS: Tuple[str, ...] = (
    # Images (embed inline)
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg",
    # Video
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp",
    # Audio
    ".mp3", ".m2a", ".wav", ".ogg", ".opus", ".m4a", ".flac",
    # Documents (uploaded as file attachments)
    ".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md", ".epub",
    # Spreadsheets / data
    ".xlsx", ".xls", ".ods", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
    # Geospatial / GIS
    ".kmz", ".kml", ".geojson", ".gpx",
    # Presentations
    ".pptx", ".ppt", ".odp", ".key",
    # Archives
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".apk", ".ipa",
    # Web / rendered output
    ".html", ".htm",
)

# Regex alternation fragment of bare extensions (no leading dot). Sorted
# longest-first so the alternation never matches a shorter ext as a prefix of
# a longer one.
_MEDIA_EXT_ALTERNATION = "|".join(
    sorted((e.lstrip(".") for e in MEDIA_DELIVERY_EXTS), key=len, reverse=True)
)

# Anchored ``MEDIA:<path>`` cleanup pattern: only strips a tag whose path ends
# in a known deliverable extension (optionally quoted/backticked).
# - A short run of emphasis/quote markers is allowed on both sides, because
#   models wrap tags in Markdown (``**MEDIA:/x.pdf**``). Code-block / inline-code
#   / blockquote contexts are neutralised earlier by ``_mask_protected_spans``,
#   so example tags remain non-deliverable.
# - Non-greedy path forms plus ``MEDIA:`` as a lookahead boundary keep two glued
#   tags (``MEDIA:/a.pngMEDIA:/b.png``) from merging into one invalid path.
# - A ``.`` ends the path only before whitespace / EOL, so a sentence-final
#   ``data.csv.`` still extracts while ``archive.tar.gz`` stays whole.
# - CJK full-width punctuation also terminates a path (``...pdf（782.6 KB）``).
_MEDIA_CJK_TERMINATORS = "（）〈〉《》：，。；！？、\u201c\u201d\u2018\u2019【】"

MEDIA_TAG_CLEANUP_RE = re.compile(
    r'''[`"'*_]{0,3}MEDIA:\s*'''
    r'''(?P<path>`[^`\n]+?`|"[^"\n]+?"|'[^'\n]+?'|'''
    r'''(?:~/|/)\S+?(?:[^\S\n]+\S+?)*?\.(?:''' + _MEDIA_EXT_ALTERNATION + r'''))'''
    r'''(?=[\s`"'*_,;:)\]}\[''' + _MEDIA_CJK_TERMINATORS + r''']|MEDIA:|\.(?:\s|$)|$)[`"'*_]{0,3}\.?''',
    re.IGNORECASE,
)

# Paths NOT covered by MEDIA_TAG_CLEANUP_RE's extension alternation — files
# with no extension (Caddyfile, Makefile) or an unknown one (.py, .log) — are
# delivered only after ``validate_media_delivery_path`` accepts them, so
# prompt-injection paths that do not validate stay visible instead of being
# silently dropped. The bare form is non-greedy and whitespace-bounded (so it
# cannot absorb the next tag or trailing prose); unknown-extension paths with
# spaces are recovered by ``_match_extensionless_path``, which extends the
# candidate across single spaces until one validates on disk.
MEDIA_EXTENSIONLESS_TAG_RE = re.compile(
    r'''[`"'*_]{0,3}MEDIA:\s*'''
    r'''(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|'''
    r'''(?:~/|/)[^\s\n`"']+?)'''
    r'''(?=[`"'\s,;:)\]}''' + _MEDIA_CJK_TERMINATORS + r''']|MEDIA:|$)'''
    r'''[`"'*_]{0,3}\s*''',
    re.IGNORECASE,
)


def _match_extensionless_path(scan_text: str, match: "re.Match") -> Optional[Tuple[str, int]]:
    """Resolve an extensionless MEDIA tag match to a validated on-disk path.

    Tries the regex-captured path first. When that fails validation, the
    candidate is progressively extended forward across single spaces
    (validation-gated, bounded at 8 tokens, never past a newline or a
    subsequent ``MEDIA:`` keyword) so unknown-extension paths containing
    spaces deliver (#24032). Returns ``(safe_path, end_offset)`` where
    ``end_offset`` is the index in ``scan_text`` just past the matched path,
    or ``None`` when nothing validates.
    """
    raw = match.group("path")
    path = _normalize_media_tag_path(raw)
    if not path:
        return None
    safe = validate_media_delivery_path(path)
    if safe:
        return safe, match.end("path")
    start = match.start("path")
    nl = scan_text.find("\n", start)
    limit = nl if nl != -1 else len(scan_text)
    segment = scan_text[start:limit]
    nxt = segment.find("MEDIA:", 1)
    if nxt != -1:
        segment = segment[:nxt]
    pos = match.end("path") - start
    for _ in range(8):
        while pos < len(segment) and segment[pos] in " \t":
            pos += 1
        if pos >= len(segment):
            break
        tok_end = pos
        while tok_end < len(segment) and segment[tok_end] not in " \t":
            tok_end += 1
        candidate = _normalize_media_tag_path(segment[:tok_end])
        safe = validate_media_delivery_path(candidate)
        if safe:
            return safe, start + tok_end
        pos = tok_end
    return None


def _merge_spans(spans: list) -> list:
    """Merge overlapping/nested (start, end) spans so multi-pattern matches
    over the same tag never double-delete adjacent text."""
    merged: list = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _normalize_media_tag_path(raw: str) -> str:
    path = str(raw or "").strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in "`\"'":
        path = path[1:-1].strip()
    return path.lstrip("`\"'").rstrip("`\"',.;:)}]")


def _path_lacks_deliverable_extension(path: str) -> bool:
    """True when MEDIA_TAG_CLEANUP_RE's extension alternation does not cover
    ``path`` — either the basename has no extension at all (Caddyfile,
    Makefile, …) or the extension is not in MEDIA_DELIVERY_EXTS (.py, .log,
    .weirdext, …). Such paths route through the validated delivery pass
    (``validate_media_delivery_path``) instead of the unconditional one, so
    every file type is deliverable (#36060) while nonexistent / denylisted
    paths stay visible in the text.
    """
    suffix = Path(path).suffix.lower()
    return not suffix or suffix not in MEDIA_DELIVERY_EXTS


def _strip_media_tag_directives(text: str) -> str:
    """Remove MEDIA: tags and [[audio_as_voice]] / [[as_document]] markers.

    Protected spans (fenced code blocks, inline code holding non-deliverable
    example tags, blockquotes, JSON string values) are used as a mask-locator
    only — tags inside them are neither stripped nor mangled, matching
    ``extract_media``'s treatment so display text and delivery agree (#16434).
    """
    if (
        "MEDIA:" not in text
        and "[[audio_as_voice]]" not in text
        and "[[as_document]]" not in text
    ):
        return text
    cleaned = text.replace("[[audio_as_voice]]", "").replace("[[as_document]]", "")

    # Locate real tag spans on a masked copy (offset-preserving), then delete
    # exactly those spans from the unmasked text — same pattern as
    # extract_media. Import-cycle-free: BasePlatformAdapter is defined later
    # in this module, so resolve it lazily at call time.
    masked = BasePlatformAdapter._mask_protected_spans(cleaned)
    masked = BasePlatformAdapter._mask_json_string_media(masked)

    spans: list = [m.span() for m in MEDIA_TAG_CLEANUP_RE.finditer(masked)]
    for match in MEDIA_EXTENSIONLESS_TAG_RE.finditer(masked):
        path = _normalize_media_tag_path(match.group("path"))
        if not path or not _path_lacks_deliverable_extension(path):
            continue
        resolved = _match_extensionless_path(masked, match)
        if resolved is not None:
            spans.append((match.start(), resolved[1]))

    if spans:
        chars = list(cleaned)
        for start, end in reversed(_merge_spans(spans)):
            del chars[start:end]
        cleaned = "".join(chars)
    return cleaned


def _platform_name(platform) -> str:
    """Normalize a Platform enum / raw string into a lowercase name."""
    value = getattr(platform, "value", platform)
    return str(value or "").lower()


# Import-time defaults; _resolve_cache_dir compares against these to tell a
# test monkeypatch from an unmodified constant.
_CACHE_DIR_IMPORT_DEFAULTS = {
    "IMAGE_CACHE_DIR": IMAGE_CACHE_DIR,
    "AUDIO_CACHE_DIR": AUDIO_CACHE_DIR,
    "DOCUMENT_CACHE_DIR": DOCUMENT_CACHE_DIR,
}


def _resolve_cache_dir(constant_name: str, new_subpath: str, old_name: str) -> Path:
    """Resolve fresh via get_son_of_anton_dir (active profile), unless a test has
    monkeypatched the constant away from its import-time default."""
    fresh = get_son_of_anton_dir(new_subpath, old_name)
    current = globals().get(constant_name)
    default = _CACHE_DIR_IMPORT_DEFAULTS.get(constant_name)
    if current is not None and default is not None and current != default:
        return Path(current)
    return fresh


def get_document_cache_dir() -> Path:
    """Return the document cache directory, creating it if it doesn't exist."""
    d = _resolve_cache_dir("DOCUMENT_CACHE_DIR", "cache/documents", "document_cache")
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_document_from_bytes(data: bytes, filename: str) -> str:
    """
    Save raw document bytes to the cache and return the absolute file path.

    The cached filename preserves the original human-readable name with a
    unique prefix: ``doc_{uuid12}_{original_filename}``.

    Args:
        data: Raw document bytes.
        filename: Original filename (e.g. "report.pdf").

    Returns:
        Absolute path to the cached document file as a string.

    Raises:
        ValueError: If the sanitized path escapes the cache directory.
    """
    cache_dir = get_document_cache_dir()
    # Sanitize: strip directory components, null bytes, and control characters
    safe_name = Path(filename).name if filename else "document"
    safe_name = safe_name.replace("\x00", "").strip()
    if not safe_name or safe_name in {".", ".."}:
        safe_name = "document"
    cached_name = f"doc_{uuid.uuid4().hex[:12]}_{safe_name}"
    filepath = cache_dir / cached_name
    # Final safety check: ensure path stays inside cache dir
    if not filepath.resolve().is_relative_to(cache_dir.resolve()):
        raise ValueError(f"Path traversal rejected: {filename!r}")
    filepath.write_bytes(data)
    return str(filepath)


def cleanup_document_cache(max_age_hours: int = 24) -> int:
    """
    Delete cached documents older than *max_age_hours*.

    Returns the number of files removed.
    """
    return _cleanup_cache_dir(get_document_cache_dir(), max_age_hours)


class MessageType(Enum):
    """Types of incoming messages."""
    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"


class ProcessingOutcome(Enum):
    """Result classification for message-processing lifecycle hooks."""

    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass
class MessageEvent:
    """
    Incoming message from a platform.

    Normalized representation that all adapters produce.
    """
    # Message content
    text: str
    message_type: MessageType = MessageType.TEXT

    # Source information
    source: SessionSource = None

    # Original platform data
    raw_message: Any = None
    message_id: Optional[str] = None

    # Platform-specific update identifier, recorded by ``/restart``.
    platform_update_id: Optional[int] = None

    # Media attachments
    # media_urls: local file paths (for vision tool access)
    media_urls: List[str] = field(default_factory=list)
    media_types: List[str] = field(default_factory=list)

    # Reply context
    reply_to_message_id: Optional[str] = None
    reply_to_text: Optional[str] = None  # Text of the replied-to message (for context injection)
    reply_to_is_own_message: bool = False  # True when the user replied to this bot/assistant's message

    # Per-channel ephemeral system prompt. Applied at API call time and never
    # persisted to transcript history.
    channel_prompt: Optional[str] = None

    # Channel context prepended ahead of the trigger message by run.py.
    channel_context: Optional[str] = None

    # Internal flag — set for synthetic events (e.g. background process
    # completion notifications) that must bypass user authorization checks.
    internal: bool = False

    # Free-form per-event metadata; consumers must not rely on any particular
    # key existing.
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Timestamps
    timestamp: datetime = field(default_factory=datetime.now)

    # Whether this event may resolve gateway commands or pending control
    # prompts. Proactive plugin events set this to False so untrusted payload
    # text remains conversational input.
    allow_gateway_control: bool = True

    def is_command(self) -> bool:
        """Check if this is a command message (e.g., /new, /reset)."""
        return self.allow_gateway_control and (self.text or "").lstrip().startswith("/")

    def get_command(self) -> Optional[str]:
        """Extract command name if this is a command message."""
        if not self.is_command():
            return None
        # Split on space and get first word, strip the /
        command_text = (self.text or "").lstrip()
        parts = command_text.split(maxsplit=1)
        raw = parts[0][1:].lower() if parts else None
        if raw and "@" in raw:
            raw = raw.split("@", 1)[0]
        # Reject file paths: valid command names never contain /
        if raw and "/" in raw:
            return None
        return raw

    def get_command_args(self) -> str:
        """Get the arguments after a command."""
        if not self.is_command():
            return self.text
        command_text = (self.text or "").lstrip()
        parts = command_text.split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""
        # iOS auto-corrects -- to — (em dash) and - to – (en dash)
        args = args.replace("\u2014\u2014", "--").replace("\u2014", "--").replace("\u2013", "-")
        return args


@dataclass
class TextDebounceState:
    event: MessageEvent
    task: asyncio.Task | None
    first_ts: float
    last_ts: float


_PLAINTEXT_GATEWAY_RESTART_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:please\s+)?restart\s+(?:the\s+)?gateway[.!?\s]*$", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?restart\s+(?:the\s+)?son-of-anton\s+gateway[.!?\s]*$", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?restart\s+son-of-anton[.!?\s]*$", re.IGNORECASE),
)


def coerce_plaintext_gateway_command(event: "MessageEvent") -> None:
    """Rewrite a tiny set of DM plaintext admin phrases into slash commands.

    This keeps high-impact operational phrases like ``restart gateway`` out of
    the LLM/tool path, where they can trigger a self-restart from inside the
    currently running agent and leave the gateway stuck in ``draining`` while it
    waits for that same agent to finish.

    Scope is intentionally narrow: DM text messages only, exact restart-style
    phrases only. Group chats keep natural-language semantics.
    """
    try:
        if event is None or event.message_type != MessageType.TEXT:
            return
        text = (event.text or "").strip()
        if not text or text.startswith("/"):
            return
        source = getattr(event, "source", None)
        if getattr(source, "chat_type", None) != "dm":
            return
        for pattern in _PLAINTEXT_GATEWAY_RESTART_PATTERNS:
            if pattern.match(text):
                event.text = "/restart"
                return
    except Exception:
        return


@dataclass
class SendResult:
    """Result of sending a message."""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: Any = None


# Send-failure categories ``classify_send_error`` maps an error onto, so the
# gateway decides in one place whether a failure means the target is dead:
# too_long, bad_format, forbidden, not_found, rate_limited, transient, unknown.

# ``not_found`` substrings split by blast radius.  A *chat-level* not_found means
# the chat/user/group itself is gone, so the whole target is dead.  A
# *thread/topic/message-level* not_found (a deleted forum topic, an edited-away
# message) leaves the parent chat reachable — it must NOT mark the whole chat
# dead.  ``classify_send_error`` collapses both into ``"not_found"``;
# ``is_chat_level_not_found`` recovers the distinction for the dead-target path.
# See gateway.dead_targets.
_CHAT_LEVEL_NOT_FOUND_SUBSTRINGS = ("chat not found",)
_SUBCHAT_NOT_FOUND_SUBSTRINGS = (
    "message to edit not found",
    "message to reply not found",
    "thread not found",
    "topic_deleted",
    "message_id_invalid",
)


def _error_blob(exc: Optional[BaseException] = None, error_text: str = "") -> str:
    """Build the lowercased text blob both send-error classifiers match against.

    Single source of truth so ``classify_send_error`` and
    ``is_chat_level_not_found`` can never drift (e.g. one including the
    exception class name and the other not) and silently disagree on the same
    failure.  Includes ``str(exc)`` (when non-empty) and the exception's class
    name, plus any explicit ``error_text``.
    """
    parts = []
    if error_text:
        parts.append(error_text)
    if exc is not None:
        exc_str = str(exc)
        if exc_str:
            parts.append(exc_str)
        parts.append(exc.__class__.__name__)
    return " ".join(parts).lower()


def classify_send_error(exc: Optional[BaseException], error_text: str = "") -> str:
    """Map a send exception / error string to a :data:`SEND_ERROR_KINDS` value.

    Platform-neutral: matches on the lowercased text of ``exc`` (and/or the
    explicit ``error_text``) against the substrings the major messaging APIs
    use.  Conservative — anything unrecognized returns ``"unknown"`` so callers
    never mistake an unclassified failure for a benign one.
    """
    blob = _error_blob(exc, error_text)
    if not blob.strip():
        return "unknown"
    if "message_too_long" in blob or "too long" in blob or "message is too long" in blob:
        return "too_long"
    if (
        "can't parse entities" in blob
        or "cant parse entities" in blob
        or "can't find end" in blob
        or "unsupported start tag" in blob
        or ("entity" in blob and "parse" in blob)
        or ("bad request" in blob and "entit" in blob)
    ):
        return "bad_format"
    if (
        "forbidden" in blob
        or "bot was blocked" in blob
        or "blocked by the user" in blob
        or "user is deactivated" in blob
        or "not enough rights" in blob
        or "have no rights" in blob
        or "not a member" in blob
    ):
        return "forbidden"
    if any(s in blob for s in _CHAT_LEVEL_NOT_FOUND_SUBSTRINGS) or any(
        s in blob for s in _SUBCHAT_NOT_FOUND_SUBSTRINGS
    ):
        return "not_found"
    if (
        "flood" in blob
        or "too many requests" in blob
        or "retry after" in blob
        or "rate limit" in blob
    ):
        return "rate_limited"
    for pat in _RETRYABLE_ERROR_PATTERNS:
        if pat in blob:
            return "transient"
    if "connecttimeout" in blob:
        return "transient"
    return "unknown"


def is_chat_level_not_found(exc: Optional[BaseException] = None, error_text: str = "") -> bool:
    """Whether a ``not_found`` failure means the *whole chat* is gone.

    :func:`classify_send_error` collapses chat-level and thread/topic/message-level
    not_found into the single ``"not_found"`` kind.  Only the chat-level case (the
    chat/user/group no longer exists) should mark a delivery target dead; a deleted
    forum topic or an edited-away message leaves the parent chat reachable.  When
    both a chat-level and a sub-chat marker are present, the sub-chat reading wins
    (conservative: never kill a chat that may still be reachable).

    Argument order mirrors :func:`classify_send_error` (``exc`` first) and both
    share :func:`_error_blob`, so the two classifiers cannot disagree on the same
    failure.
    """
    blob = _error_blob(exc, error_text)
    if any(s in blob for s in _SUBCHAT_NOT_FOUND_SUBSTRINGS):
        return False
    return any(s in blob for s in _CHAT_LEVEL_NOT_FOUND_SUBSTRINGS)


class EphemeralReply(str):
    """System-notice reply from a slash-command handler.

    Subclassing ``str`` keeps the wrapper transparent to anything that
    treats handler return values as text; ``isinstance(r, EphemeralReply)``
    still distinguishes system notices from agent responses.
    """

    @property
    def text(self) -> str:
        """Return the underlying text.

        Provided for call sites that want an explicit string conversion,
        though ``str(reply)`` and using ``reply`` directly where a string
        is expected both work identically.
        """
        return str.__str__(self)


def _invalidate_pending_stt_cache(event: MessageEvent) -> None:
    """Clear the gateway-side voice-note cache when media is merged into an event.

    ``merge_pending_message_event`` extends ``media_urls`` in place when two
    media-bearing messages arrive in quick succession.  The gateway runner
    caches the voice-annotated text on the event via ``setattr`` (see
    ``_enrich_pending_audio_event_once``); if the cached event gains new
    media after the cache was populated, the stale text must be discarded so
    the next call picks up the merged attachments.
    """
    if hasattr(event, "_gateway_pending_stt_text"):
        delattr(event, "_gateway_pending_stt_text")


def merge_pending_message_event(
    pending_messages: Dict[str, MessageEvent],
    session_key: str,
    event: MessageEvent,
    *,
    merge_text: bool = False,
) -> None:
    """Store or merge a pending event for a session.

    Photo bursts/albums often arrive as multiple near-simultaneous PHOTO
    events. Merge those into the existing queued event so the next turn sees
    the whole burst.

    When ``merge_text`` is enabled, rapid follow-up TEXT events are appended
    instead of replacing the pending turn. This is used for bursty
    follow-ups so a multi-part user thought is not silently truncated to only
    the last queued fragment.
    """
    existing = pending_messages.get(session_key)
    if existing:
        existing_is_photo = getattr(existing, "message_type", None) == MessageType.PHOTO
        incoming_is_photo = event.message_type == MessageType.PHOTO
        existing_has_media = bool(existing.media_urls)
        incoming_has_media = bool(event.media_urls)

        if existing_is_photo and incoming_is_photo:
            existing.media_urls.extend(event.media_urls)
            existing.media_types.extend(event.media_types)
            if event.text:
                existing.text = BasePlatformAdapter._merge_caption(existing.text, event.text)
            _invalidate_pending_stt_cache(existing)
            return

        if existing_has_media or incoming_has_media:
            if incoming_has_media:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)
            if event.text:
                if existing.text:
                    existing.text = BasePlatformAdapter._merge_caption(existing.text, event.text)
                else:
                    existing.text = event.text
            if existing_is_photo or incoming_is_photo:
                existing.message_type = MessageType.PHOTO
            elif (
                getattr(existing, "message_type", None) == MessageType.TEXT
                and event.message_type != MessageType.TEXT
            ):
                existing.message_type = event.message_type
            _invalidate_pending_stt_cache(existing)
            return

        if (
            merge_text
            and getattr(existing, "message_type", None) == MessageType.TEXT
            and event.message_type == MessageType.TEXT
        ):
            if event.text:
                existing.text = f"{existing.text}\n{event.text}" if existing.text else event.text
            return

    pending_messages[session_key] = event


# Error substrings that indicate a transient *connection* failure worth retrying.
# "timeout" / "timed out" / "readtimeout" / "writetimeout" are intentionally
# excluded: a read/write timeout on a non-idempotent call (e.g. send_message)
# means the request may have reached the server — retrying risks duplicate
# delivery.  "connecttimeout" is safe because the connection was never
# established.  Platforms that know a timeout is safe to retry should set
# SendResult.retryable = True explicitly.
_RETRYABLE_ERROR_PATTERNS = (
    "connecterror",
    "connectionerror",
    "connectionreset",
    "connectionrefused",
    "connecttimeout",
    "network",
    "broken pipe",
    "remotedisconnected",
    "eoferror",
)


# Type for message handlers.  Handlers may return a plain string (normal
# reply), an ``EphemeralReply`` system notice, or
# ``None`` when the response was already delivered (e.g. via streaming).
MessageHandler = Callable[[MessageEvent], Awaitable[Optional[Union[str, "EphemeralReply"]]]]


def _strip_media_directives(text: str) -> str:
    """Strip internal delivery directives ([[audio_as_voice]], [[as_document]],
    MEDIA:<path>) so they never render as visible text.

    Backstop only: run ``extract_media`` first. MEDIA cleanup uses the shared
    ``MEDIA_TAG_CLEANUP_RE`` (only tags whose path has a known deliverable
    extension are removed; an unknown-extension tag is intentionally left so the
    bare-path detector downstream can still pick it up, per #34517). Validated
    extension-less tags (e.g. ``MEDIA:/output/Caddyfile``) are also removed.
    [[...]] is exact.
    """
    if not text:
        return text
    return _strip_media_tag_directives(text)


class BasePlatformAdapter(ABC):
    """
    Base class for platform adapters.

    Subclasses implement platform-specific logic for:
    - Connecting and authenticating
    - Receiving messages
    - Sending messages/responses
    - Handling media
    """

    def __init__(self, config: PlatformConfig, platform: Platform):
        self.config = config
        self.platform = platform
        self._message_handler: Optional[MessageHandler] = None
        self._running = False
        self._fatal_error_code: Optional[str] = None
        self._fatal_error_message: Optional[str] = None
        self._fatal_error_retryable = True

        # Track active message handlers per session for interrupt support.
        # _active_sessions stores the per-session interrupt Event; _session_tasks
        # maps session → the specific Task currently processing it so that
        # session-terminating commands (/stop, /new, /reset) can cancel the
        # right task and release the adapter-level guard deterministically.
        # Without the owner-task map, an old task's finally block could delete
        # a newer task's guard, leaving stale busy state.
        self._active_sessions: Dict[str, asyncio.Event] = {}
        self._pending_messages: Dict[str, MessageEvent] = {}
        self._session_tasks: Dict[str, asyncio.Task] = {}
        # Background message-processing tasks spawned by handle_message().
        # Gateway shutdown cancels these so an old gateway instance doesn't keep
        # working on a task after a restart.
        self._background_tasks: set[asyncio.Task] = set()
        # One-shot callbacks to fire after the main response is delivered.
        # Keyed by session_key. Values are either a bare callback (legacy) or
        # a ``(generation, callback)`` tuple so GatewayRunner can make deferred
        # deliveries generation-aware and avoid stale runs clearing callbacks
        # registered by a fresher run for the same session.
        self._post_delivery_callbacks: Dict[str, Any] = {}
        self._expected_cancelled_tasks: set[asyncio.Task] = set()
        self._busy_session_handler: Optional[Callable[[MessageEvent, str], Awaitable[bool]]] = None
        # Chats where typing indicator is paused (e.g. during approval waits).
        # _keep_typing skips send_typing when the chat_id is in this set.
        self._typing_paused: set = set()

    @property
    def has_fatal_error(self) -> bool:
        return self._fatal_error_message is not None

    @property
    def fatal_error_message(self) -> Optional[str]:
        return self._fatal_error_message

    @property
    def fatal_error_code(self) -> Optional[str]:
        return self._fatal_error_code

    @property
    def fatal_error_retryable(self) -> bool:
        return self._fatal_error_retryable

    def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
        self._running = False
        self._fatal_error_code = code
        self._fatal_error_message = message
        self._fatal_error_retryable = retryable
        self._write_runtime_status_safe("fatal", platform_state="fatal", error_code=code, error_message=message)

    def _write_runtime_status_safe(self, context: str, **kwargs) -> None:
        """Write runtime status; log first failure per context at warning, rest at debug.

        Status writes can fail on permissions, ENOSPC, missing status dir, etc.
        Logging every failure would spam the log on reconnect loops, so this
        surfaces the first failure per (platform, context) at warning level and
        downgrades subsequent failures to debug.
        """
        try:
            from gateway.status import write_runtime_status
            write_runtime_status(platform=self.platform.value, **kwargs)
        except Exception as exc:
            # Use getattr so object.__new__(...) test harnesses that skip __init__
            # don't blow up on attribute access.
            logged = getattr(self, "_status_write_logged", None)
            if logged is None:
                logged = set()
                try:
                    self._status_write_logged = logged
                except Exception:
                    pass
            key = (self.platform.value, context)
            if key not in logged:
                logger.warning(
                    "Failed to write runtime status (%s) for %s: %s (further failures at debug level)",
                    context, self.platform.value, exc,
                )
                logged.add(key)
            else:
                logger.debug("Failed to write runtime status (%s) for %s: %s", context, self.platform.value, exc)

    def _acquire_platform_lock(self, scope: str, identity: str, resource_desc: str) -> bool:
        """Acquire a scoped lock for this adapter. Returns True on success."""
        from gateway.status import acquire_scoped_lock, scoped_lock_owner_label

        self._platform_lock_scope = scope
        self._platform_lock_identity = identity
        acquired, existing = acquire_scoped_lock(
            scope, identity, metadata={'platform': self.platform.value}
        )
        if acquired:
            return True

        owner_pid = existing.get('pid') if isinstance(existing, dict) else None
        # Scoped locks are machine-global, so the holder is often a gateway
        # serving a different SON_OF_ANTON_HOME. A bare PID gives an operator no
        # way to tell WHICH gateway owns the credential — name its home when we can.
        owner_profile = scoped_lock_owner_label(existing)
        if owner_profile:
            holder = f" by the gateway for {owner_profile}"
            holder += f" (PID {owner_pid})" if owner_pid else ""
            remedy = " Stop that gateway first."
        else:
            holder = f" (PID {owner_pid})" if owner_pid else ""
            remedy = " Stop the other gateway first."
        message = f"{resource_desc} already in use{holder}.{remedy}"
        logger.error('[%s] %s', self.name, message)
        self._set_fatal_error(f'{scope}_lock', message, retryable=True)
        return False

    def _release_platform_lock(self) -> None:
        """Release the scoped lock acquired by _acquire_platform_lock."""
        identity = getattr(self, '_platform_lock_identity', None)
        if not identity:
            return
        from gateway.status import release_scoped_lock
        release_scoped_lock(self._platform_lock_scope, identity)
        self._platform_lock_identity = None

    @property
    def name(self) -> str:
        """Human-readable name for this adapter."""
        return self.platform.value.title()
    
    def set_message_handler(self, handler: MessageHandler) -> None:
        """
        Set the handler for incoming messages.
        
        The handler receives a MessageEvent and should return
        an optional response string.
        """
        self._message_handler = handler

    def set_busy_session_handler(self, handler: Optional[Callable[[MessageEvent, str], Awaitable[bool]]]) -> None:
        """Set an optional handler for messages arriving during active sessions."""
        self._busy_session_handler = handler

    def set_session_store(self, session_store: Any) -> None:
        """Set the session store used to dedup re-delivered media paths."""
        self._session_store = session_store

    def _history_media_paths_for_session(self, session_key: str) -> Optional[set]:
        """Return media paths already delivered in prior turns of this session.

        Loads the persisted transcript, drops the current turn, and scans the
        remaining history for MEDIA: tags. Used to prevent the model from
        re-delivering the same file when it echoes an old MEDIA tag.
        """
        store = getattr(self, "_session_store", None)
        if not store:
            return None
        try:
            # The transcript store is keyed by session_id, not the gateway
            # session_key — map through the routing index first. Falling back
            # to the raw key covers stores that accept either.
            session_id = None
            peek = getattr(store, "peek_session_id", None)
            if callable(peek):
                session_id = peek(session_key)
            transcript = store.load_transcript(session_id or session_key)
        except Exception:
            return None
        if not transcript:
            return None
        # Exclude the CURRENT TURN entirely — everything from the last user
        # message onward. The agent persists rows as it produces them, so by
        # delivery time the transcript already contains this turn's tool
        # results and assistant reply, whose paths must not suppress their own
        # delivery.
        history = list(transcript)
        last_user_idx = None
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx is not None:
            history = history[:last_user_idx]
        else:
            # No user row (unusual store shape): keep the old safe behavior of
            # at least excluding the trailing assistant reply.
            for msg in reversed(history):
                if msg.get("role") == "assistant":
                    history.remove(msg)
                    break
        if not history:
            return None
        # Avoid circular import: gateway.run already imports this module.
        from gateway.run import _collect_history_media_paths
        return _collect_history_media_paths(history)

    async def _bounded_history_media_paths_for_session(
        self, session_key: str
    ) -> Optional[set]:
        """Run best-effort history lookup in a bounded isolated daemon thread."""
        admission = _HISTORY_MEDIA_LOOKUP_ADMISSION
        if not admission.acquire(blocking=False):
            logger.warning(
                "[%s] Media-delivery history lookup capacity exhausted for %s; "
                "delivering bare local file path(s) without history dedup",
                self.name,
                session_key,
            )
            return None

        loop = asyncio.get_running_loop()
        result_future = loop.create_future()

        def _publish_result(result=None, error=None):
            if result_future.done():
                return
            if error is not None:
                result_future.set_exception(error)
            else:
                result_future.set_result(result)

        def _worker():
            try:
                result = self._history_media_paths_for_session(session_key)
            except BaseException as exc:
                try:
                    loop.call_soon_threadsafe(_publish_result, None, exc)
                except RuntimeError:
                    pass  # Event loop already closed during gateway shutdown.
            else:
                try:
                    loop.call_soon_threadsafe(_publish_result, result, None)
                except RuntimeError:
                    pass  # Event loop already closed during gateway shutdown.
            finally:
                admission.release()

        try:
            threading.Thread(
                target=_worker,
                name="media-history-lookup",
                daemon=True,
            ).start()
        except Exception:
            # Thread could not be started (e.g. thread exhaustion — start()
            # raises RuntimeError). The worker never ran, so its
            # finally-release never fires — release the admission permit here
            # to avoid leaking it permanently, and fail open like every other
            # path in this helper. (Unlike _worker's BaseException forwarding,
            # swallowing here must not eat KeyboardInterrupt/SystemExit.)
            admission.release()
            logger.warning(
                "[%s] Could not start media-delivery history lookup worker "
                "for %s; delivering bare local file path(s) without history "
                "dedup",
                self.name,
                session_key,
                exc_info=True,
            )
            return None
        try:
            return await asyncio.wait_for(
                result_future,
                timeout=_HISTORY_MEDIA_LOOKUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[%s] Timed out loading media-delivery history for %s; "
                "delivering bare local file path(s) without history dedup",
                self.name,
                session_key,
            )
            return None
        except Exception:
            # The worker publishes its own failure via set_exception; this
            # helper is documented as best-effort/fail-open, so swallow the
            # error here instead of letting it kill media delivery.
            logger.warning(
                "[%s] Media-delivery history lookup failed for %s; "
                "delivering bare local file path(s) without history dedup",
                self.name,
                session_key,
                exc_info=True,
            )
            return None

    @abstractmethod
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to the platform and start receiving messages.

        ``is_reconnect`` is True when the reconnect watcher re-establishes a
        platform that dropped after an outage. Returns True on success.
        """
        pass
    
    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the platform."""
        pass
    
    @abstractmethod
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> SendResult:
        """
        Send a message to a chat.
        
        Args:
            chat_id: The chat/channel ID to send to
            content: Message content (may be markdown)
            reply_to: Optional message ID to reply to
            metadata: Additional platform-specific options
        
        Returns:
            SendResult with success status and message ID
        """
        pass

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a button slash-command confirmation prompt.

        Signal has no buttons, so this reports "Not supported" and the gateway
        falls back to its text prompt (see
        ``GatewayRunner._request_slash_confirm``).
        """
        return SendResult(success=False, error="Not supported")

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a clarify prompt to the user as text.

        Multiple choice renders a numbered list and calls
        ``mark_awaiting_text()`` so the gateway's text-intercept captures the
        typed reply ("2" or the choice text); an open-ended question is sent as
        plain text and the next message in the session answers it.
        """
        if choices:
            # Multi-select clarifies register their flag on the pending entry;
            # look it up by id so the signature stays adapter-compatible.
            _is_multi = False
            try:
                from tools import clarify_gateway as _cg
                with _cg._lock:
                    _entry = _cg._entries.get(clarify_id)
                _is_multi = bool(_entry and getattr(_entry, "multi_select", False))
            except Exception:
                _is_multi = False
            lines = [f"❓ {question}", ""]
            for i, choice in enumerate(choices, start=1):
                lines.append(f"  {i}. {choice}")
            lines.append("")
            if _is_multi:
                lines.append(
                    "Multiple selections allowed — reply with the numbers "
                    "separated by commas or spaces (e.g. \"1, 3\"), the option "
                    "text, or your own answer."
                )
            else:
                lines.append("Reply with the number, the option text, or your own answer.")
            text = "\n".join(lines)
            # Text fallback: enable text-capture so the gateway intercept
            # picks up the user's typed reply (e.g. "2" or choice text).
            from tools.clarify_gateway import mark_awaiting_text
            mark_awaiting_text(clarify_id)
        else:
            text = f"❓ {question}"
        return await self.send(
            chat_id=chat_id,
            content=text,
            metadata=metadata,
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send a typing indicator."""

    async def stop_typing(self, chat_id: str) -> None:
        """Stop a typing indicator."""

    async def _stop_typing_with_metadata(self, chat_id: str, metadata=None) -> None:
        """Stop typing; Signal keys typing state by chat, so metadata is unused."""
        await self.stop_typing(chat_id)

    @staticmethod
    def extract_images(content: str) -> Tuple[List[Tuple[str, str]], str]:
        """
        Extract image URLs from markdown and HTML image tags in a response.
        
        Finds patterns like:
        - ![alt text](https://example.com/image.png)
        - <img src="https://example.com/image.png">
        - <img src="https://example.com/image.png"></img>
        
        Args:
            content: The response text to scan.
        
        Returns:
            Tuple of (list of (url, alt_text) pairs, cleaned content with image tags removed).
        """
        images = []
        cleaned = content
        
        # Match markdown images: ![alt](url)
        md_pattern = r'!\[([^\]]*)\]\((https?://[^\s\)]+)\)'
        for match in re.finditer(md_pattern, content):
            alt_text = match.group(1)
            url = match.group(2)
            # Only extract URLs that look like actual images
            if any(url.lower().endswith(ext) or ext in url.lower() for ext in
                   ['.png', '.jpg', '.jpeg', '.gif', '.webp', 'fal.media', 'fal-cdn', 'replicate.delivery']):
                images.append((url, alt_text))
        
        # Match HTML img tags: <img src="url"> or <img src="url"></img> or <img src="url"/>
        html_pattern = r'<img\s+src=["\']?(https?://[^\s"\'<>]+)["\']?\s*/?>\s*(?:</img>)?'
        for match in re.finditer(html_pattern, content):
            url = match.group(1)
            images.append((url, ""))
        
        # Remove only the matched image tags from content (not all markdown images)
        if images:
            extracted_urls = {url for url, _ in images}
            def _remove_if_extracted(match):
                url = match.group(2) if match.lastindex >= 2 else match.group(1)
                return '' if url in extracted_urls else match.group(0)
            cleaned = re.sub(md_pattern, _remove_if_extracted, cleaned)
            cleaned = re.sub(html_pattern, _remove_if_extracted, cleaned)
            # Clean up leftover blank lines
            cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
        
        return images, cleaned
    
    async def _notify_media_delivery_failure(
        self,
        chat_id: str,
        media_path: str,
        *,
        is_voice: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Send a user-visible notice when a MEDIA attachment could not be delivered.

        The non-streaming dispatch loop strips ``MEDIA:`` tags before sending
        attachments. When the subsequent upload returns ``success=False`` (for
        example Discord accepted the message but attached nothing), the user
        must see a failure notice instead of a silent drop (#66797).
        """
        ext = Path(media_path).suffix.lower()
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
        if is_voice or should_send_media_as_audio(self.platform, ext, is_voice=is_voice):
            text = "⚠️ Couldn't deliver the audio attachment."
        elif ext in _VIDEO_EXTS:
            text = "⚠️ Couldn't deliver the video attachment."
        else:
            file_name = os.path.basename(media_path)
            text = f"⚠️ Couldn't deliver the file attachment ({file_name})."
        try:
            notice = await self.send(chat_id=chat_id, content=text, metadata=metadata)
            if not notice.success:
                logger.debug(
                    "[%s] Could not send media-delivery-failure notice: %s",
                    self.name,
                    notice.error,
                )
        except Exception as notify_err:
            logger.debug(
                "[%s] Could not send media-delivery-failure notice: %s",
                self.name,
                notify_err,
            )

    @staticmethod
    def validate_media_delivery_path(path: str) -> Optional[str]:
        """Return a resolved path if it is safe for native attachment upload."""
        return validate_media_delivery_path(path)

    @staticmethod
    def filter_media_delivery_paths(media_files) -> List[Tuple[str, bool]]:
        """Drop unsafe MEDIA paths and normalize accepted paths."""
        safe_media: List[Tuple[str, bool]] = []
        for media_path, is_voice in media_files or []:
            raw = str(media_path)
            safe_path = validate_media_delivery_path(raw)
            if safe_path:
                safe_media.append((safe_path, bool(is_voice)))
            else:
                logger.warning("Skipping unsafe MEDIA directive path: %s", _log_safe_path(raw))
        return safe_media

    @staticmethod
    def filter_local_delivery_paths(file_paths) -> List[str]:
        """Drop unsafe bare local file paths and normalize accepted paths."""
        safe_paths: List[str] = []
        for file_path in file_paths or []:
            raw = str(file_path)
            safe_path = validate_media_delivery_path(raw)
            if safe_path:
                safe_paths.append(safe_path)
            else:
                logger.warning("Skipping unsafe local file path: %s", _log_safe_path(raw))
        return safe_paths


    @staticmethod
    def _mask_protected_spans(content: str) -> str:
        """Replace content inside fenced code blocks, inline code spans,
        and blockquotes with spaces to prevent MEDIA: false positives.

        Preserves character count so regex match offsets stay valid.
        Skips masking backtick-quoted paths in MEDIA: tags (e.g.
        ``MEDIA:`/path/to/file.png` ``) to avoid breaking path extraction.
        """
        chars = list(content)

        # Build list of (start, end) spans to mask
        spans: list = []

        # Fenced code blocks: ```...```
        for m in re.finditer(r'```[^\n]*\n.*?```', content, re.DOTALL):
            spans.append((m.start(), m.end()))

        # Inline code: `...` but NOT backtick-quoted paths in MEDIA: tags
        for m in re.finditer(r'`[^`\n]+`', content):
            start = m.start()
            # Check if this is a backtick-quoted path after MEDIA:
            prefix = content[max(0, start - 20):start]
            if re.search(r'MEDIA:\s*$', prefix):
                continue  # This is a MEDIA path quote, not inline code
            # A whole tag wrapped in inline code (`MEDIA:/path.csv`) is a real
            # delivery directive, not a prose example — models routinely format
            # file paths as inline code. Deliver it IF the path validates
            # (exists on disk, not denylisted). Prose examples with
            # non-existent paths stay masked (#35695), and fenced code blocks
            # are always masked regardless.
            inner = m.group(0)[1:-1].strip()
            if inner.upper().startswith("MEDIA:"):
                candidate = _normalize_media_tag_path(inner[6:])
                if candidate and validate_media_delivery_path(candidate):
                    continue  # Real deliverable tag in inline code — keep it scannable
            spans.append((start, m.end()))

        # Blockquote lines: > at line start
        for m in re.finditer(r'^>.*$', content, re.MULTILINE):
            spans.append((m.start(), m.end()))

        # Apply masking
        for start, end in spans:
            for i in range(start, end):
                if chars[i] != '\n':
                    chars[i] = ' '

        return ''.join(chars)


    @staticmethod
    def _mask_json_string_media(content: str) -> str:
        """Blank out ``MEDIA:<bare-path>`` occurrences that sit inside a JSON
        string *value* so they are never delivered as real attachments.

        Serialized tool results frequently embed a previous reply's text, e.g.::

            {"result": "MEDIA:/Users/x/.son-of-anton/media/generated/stale.png"}

        Here the ``MEDIA:`` is part of stored text, not an outbound directive,
        but the bare-path branch of ``MEDIA_TAG_CLEANUP_RE`` would still match it
        and re-deliver a stale file. (Regression report #34375.)

        The discriminator is precise so legitimate tags are untouched:

        * Only spans opened by a JSON value-context quote (``:``, ``,``, ``{`` or
          ``[`` immediately before the ``"``) are considered.
        * Within such a span, only a ``MEDIA:`` followed by a **bare** path
          (``/``, ``~/`` or ``X:\\``) is masked. A ``MEDIA:"..."`` quoted-path
          tag — a real LLM output format the extractor supports — is not bare and
          is left alone.
        * Tags at line start, after prose whitespace, or indented are outside any
          JSON value span and are never affected.

        Offsets are preserved (matched chars replaced with spaces, newlines kept)
        so downstream match positions stay valid.
        """
        if '"' not in content or "MEDIA:" not in content:
            return content
        chars = list(content)
        # JSON value-context string: a quote preceded by : , { or [ (optional ws),
        # capturing the (escape-aware) string body up to the closing quote.
        for m in re.finditer(r'(?<=[:,{\[])\s*"((?:[^"\\\n]|\\.)*)"', content):
            seg = m.group(1)
            if re.search(r'MEDIA:\s*(?:~/|/|[A-Za-z]:[/\\])', seg):
                for i in range(m.start(1), m.end(1)):
                    if chars[i] != '\n':
                        chars[i] = ' '
        return ''.join(chars)

    @staticmethod
    def extract_media(content: str) -> Tuple[List[Tuple[str, bool]], str]:
        """
        Extract MEDIA:<path> tags and [[audio_as_voice]] directives from response text.

        The TTS tool returns responses like:
            [[audio_as_voice]]
            MEDIA:/path/to/audio.ogg

        Turns that produce large/lossless images (e.g. an info-graph, where a
        rendered JPG is 1-2 MB but the photo path recompresses to
        ~200 KB at 1280px) can use ``[[as_document]]`` to request unmodified
        delivery via sendDocument instead of sendPhoto/sendMediaGroup. The
        directive is detected at the dispatch sites (which have access to the
        original response); this method just strips it so it never leaks into
        user-visible text. Per-file granularity is intentionally not exposed —
        when an agent emits ``[[as_document]]`` once, every image path in the
        same response is delivered as a document, mirroring the all-or-nothing
        scope of ``[[audio_as_voice]]``.

        Args:
            content: The response text to scan.

        Returns:
            Tuple of (list of (path, is_voice) pairs, cleaned content with tags removed).
        """
        media = []
        cleaned = content

        # Check for [[audio_as_voice]] directive
        has_voice_tag = "[[audio_as_voice]]" in content
        cleaned = cleaned.replace("[[audio_as_voice]]", "")
        # Strip [[as_document]] directive — callers inspect the original
        # ``content`` for it (so they can still react to it); here we just
        # keep it out of the user-visible cleaned text.
        cleaned = cleaned.replace("[[as_document]]", "")
        
        # Extract MEDIA:<path> tags, allowing optional whitespace after the colon
        # and quoted/backticked paths for LLM-formatted outputs. The extension
        # set is the shared MEDIA_DELIVERY_EXTS source of truth (built once into
        # MEDIA_TAG_CLEANUP_RE) so it can never drift from extract_local_files.
        media_pattern = MEDIA_TAG_CLEANUP_RE
        # Mask example/stored MEDIA: paths before scanning so they are never
        # delivered as real attachments:
        #  - code blocks / inline code / blockquotes hold prose examples (#35695)
        #  - serialized JSON string values hold stored tool-result text (#34375)
        # Both maskers are offset-preserving (chars -> spaces) so match offsets
        # stay valid; chaining them masks the union of both protected regions.
        scan_content = BasePlatformAdapter._mask_protected_spans(content)
        scan_content = BasePlatformAdapter._mask_json_string_media(scan_content)
        # Dedupe on the expanded path (first occurrence wins) so the same file
        # referenced twice in one response — e.g. a MEDIA tag inline AND in a
        # summary footer — is uploaded once, not twice (#29131).
        seen_paths: set = set()
        for match in media_pattern.finditer(scan_content):
            path = _normalize_media_tag_path(match.group("path"))
            if path:
                # ``[[audio_as_voice]]`` is message-global, but it must only
                # affect audio files. Tagging a non-audio file (image, video,
                # document) as is_voice taints it: an image flagged is_voice is
                # excluded from the embedded-photo batch and falls through to
                # send_document, arriving as a file attachment instead of an
                # inline photo. Gating on the extension lets one message carry
                # an embedded image AND a voice bubble together.
                ext = os.path.splitext(path)[1].lower()
                is_voice = has_voice_tag and ext in _AUDIO_EXTS
                try:
                    expanded = os.path.expanduser(path)
                except (OSError, RuntimeError, ValueError):
                    # Skip a crafted ~\x00 path rather than aborting extraction
                    # and dropping every other attachment in the response.
                    continue
                if expanded not in seen_paths:
                    seen_paths.add(expanded)
                    media.append((expanded, is_voice))

        for match in MEDIA_EXTENSIONLESS_TAG_RE.finditer(scan_content):
            path = _normalize_media_tag_path(match.group("path"))
            if not path or not _path_lacks_deliverable_extension(path):
                continue
            resolved = _match_extensionless_path(scan_content, match)
            if resolved is None:
                continue
            safe = resolved[0]
            if safe not in seen_paths:
                _safe_ext = os.path.splitext(safe)[1].lower()
                media.append((safe, has_voice_tag and _safe_ext in _AUDIO_EXTS))
                seen_paths.add(safe)

        # Remove the delivered MEDIA tags from the user-visible text. Mask a
        # length-equal copy of ``cleaned`` (same union of protected regions) to
        # *locate* the real tag spans, then delete exactly those spans from the
        # *unmasked* ``cleaned``. Masking is only a locator — protected spans
        # (code blocks, quotes, JSON-embedded MEDIA: text) must survive verbatim
        # in the delivered text, not be blanked to whitespace. Masking
        # ``cleaned`` (not ``content``) keeps offsets valid after the
        # [[audio_as_voice]] / [[as_document]] directives are removed.
        if media:
            masked_cleaned = BasePlatformAdapter._mask_protected_spans(cleaned)
            masked_cleaned = BasePlatformAdapter._mask_json_string_media(masked_cleaned)
            spans = [m.span() for m in media_pattern.finditer(masked_cleaned)]
            for match in MEDIA_EXTENSIONLESS_TAG_RE.finditer(masked_cleaned):
                path = _normalize_media_tag_path(match.group("path"))
                if not path or not _path_lacks_deliverable_extension(path):
                    continue
                resolved = _match_extensionless_path(masked_cleaned, match)
                if resolved is not None:
                    spans.append((match.start(), resolved[1]))
            if spans:
                chars = list(cleaned)
                for start, end in reversed(_merge_spans(spans)):
                    del chars[start:end]
                cleaned = "".join(chars)
                cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
        
        return media, cleaned

    @staticmethod
    def strip_media_directives_for_display(text: str) -> str:
        """Strip MEDIA: directives from streamed/display text.

        Known-extension tags are removed unconditionally (same as
        ``MEDIA_TAG_CLEANUP_RE``). Extension-less tags are removed only when
        ``validate_media_delivery_path`` accepts the path so undeliverable
        paths stay visible for debugging.
        """
        if (
            "MEDIA:" not in text
            and "[[audio_as_voice]]" not in text
            and "[[as_document]]" not in text
        ):
            return text
        cleaned = _strip_media_tag_directives(text)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.rstrip()

    @staticmethod
    def extract_local_files(content: str) -> Tuple[List[str], str]:
        """
        Detect bare local file paths in response text for native delivery.

        Matches absolute paths (/...) and tilde paths (~/) ending in common
        image, video, audio, or document extensions.  Validates each
        candidate with ``os.path.isfile()`` to avoid false positives from
        URLs or non-existent paths.

        The extension list is broader than just images/video so the agent
        can produce arbitrary artifacts (charts, PDFs, spreadsheets, code
        archives, CSVs) and have them ship to the user as native uploads
        without needing an explicit ``MEDIA:`` tag.  Image / video
        extensions still embed inline where the platform supports it;
        document extensions route through ``send_document``.  The dispatch
        partition lives in ``gateway/run.py``.

        Paths inside fenced code blocks (``` ... ```) and inline code
        (`...`) are ignored so that code samples are never mutilated.

        Returns:
            Tuple of (list of expanded file paths, cleaned text with the
            raw path strings removed).
        """
        _LOCAL_MEDIA_EXTS = MEDIA_DELIVERY_EXTS
        ext_part = '|'.join(e.lstrip('.') for e in _LOCAL_MEDIA_EXTS)

        # (?<![/:\w.]) prevents matching inside URLs (e.g. https://…/img.png)
        #             and relative paths (./foo.png)
        # (?:~/|/)    anchors to absolute or home-relative Unix paths
        # (?:[A-Za-z]:[/\\]) anchors to Windows drive-letter paths (#34632)
        path_re = re.compile(
            r'(?<![/:\w.])(?:~/|/|[A-Za-z]:[/\\])(?:[\w.\-]+[/\\])*[\w.\-]+\.(?:' + ext_part + r')\b',
            re.IGNORECASE,
        )

        # Build spans covered by fenced code blocks and inline code
        code_spans: list = []
        for m in re.finditer(r'```[^\n]*\n.*?```', content, re.DOTALL):
            code_spans.append((m.start(), m.end()))
        for m in re.finditer(r'`[^`\n]+`', content):
            code_spans.append((m.start(), m.end()))

        def _in_code(pos: int) -> bool:
            return any(s <= pos < e for s, e in code_spans)

        found: list = []  # (raw_match_text, expanded_path)
        for match in path_re.finditer(content):
            if _in_code(match.start()):
                continue
            raw = match.group(0)
            expanded = os.path.expanduser(raw)
            if os.path.isfile(expanded):
                found.append((raw, expanded))
            else:
                # The reply mentions a deliverable-looking path that does not
                # exist on disk, so it is silently dropped from native delivery.
                # This is the most common reason a promised file never arrives
                # (the model said "here's your file" but never wrote it, or
                # referenced the wrong path). Log it so the gap is visible in
                # gateway.log rather than vanishing without a trace.
                logger.info(
                    "Skipping bare file path in reply (no file on disk): %s",
                    _log_safe_path(raw),
                )

        # Deduplicate by expanded path, preserving discovery order
        seen: set = set()
        unique: list = []
        for raw, expanded in found:
            if expanded not in seen:
                seen.add(expanded)
                unique.append((raw, expanded))

        paths = [expanded for _, expanded in unique]

        cleaned = content
        if unique:
            for raw, _exp in unique:
                cleaned = cleaned.replace(raw, '')
            cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

        return paths, cleaned

    async def _keep_typing(
        self,
        chat_id: str,
        interval: float = 2.0,
        metadata=None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """
        Continuously send typing indicator until cancelled.
        
        Platform typing status expires after ~5 seconds, so we refresh every 2
        to recover quickly after progress messages interrupt it.
        
        Skips send_typing when the chat is in ``_typing_paused`` (e.g. while
        the agent is waiting for dangerous-command approval).  This is critical
        for Slack's Assistant API where ``assistant_threads_setStatus`` disables
        the compose box — pausing lets the user type ``/approve`` or ``/deny``.

        Each ``send_typing`` call is bounded by a ~1.5s timeout so a slow
        network round-trip can't stall the refresh cadence.  Discord-side
        typing expires after ~5s; if any individual send_typing
        takes longer than the refresh interval, the bubble would die and
        stay dead until that call returns.  Abandoning the slow call lets
        the next tick fire a fresh send_typing on schedule — as long as
        one of them succeeds within the 5s platform-side window, the bubble
        stays visible across provider stalls / upstream API timeouts.
        """
        # Bound each send_typing round-trip so the refresh cadence isn't
        # gated on network health.  Must stay below ``interval`` so a slow
        # call gets abandoned before the next scheduled tick.
        _send_typing_timeout = max(0.25, min(1.5, interval - 0.25))
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
                if chat_id not in self._typing_paused:
                    try:
                        await asyncio.wait_for(
                            self.send_typing(chat_id, metadata=metadata),
                            timeout=_send_typing_timeout,
                        )
                    except asyncio.TimeoutError:
                        # Slow network — abandon this tick, keep the loop
                        # on schedule so the next send_typing fires fresh.
                        pass
                    except asyncio.CancelledError:
                        raise
                    except Exception as typing_err:
                        logger.debug(
                            "[%s] send_typing error (non-fatal): %s",
                            self.name, typing_err,
                        )
                if stop_event is None:
                    await asyncio.sleep(interval)
                    continue
                loop = asyncio.get_running_loop()
                deadline = loop.time() + interval
                while not stop_event.is_set():
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    # Poll instead of wait_for(stop_event.wait()).  Cancelling
                    # wait_for while it owns the inner Event.wait task can leave
                    # shutdown paths stuck awaiting the typing task on Python
                    # 3.11/pytest-asyncio; sleep cancellation is immediate.
                    await asyncio.sleep(min(0.25, remaining))
                if stop_event.is_set():
                    return
        except asyncio.CancelledError:
            pass  # Normal cancellation when handler completes
        finally:
            # Ensure the underlying platform typing loop is stopped.
            # _keep_typing may have called send_typing() after an outer
            # stop_typing() cleared the task dict, recreating the loop.
            # Cancelling _keep_typing alone won't clean that up.
            if hasattr(self, "stop_typing"):
                try:
                    await self._stop_typing_with_metadata(chat_id, metadata)
                except Exception:
                    pass
            self._typing_paused.discard(chat_id)

    async def _stop_typing_refresh(
        self,
        chat_id: str,
        typing_task: asyncio.Task | None = None,
        *,
        metadata=None,
        timeout: float = 0.5,
        stop_attempts: int = 2,
    ) -> None:
        """Stop the refresh task and platform typing state as one operation."""
        self._typing_paused.add(chat_id)
        try:
            if typing_task is not None and not typing_task.done():
                typing_task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(typing_task), timeout=timeout)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    # The task is cancelled; don't let a slow adapter-specific
                    # cleanup block response delivery or shutdown.
                    pass
            if not hasattr(self, "stop_typing"):
                return
            attempts = max(1, stop_attempts)
            for attempt in range(attempts):
                try:
                    await self._stop_typing_with_metadata(chat_id, metadata)
                except Exception:
                    pass
                if attempt < attempts - 1:
                    await asyncio.sleep(0)
        finally:
            self._typing_paused.discard(chat_id)

    def pause_typing_for_chat(self, chat_id: str) -> None:
        """Pause typing indicator for a chat (e.g. during approval waits).

        Thread-safe (CPython GIL) — can be called from the sync agent thread
        while ``_keep_typing`` runs on the async event loop.
        """
        self._typing_paused.add(chat_id)

    def resume_typing_for_chat(self, chat_id: str) -> None:
        """Resume typing indicator for a chat after approval resolves."""
        self._typing_paused.discard(chat_id)

    async def interrupt_session_activity(self, session_key: str, chat_id: str, metadata=None) -> None:
        """Signal the active session loop to stop and clear typing immediately."""
        if session_key:
            interrupt_event = self._active_sessions.get(session_key)
            if interrupt_event is not None:
                interrupt_event.set()
        try:
            await self._stop_typing_with_metadata(chat_id, metadata)
        except Exception:
            pass

    def register_post_delivery_callback(
        self,
        session_key: str,
        callback: Callable,
        *,
        generation: int | None = None,
    ) -> None:
        """Register a deferred callback to fire after the main response.

        ``generation`` lets callers tie the callback to a specific gateway run
        generation so stale runs cannot clear callbacks owned by a fresher run.

        If a callback for the same ``session_key`` (and generation, when set)
        is already registered, the new callback is chained — both fire, in
        registration order, with per-callback exception isolation. This lets
        independent features (background-review release + temporary-bubble
        cleanup) coexist without clobbering each other. Stale-generation
        callers never overwrite a fresher generation's slot.
        """
        if not session_key or not callable(callback):
            return

        existing = self._post_delivery_callbacks.get(session_key)
        if existing is not None:
            if isinstance(existing, tuple) and len(existing) == 2:
                existing_gen, existing_cb = existing
            else:
                existing_gen, existing_cb = None, existing
            # Stale-generation registrations never overwrite a fresher slot.
            if (
                existing_gen is not None
                and generation is not None
                and int(generation) < int(existing_gen)
            ):
                return
            # Same-or-newer generation: chain with the existing callback so
            # both fire in registration order.
            if callable(existing_cb) and (
                existing_gen is None
                or generation is None
                or int(existing_gen) == int(generation)
            ):
                _prev = existing_cb
                _new = callback

                async def _chained() -> None:
                    # Both _prev and _new may be sync or async. The chained
                    # wrapper itself must be async because the outer invoker
                    # (``_handle_message`` etc.) awaits awaitable callbacks; a
                    # sync wrapper here would call ``_prev()`` / ``_new()`` and
                    # silently drop any returned coroutine, breaking chained
                    # async post-delivery hooks (e.g. ``/goal`` continuations).
                    for _cb in (_prev, _new):
                        try:
                            _result = _cb()
                            if inspect.isawaitable(_result):
                                await _result
                        except Exception:
                            logger.debug(
                                "Post-delivery callback failed", exc_info=True
                            )

                callback = _chained

        if generation is None:
            self._post_delivery_callbacks[session_key] = callback
        else:
            self._post_delivery_callbacks[session_key] = (int(generation), callback)

    def pop_post_delivery_callback(
        self,
        session_key: str,
        *,
        generation: int | None = None,
    ) -> Callable | None:
        """Pop a deferred callback, optionally requiring generation ownership."""
        if not session_key:
            return None
        entry = self._post_delivery_callbacks.get(session_key)
        if entry is None:
            return None
        if isinstance(entry, tuple) and len(entry) == 2:
            entry_generation, callback = entry
            if generation is not None and int(entry_generation) != int(generation):
                return None
            self._post_delivery_callbacks.pop(session_key, None)
            return callback if callable(callback) else None
        if generation is not None:
            return None
        self._post_delivery_callbacks.pop(session_key, None)
        return entry if callable(entry) else None

    # ── Processing lifecycle hooks ──────────────────────────────────────────
    # Subclasses override these to react to message processing events
    # (Signal adds 👀/✅/❌ reactions).

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Hook called when background processing begins."""

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Hook called when background processing completes."""

    async def _run_processing_hook(self, hook_name: str, *args: Any, **kwargs: Any) -> None:
        """Run a lifecycle hook without letting failures break message flow."""
        hook = getattr(self, hook_name, None)
        if not callable(hook):
            return
        try:
            await hook(*args, **kwargs)
        except Exception as e:
            logger.warning("[%s] %s hook failed: %s", self.name, hook_name, e)

    @staticmethod
    def _is_retryable_error(error: Optional[str]) -> bool:
        """Return True if the error string looks like a transient network failure."""
        if not error:
            return False
        lowered = error.lower()
        return any(pat in lowered for pat in _RETRYABLE_ERROR_PATTERNS)

    @staticmethod
    def _is_timeout_error(error: Optional[str]) -> bool:
        """Return True if the error string indicates a read/write timeout.

        Timeout errors are NOT retryable and should NOT trigger plain-text
        fallback — the request may have already been delivered.
        """
        if not error:
            return False
        lowered = error.lower()
        return "timed out" in lowered or "readtimeout" in lowered or "writetimeout" in lowered

    def _unwrap_ephemeral(self, response: Any) -> Optional[str]:
        """Unwrap a handler response (plain string, ``None`` or
        :class:`EphemeralReply`) into its text."""
        if isinstance(response, EphemeralReply):
            return response.text
        return response

    async def _send_with_retry(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Any = None,
        max_retries: int = 2,
        base_delay: float = 2.0,
    ) -> "SendResult":
        """
        Send a message with automatic retry for transient network errors.

        On permanent failures (e.g. formatting / permission errors) falls back
        to a plain-text version before giving up. If all attempts fail due to
        network errors, sends the user a brief delivery-failure notice so they
        know to retry rather than waiting indefinitely.
        """

        result = await self.send(
            chat_id=chat_id,
            content=content,
            reply_to=reply_to,
            metadata=metadata,
        )

        if result.success:
            return result

        error_str = result.error or ""
        is_network = result.retryable or self._is_retryable_error(error_str)

        # Timeout errors are not safe to retry (message may have been
        # delivered) and not formatting errors — return the failure as-is.
        if not is_network and self._is_timeout_error(error_str):
            return result

        if is_network:
            # Retry with exponential backoff for transient errors.
            # Honor server-requested retry_after
            # when present — it is authoritative over our backoff schedule.
            server_retry_after = result.retry_after
            for attempt in range(1, max_retries + 1):
                if server_retry_after is not None:
                    delay = server_retry_after + random.uniform(0, 1)
                    server_retry_after = None  # only honor once per send
                else:
                    delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                logger.warning(
                    "[%s] Send failed (attempt %d/%d, retrying in %.1fs): %s",
                    self.name, attempt, max_retries, delay, error_str,
                )
                await asyncio.sleep(delay)
                result = await self.send(
                    chat_id=chat_id,
                    content=content,
                    reply_to=reply_to,
                    metadata=metadata,
                )
                if result.success:
                    logger.info("[%s] Send succeeded on retry %d", self.name, attempt)
                    return result
                error_str = result.error or ""
                if result.retry_after is not None:
                    server_retry_after = result.retry_after
                if not (result.retryable or self._is_retryable_error(error_str)):
                    break  # error switched to non-transient — fall through to plain-text fallback
            else:
                # All retries exhausted (loop completed without break) — notify user
                logger.error("[%s] Failed to deliver response after %d retries: %s", self.name, max_retries, error_str)
                notice = (
                    "\u26a0\ufe0f Message delivery failed after multiple attempts. "
                    "Please try again \u2014 your request was processed but the response could not be sent."
                )
                try:
                    await self.send(chat_id=chat_id, content=notice, reply_to=reply_to, metadata=metadata)
                except Exception as notify_err:
                    logger.debug("[%s] Could not send delivery-failure notice: %s", self.name, notify_err)
                return result

        # Non-network / post-retry formatting failure: try plain text as fallback
        logger.warning("[%s] Send failed: %s — trying plain-text fallback", self.name, error_str)
        fallback_result = await self.send(
            chat_id=chat_id,
            content=f"(Response formatting failed, plain text:)\n\n{content[:3500]}",
            reply_to=reply_to,
            metadata=metadata,
        )
        if not fallback_result.success:
            logger.error("[%s] Fallback send also failed: %s", self.name, fallback_result.error)
        return fallback_result

    @staticmethod
    def _merge_caption(existing_text: Optional[str], new_text: str) -> str:
        """Merge a new caption into existing text, avoiding duplicates.

        Uses line-by-line exact match (not substring) to prevent false positives
        where a shorter caption is silently dropped because it appears as a
        substring of a longer one (e.g. "Meeting" inside "Meeting agenda").
        Whitespace is normalised for comparison.
        """
        if not existing_text:
            return new_text
        existing_captions = [c.strip() for c in existing_text.split("\n\n")]
        if new_text.strip() not in existing_captions:
            return f"{existing_text}\n\n{new_text}".strip()
        return existing_text

    def _text_debounce_store(self) -> dict[str, TextDebounceState]:
        store = getattr(self, "_text_debounce", None)
        if store is None:
            store = {}
            self._text_debounce = store
        return store

    def _is_queue_text_debounce_candidate(self, event: MessageEvent) -> bool:
        """Return True for normal text eligible for busy-text debounce."""
        result = (
            event.message_type == MessageType.TEXT
            and not getattr(event, "internal", False)
            and not event.is_command()
            and bool((event.text or "").strip())
        )
        if result:
            logger.debug(
                "[%s] Queue-text debounce candidate accepted: session=%s text_len=%d",
                self.name,
                getattr(event, "session_key", "?"),
                len(event.text or ""),
            )
        return result

    def _can_merge_text_debounce_events(self, existing: MessageEvent, event: MessageEvent) -> bool:
        """Return True when two text debounce events came from the same sender."""

        def _identity(candidate: MessageEvent) -> tuple[str, ...] | None:
            source = getattr(candidate, "source", None)
            if source is None:
                return None
            platform = _platform_name(getattr(source, "platform", None))
            sender = getattr(source, "user_id_alt", None) or getattr(source, "user_id", None)
            if sender:
                return (platform, str(sender))
            if getattr(source, "chat_type", None) in {"dm", "private"} and getattr(source, "chat_id", None):
                return (platform, "dm", str(source.chat_id))
            return None

        existing_sender = _identity(existing)
        incoming_sender = _identity(event)
        return existing_sender is not None and existing_sender == incoming_sender

    def _text_debounce_delay(self, session_key: str) -> float:
        """Return bounded busy-text debounce delay for ``session_key``."""
        state = self._text_debounce_store().get(session_key)
        if state is None:
            return 0.0
        now = time.monotonic()
        window_deadline = state.last_ts + self._busy_text_debounce_seconds
        hard_cap_deadline = state.first_ts + self._busy_text_hard_cap_seconds
        return max(0.0, min(window_deadline, hard_cap_deadline) - now)

    async def _queue_text_debounce(self, session_key: str, event: MessageEvent) -> None:
        """Buffer normal queue-mode busy text and schedule a bounded flush."""
        store = self._text_debounce_store()
        state = store.get(session_key)

        if state is not None and not self._can_merge_text_debounce_events(state.event, event):
            # Preserve sender attribution in shared sessions. The current
            # buffer becomes the next pending turn; the new sender starts a
            # fresh debounce burst when the pending slot allows it.
            await self._flush_text_debounce_now(session_key)
            state = store.get(session_key)
            if state is not None and not self._can_merge_text_debounce_events(state.event, event):
                existing_pending = self._pending_messages.get(session_key)
                if existing_pending is not None and self._can_merge_text_debounce_events(existing_pending, event):
                    merge_pending_message_event(
                        self._pending_messages,
                        session_key,
                        event,
                        merge_text=True,
                    )
                return

        now = time.monotonic()
        if state is None:
            state = TextDebounceState(
                event=event,
                task=None,
                first_ts=now,
                last_ts=now,
            )
            store[session_key] = state
        else:
            if event.text:
                state.event.text = (
                    f"{state.event.text}\n{event.text}"
                    if state.event.text
                    else event.text
                )
            latest_message_id = getattr(event, "message_id", None)
            latest_anchor = latest_message_id or getattr(event, "reply_to_message_id", None)
            if latest_message_id is not None:
                state.event.message_id = str(latest_message_id)
            if latest_anchor is not None and hasattr(state.event, "reply_to_message_id"):
                state.event.reply_to_message_id = str(latest_anchor)
            state.last_ts = now

        if state.task is not None and not state.task.done():
            state.task.cancel()

        delay = self._text_debounce_delay(session_key)
        state.task = asyncio.create_task(self._flush_text_debounce(session_key, delay))

    async def _flush_text_debounce(self, session_key: str, delay: float) -> None:
        """Timer task that flushes the debounced text buffer."""
        try:
            await asyncio.sleep(delay)
            await self._flush_text_debounce_now(session_key)
        except asyncio.CancelledError:
            return
        finally:
            current = asyncio.current_task()
            state = self._text_debounce_store().get(session_key)
            if state is not None and state.task is current:
                state.task = None

    async def _flush_text_debounce_now(self, session_key: str) -> bool:
        """Force-flush one debounced busy-text burst into the pending slot."""
        store = self._text_debounce_store()
        state = store.get(session_key)
        if state is None:
            return False

        current = asyncio.current_task()
        if state.task is not None and state.task is not current and not state.task.done():
            state.task.cancel()
        state.task = None

        existing_pending = self._pending_messages.get(session_key)
        if (
            existing_pending is not None
            and not self._can_merge_text_debounce_events(existing_pending, state.event)
        ):
            return False

        state = store.pop(session_key, None)
        if state is None:
            return False
        merge_pending_message_event(
            self._pending_messages,
            session_key,
            state.event,
            merge_text=True,
        )
        return True

    def _discard_text_debounce(self, session_key: str) -> None:
        """Cancel and drop pending text debounce state for control commands."""
        state = self._text_debounce_store().pop(session_key, None)
        if state is not None and state.task is not None and not state.task.done():
            state.task.cancel()

    # ------------------------------------------------------------------
    # Session task + guard ownership helpers
    # ------------------------------------------------------------------
    # These were introduced together with the _session_tasks owner map to
    # make session lifecycle reconciliation deterministic across (a) the
    # normal completion path, (b) /stop/ /new/ /reset bypass commands,
    # and (c) stale-lock self-heal on the next inbound message.

    def _release_session_guard(
        self,
        session_key: str,
        *,
        guard: Optional[asyncio.Event] = None,
    ) -> None:
        """Release the adapter-level guard for a session.

        When ``guard`` is provided, only release the entry if it still points
        at that exact Event.  This lets reset-like commands swap in a temporary
        guard while the old processing task unwinds, without having the old
        task's cleanup accidentally clear the replacement guard.
        """
        current_guard = self._active_sessions.get(session_key)
        if current_guard is None:
            return
        if guard is not None and current_guard is not guard:
            return
        del self._active_sessions[session_key]

    def _session_task_is_stale(self, session_key: str) -> bool:
        """Return True if the owner task for ``session_key`` is done/cancelled.

        A lock is "stale" when the adapter still has ``_active_sessions[key]``
        AND a known owner task in ``_session_tasks`` that has already exited.
        When there is no owner task at all, that usually means the guard was
        installed by some path other than handle_message() (tests sometimes
        install guards directly) — don't treat that as stale.  The on-entry
        self-heal only needs to handle the production split-brain case where
        an owner task was recorded, then exited without clearing its guard.
        """
        task = self._session_tasks.get(session_key)
        if task is None:
            return False
        done = getattr(task, "done", None)
        return bool(done and done())

    def _heal_stale_session_lock(self, session_key: str) -> bool:
        """Clear a stale session lock if the owner task is already gone.

        Returns True if a stale lock was healed.  Returns False if there is
        no lock, or the owner task is still alive (the normal busy case).

        This is the on-entry safety net sidbin's issue #11016 analysis calls
        for: without it, a split-brain — adapter still thinks the session is
        active, but nothing is actually processing — traps the chat in
        infinite "Interrupting current task..." until the gateway is
        restarted.
        """
        if session_key not in self._active_sessions:
            return False
        if not self._session_task_is_stale(session_key):
            return False
        logger.warning(
            "[%s] Healing stale session lock for %s (owner task is done/absent)",
            self.name,
            session_key,
        )
        self._active_sessions.pop(session_key, None)
        self._pending_messages.pop(session_key, None)
        self._session_tasks.pop(session_key, None)
        self._discard_text_debounce(session_key)
        return True

    def _start_session_processing(
        self,
        event: MessageEvent,
        session_key: str,
        *,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> bool:
        """Spawn a background processing task under the given session guard.

        Returns True on success.  If the runtime stubs ``create_task`` with a
        non-Task sentinel (some tests do this), the guard is rolled back and
        False is returned so the caller isn't left holding a half-installed
        session lock.
        """
        guard = interrupt_event or asyncio.Event()
        self._active_sessions[session_key] = guard

        task = asyncio.create_task(self._process_message_background(event, session_key))
        self._session_tasks[session_key] = task
        try:
            self._background_tasks.add(task)
        except TypeError:
            # Tests stub create_task() with lightweight sentinels that are not
            # hashable and do not support lifecycle callbacks.
            self._session_tasks.pop(session_key, None)
            self._release_session_guard(session_key, guard=guard)
            return False
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)
            task.add_done_callback(self._expected_cancelled_tasks.discard)
        return True

    async def cancel_session_processing(
        self,
        session_key: str,
        *,
        release_guard: bool = True,
        discard_pending: bool = True,
    ) -> None:
        """Cancel in-flight processing for a single session.

        ``release_guard=False`` keeps the adapter-level session guard in place
        so reset-like commands can finish atomically before follow-up messages
        are allowed to start a fresh background task.

        Bounded by a 5s timeout so a wedged finally block in the cancelled
        task (typing-task cleanup, on_processing_complete hook, etc.) can't
        stall the calling dispatch coroutine — particularly under pytest-
        asyncio where the event loop's cancellation-propagation semantics
        differ subtly from a bare ``asyncio.run`` harness.
        """
        task = self._session_tasks.pop(session_key, None)
        if task is not None and not task.done():
            logger.debug(
                "[%s] Cancelling active processing for session %s",
                self.name,
                session_key,
            )
            self._expected_cancelled_tasks.add(task)
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] Cancelled task for %s did not exit within 5s; "
                    "unblocking dispatch and letting the task unwind in the background",
                    self.name, session_key,
                )
            except Exception:
                logger.debug(
                    "[%s] Session cancellation raised while unwinding %s",
                    self.name,
                    session_key,
                    exc_info=True,
                )
        if discard_pending:
            self._pending_messages.pop(session_key, None)
            self._discard_text_debounce(session_key)
        if release_guard:
            self._release_session_guard(session_key)

    async def _drain_pending_after_session_command(
        self,
        session_key: str,
        command_guard: asyncio.Event,
    ) -> None:
        """Resume the latest queued follow-up once a session command completes.

        Called at the tail of /stop, /new, and /reset dispatch.  Releases the
        command-scoped guard, then — if a follow-up message landed while the
        command was running — spawns a fresh processing task for it.
        """
        await self._flush_text_debounce_now(session_key)
        pending_event = self._pending_messages.pop(session_key, None)
        self._release_session_guard(session_key, guard=command_guard)
        if pending_event is None:
            return
        self._start_session_processing(pending_event, session_key)

    async def _dispatch_active_session_command(
        self,
        event: MessageEvent,
        session_key: str,
        cmd: str,
    ) -> None:
        """Dispatch a reset-like bypass command while preserving guard ordering.

        /stop, /new, and /reset must:
          1. Keep the session guard installed while the runner processes the
             command (so a racing follow-up message stays queued, not
             dispatched as a second parallel run).
          2. Cancel the old in-flight adapter task only AFTER the runner has
             finished handling the command (so the runner sees consistent
             state and its response is sent in order).
          3. Release the command-scoped guard and drain the latest queued
             follow-up exactly once, after 1 and 2 complete.
        """
        logger.debug(
            "[%s] Command '/%s' bypassing active-session guard for %s",
            self.name,
            cmd,
            session_key,
        )

        current_guard = self._active_sessions.get(session_key)
        command_guard = asyncio.Event()
        self._active_sessions[session_key] = command_guard
        thread_meta = _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))

        try:
            response = await self._message_handler(event)
            _text = self._unwrap_ephemeral(response)
            # Send the response BEFORE cancelling the old task so the send
            # cannot be affected by task-cancellation side effects (race
            # condition fix — issue #18912).  Previously the send happened
            # after cancel_session_processing, which could silently drop the
            # "/new" confirmation when an agent was actively running.
            if _text:
                logger.info(
                    "[%s] Sending command '/%s' response (%d chars) to %s",
                    self.name,
                    cmd,
                    len(_text),
                    event.source.chat_id,
                )
                await self._send_with_retry(
                    chat_id=event.source.chat_id,
                    content=_text,
                    reply_to=_reply_anchor_for_event(event),
                    metadata=_mark_notify_metadata(thread_meta),
                )
            # Old adapter task (if any) is cancelled AFTER the response has
            # been sent — keeps ordering deterministic and avoids the race.
            await self.cancel_session_processing(
                session_key,
                release_guard=False,
                discard_pending=False,
            )
        except Exception:
            # On failure, restore the original guard if one still exists so
            # we don't leave the session in a half-reset state.
            if self._active_sessions.get(session_key) is command_guard:
                if session_key in self._session_tasks and current_guard is not None:
                    self._active_sessions[session_key] = current_guard
                else:
                    self._release_session_guard(session_key, guard=command_guard)
            raise

        await self._drain_pending_after_session_command(session_key, command_guard)

    async def handle_message(self, event: MessageEvent) -> None:
        """
        Process an incoming message.
        
        This method returns quickly by spawning background tasks.
        This allows new messages to be processed even while an agent is running,
        enabling interruption support.
        """
        if not self._message_handler:
            return

        if event.allow_gateway_control:
            coerce_plaintext_gateway_command(event)

        session_key = build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
        expected_session_key = str(
            (event.metadata or {}).get("gateway_session_key") or ""
        ).strip()
        if expected_session_key and session_key != expected_session_key:
            logger.warning(
                "Dropping internally routed event: expected session=%s derived=%s",
                expected_session_key,
                session_key,
            )
            return

        # On-entry self-heal: if the adapter still has an _active_sessions
        # entry for this key but the owner task has already exited (done or
        # cancelled), the lock is stale.  Clear it and fall through to
        # normal dispatch so the user isn't trapped behind a dead guard —
        # this is the split-brain tail described in issue #11016.
        if session_key in self._active_sessions:
            self._heal_stale_session_lock(session_key)

        # Check if there's already an active handler for this session
        if session_key in self._active_sessions:
            # Certain commands must bypass the active-session guard and be
            # dispatched directly to the gateway runner.  Without this, they
            # are queued as pending messages and either:
            #   - leak into the conversation as user text (/stop, /new), or
            #   - deadlock (/approve, /deny — agent is blocked on Event.wait)
            #
            # Dispatch inline: call the message handler directly and send the
            # response.  Do NOT use _process_message_background — it manages
            # session lifecycle and its cleanup races with the running task
            # (see PR #4926).
            cmd = event.get_command()
            from son_of_anton_cli.commands import (
                is_interrupt_then_dispatch,
                should_bypass_active_session,
            )

            if should_bypass_active_session(cmd):
                # /stop, /new, /reset must cancel the in-flight adapter task
                # and preserve ordering of queued follow-ups.  Route those
                # through the dedicated handoff path that serializes
                # cancellation + runner response + pending drain.
                # (Registry-derived: busy_policy == "interrupt_then_dispatch".)
                if cmd and is_interrupt_then_dispatch(cmd):
                    self._discard_text_debounce(session_key)
                    try:
                        await self._dispatch_active_session_command(event, session_key, cmd)
                    except Exception as e:
                        logger.error(
                            "[%s] Command '/%s' dispatch failed: %s",
                            self.name, cmd, e, exc_info=True,
                        )
                    return

                # Other bypass commands (/approve, /deny, /status,
                # /background, /restart) just need direct dispatch — they
                # don't cancel the running task.
                logger.debug(
                    "[%s] Command '/%s' bypassing active-session guard for %s",
                    self.name, cmd, session_key,
                )
                try:
                    _thread_meta = _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))
                    response = await self._message_handler(event)
                    _text = self._unwrap_ephemeral(response)
                    if _text:
                        await self._send_with_retry(
                            chat_id=event.source.chat_id,
                            content=_text,
                            reply_to=_reply_anchor_for_event(event),
                            metadata=_mark_notify_metadata(_thread_meta),
                        )
                except Exception as e:
                    logger.error("[%s] Command '/%s' dispatch failed: %s", self.name, cmd, e, exc_info=True)
                return

            # Clarify reply bypass: if the agent is blocked on a
            # clarify_tool call, the next non-command message in this
            # session MUST reach the runner so typed numeric choices,
            # exact choices, and free-form "Other" answers can resolve the
            # clarify-intercept and unblock the agent.
            #
            # Without this bypass: the message gets queued in
            # _pending_messages as a follow-up turn instead of reaching the
            # clarify resolver, leaving the agent blocked and discarding the
            # user's answer.
            # Same shape as the /approve deadlock fix (PR #4926) — both
            # cases are "agent thread blocked on Event.wait, message must
            # reach the resolver before being treated as a new turn."
            if not cmd and event.allow_gateway_control:
                try:
                    from tools import clarify_gateway as _clarify_mod
                    _has_text_clarify = (
                        _clarify_mod.get_pending_for_session(
                            session_key,
                            include_choice_prompts=True,
                        ) is not None
                    )
                except Exception:
                    _has_text_clarify = False

                if _has_text_clarify:
                    logger.debug(
                        "[%s] Routing message to clarify text-intercept for %s",
                        self.name, session_key,
                    )
                    try:
                        _thread_meta = _thread_metadata_for_source(
                            event.source, _reply_anchor_for_event(event)
                        )
                        response = await self._message_handler(event)
                        _text = self._unwrap_ephemeral(response)
                        if _text:
                            await self._send_with_retry(
                                chat_id=event.source.chat_id,
                                content=_text,
                                reply_to=_reply_anchor_for_event(event),
                                metadata=_mark_notify_metadata(_thread_meta),
                            )
                    except Exception as e:
                        logger.error(
                            "[%s] Clarify text-intercept dispatch failed: %s",
                            self.name, e, exc_info=True,
                        )
                    return

            if self._busy_session_handler is not None:
                try:
                    if await self._busy_session_handler(event, session_key):
                        return
                except Exception as e:
                    logger.error("[%s] Busy-session handler failed: %s", self.name, e, exc_info=True)

            # Special case: photo bursts/albums frequently arrive as multiple near-
            # simultaneous messages. Queue them without interrupting the active run,
            # then process them immediately after the current task finishes.
            if event.message_type == MessageType.PHOTO:
                logger.debug("[%s] Queuing photo follow-up for session %s without interrupt", self.name, session_key)
                merge_pending_message_event(self._pending_messages, session_key, event)
                return  # Don't interrupt now - will run after current task completes

            if self._is_queue_text_debounce_candidate(event):
                logger.debug(
                    "[%s] New text message while session %s is active — "
                    "debouncing follow-up (window=%.2fs)",
                    self.name,
                    session_key,
                    self._busy_text_debounce_seconds,
                )
                await self._queue_text_debounce(session_key, event)
            else:
                logger.debug(
                    "[%s] New message while session %s is active — queuing follow-up "
                    "(no interrupt, will cascade after current turn)",
                    self.name,
                    session_key,
                )
                merge_pending_message_event(
                    self._pending_messages,
                    session_key,
                    event,
                    merge_text=event.message_type == MessageType.TEXT,
                )
            return  # Don't process now - will be handled after current task finishes
        
        # Mark session as active BEFORE spawning background task to close
        # the race window where a second message arriving before the task
        # starts would also pass the _active_sessions check and spawn a
        # duplicate task.  (grammY sequentialize / aiogram EventIsolation
        # pattern — set the guard synchronously, not inside the task.)
        # _start_session_processing installs the guard AND the owner-task
        # mapping atomically so stale-lock detection works.
        self._start_session_processing(event, session_key)
    
    @staticmethod
    def _get_human_delay() -> float:
        """
        Return a random delay in seconds for human-like response pacing.

        Reads from env vars:
          SON_OF_ANTON_HUMAN_DELAY_MODE: "off" (default) | "natural" | "custom"
          SON_OF_ANTON_HUMAN_DELAY_MIN_MS: minimum delay in ms (default 800, custom mode)
          SON_OF_ANTON_HUMAN_DELAY_MAX_MS: maximum delay in ms (default 2500, custom mode)
        """
        mode = os.getenv("SON_OF_ANTON_HUMAN_DELAY_MODE", "off").lower()
        if mode == "off":
            return 0.0
        if mode == "natural":
            min_ms, max_ms = 800, 2500
            return random.uniform(min_ms / 1000.0, max_ms / 1000.0)
        # custom mode — tolerate malformed env vars instead of crashing.
        try:
            min_ms = int(os.getenv("SON_OF_ANTON_HUMAN_DELAY_MIN_MS", "800"))
        except (TypeError, ValueError):
            min_ms = 800
        try:
            max_ms = int(os.getenv("SON_OF_ANTON_HUMAN_DELAY_MAX_MS", "2500"))
        except (TypeError, ValueError):
            max_ms = 2500
        return random.uniform(min_ms / 1000.0, max_ms / 1000.0)

    async def _process_message_background(self, event: MessageEvent, session_key: str) -> None:
        """Background task that actually processes the message."""
        # Track delivery outcomes for the processing-complete hook
        delivery_attempted = False
        delivery_succeeded = False

        def _record_delivery(result):
            nonlocal delivery_attempted, delivery_succeeded
            if result is None:
                return
            delivery_attempted = True
            if getattr(result, "success", False):
                delivery_succeeded = True

        # Reuse the interrupt event set by handle_message() (which marks
        # the session active before spawning this task to prevent races).
        # Fall back to a new Event only if the entry was removed externally.
        interrupt_event = self._active_sessions.get(session_key) or asyncio.Event()
        self._active_sessions[session_key] = interrupt_event
        
        # Start continuous typing indicator (refreshes every 2 seconds).
        # Gated per-platform: when typing_indicator=False the refresh loop is
        # never spawned, so no "typing…" / "is thinking…" status is shown.
        # typing_task stays None; _stop_typing_refresh already no-ops on None.
        _thread_metadata = _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))
        typing_task: Optional[asyncio.Task] = None
        if getattr(self.config, "typing_indicator", True):
            _keep_typing_kwargs: Dict[str, Any] = {"metadata": _thread_metadata}
            try:
                _keep_typing_sig = inspect.signature(self._keep_typing)
            except (TypeError, ValueError):
                _keep_typing_sig = None
            if _keep_typing_sig is None or "stop_event" in _keep_typing_sig.parameters:
                _keep_typing_kwargs["stop_event"] = interrupt_event
            typing_task = asyncio.create_task(
                self._keep_typing(
                    event.source.chat_id,
                    **_keep_typing_kwargs,
                )
            )

        async def _stop_typing_task() -> None:
            await self._stop_typing_refresh(
                event.source.chat_id,
                typing_task,
                metadata=_thread_metadata,
            )
        
        try:
            await self._run_processing_hook("on_processing_start", event)

            # Call the handler (this can take a while with tool calls)
            response = await self._message_handler(event)
            is_ephemeral_response = isinstance(response, EphemeralReply)

            # Slash-command handlers may return an EphemeralReply system
            # notice. Unwrap here so all the downstream extract_media /
            # text-processing logic sees a plain string.
            response = self._unwrap_ephemeral(response)

            # Send response if any.  A None/empty response is normal when
            # streaming already delivered the text (already_sent=True) or
            # when the message was queued behind an active agent.  Log at
            # DEBUG to avoid noisy warnings for expected behavior.
            #
            # Suppress stale response when the session was interrupted by a
            # new message that hasn't been consumed yet.  The pending message
            # is processed by the pending-message handler below (#8221/#2483).
            if (
                response
                and interrupt_event.is_set()
                and session_key in self._pending_messages
            ):
                logger.info(
                    "[%s] Suppressing stale response for interrupted session %s",
                    self.name,
                    session_key,
                )
                response = None
            if not response:
                logger.debug("[%s] Handler returned empty/None response for %s", self.name, event.source.chat_id)
            if response:
                # Capture [[as_document]] before extract_media strips it, so the
                # dispatch partition below can route image-extension files
                # through send_document instead of send_multiple_images. Used
                # by turns that produce large/lossless images (e.g. info-graphs)
                # where the photo path's recompression destroys legibility.
                force_document_attachments = "[[as_document]]" in response

                # Pre-extract snapshot for the #29346 recovery/invariant below.
                _response_pre_extract = response

                # Extract MEDIA:<path> tags (from TTS tool) before other processing
                media_files, response = self.extract_media(response)
                media_files = self.filter_media_delivery_paths(media_files)

                # Extract image URLs and send them as native platform attachments
                images, text_content = self.extract_images(response)
                # Strip any remaining internal directives from message body (fixes #1561).
                # _strip_media_directives shares MEDIA_TAG_CLEANUP_RE, so a MEDIA: tag
                # with an unknown extension is intentionally left in the body for
                # extract_local_files below to pick up rather than silently dropped (#34517).
                text_content = _strip_media_directives(text_content).strip()
                if images:
                    logger.info("[%s] extract_images found %d image(s) in response (%d chars)", self.name, len(images), len(response))

                local_files = []
                if not is_ephemeral_response:
                    # Auto-detect bare local file paths for native media delivery
                    # (helps small models that don't use MEDIA: syntax). Skip
                    # system/command notices so config paths stay visible text
                    # instead of becoming native uploads.
                    local_files, text_content = self.extract_local_files(text_content)
                    local_files = self.filter_local_delivery_paths(local_files)
                    # Do NOT load the full SQLite transcript for ordinary text or
                    # explicit MEDIA tags.  History is needed only for bare local
                    # paths auto-detected above.  Run that synchronous DB/decode
                    # work off the platform event loop so a slow state.db read
                    # cannot block Discord heartbeats and trigger the liveness
                    # watchdog.  On lookup failure the helper returns None and we
                    # fail open by delivering the candidate file.
                    _history_media_paths = None
                    if local_files:
                        _history_media_paths = (
                            await self._bounded_history_media_paths_for_session(
                                session_key
                            )
                        )
                    if _history_media_paths:
                        _suppressed = [p for p in local_files if p in _history_media_paths]
                        if _suppressed:
                            # Log the suppression (#73771) — silent drops here
                            # cost operators hours of log-diving.
                            logger.info(
                                "[%s] Suppressing %d bare local file path(s) already "
                                "delivered in this session: %s",
                                self.name, len(_suppressed), _suppressed,
                            )
                        local_files = [p for p in local_files if p not in _history_media_paths]
                    if local_files:
                        logger.info("[%s] extract_local_files found %d file(s) in response", self.name, len(local_files))

                # A2 (#29346): extraction can reduce a non-empty response to
                # empty text with no attachment, and the `if text_content` guard
                # below then drops it silently. Recover on every platform (#33842
                # was Discord-only); the guard avoids duplicating an attachment.
                if not (text_content or images or local_files or media_files):
                    # Recover from the post-extract_media `response`, not the raw
                    # snapshot: extract_media already stripped MEDIA (incl. spaced
                    # paths) with its full grammar, so no fragment can leak.
                    _recovered = _strip_media_directives(response).strip()
                    if _recovered:
                        logger.warning(
                            "[%s] response_delivery_recovered: extract pipeline "
                            "reduced a non-empty response (%d chars) to empty with "
                            "no attachment; delivering recovered original to %s",
                            self.name, len(_response_pre_extract), event.source.chat_id,
                        )
                        text_content = _recovered

                # Final user-visible content (text, TTS, media, files) gets
                # the existing notify=True marker. Clone once so typing/status
                # metadata stays unmarked and progress bubbles remain
                # thread-strict.
                _final_thread_metadata = _mark_notify_metadata(_thread_metadata)

                # Send the text portion.
                if text_content:
                    delivery_adapter = self
                    logger.info(
                        "[%s] Sending response (%d chars) to %s",
                        delivery_adapter.name,
                        len(text_content),
                        event.source.chat_id,
                    )
                    _reply_anchor = _reply_anchor_for_event(event)
                    # Delivery-obligation ledger: durably record the final
                    # response BEFORE the send attempt so a gateway crash
                    # between finalize and platform ACK can redeliver it on
                    # the next boot instead of silently losing the turn's
                    # output (#58818). Best-effort at every step — ledger
                    # trouble must never block or delay the actual send.
                    # Slash-command and ephemeral replies are cheap to
                    # regenerate and are not recorded.
                    _obligation_id = None
                    if not is_ephemeral_response and not str(
                        event.text or ""
                    ).lstrip().startswith(("/", self.typed_command_prefix or "!")):
                        try:
                            from gateway.delivery_ledger import (
                                compute_obligation_id,
                                ledger_enabled,
                                mark_attempting,
                                record_obligation,
                            )

                            if await asyncio.to_thread(ledger_enabled):
                                _obligation_id = compute_obligation_id(
                                    session_key,
                                    str(getattr(event, "message_id", "") or ""),
                                    text_content,
                                )
                                await asyncio.to_thread(
                                    record_obligation,
                                    obligation_id=_obligation_id,
                                    session_key=session_key,
                                    platform=str(
                                        getattr(event.source.platform, "value",
                                                event.source.platform)
                                    ),
                                    chat_id=event.source.chat_id,
                                    thread_id=getattr(event.source, "thread_id", None),
                                    content=text_content,
                                )
                                await asyncio.to_thread(mark_attempting, _obligation_id)
                        except Exception:
                            logger.debug("delivery ledger record failed", exc_info=True)
                            _obligation_id = None
                    result = await delivery_adapter._send_with_retry(
                        chat_id=event.source.chat_id,
                        content=text_content,
                        reply_to=_reply_anchor,
                        metadata=_final_thread_metadata,
                    )
                    _record_delivery(result)
                    if _obligation_id is not None:
                        try:
                            from gateway.delivery_ledger import (
                                mark_delivered,
                                mark_failed,
                            )

                            if getattr(result, "success", False):
                                await asyncio.to_thread(mark_delivered, _obligation_id)
                            else:
                                await asyncio.to_thread(
                                    mark_failed,
                                    _obligation_id,
                                    str(getattr(result, "error", "") or ""),
                                )
                        except Exception:
                            logger.debug(
                                "delivery ledger update failed", exc_info=True
                            )

                # Human-like pacing delay between text and media
                human_delay = self._get_human_delay()

                # Send extracted images as native attachments
                if images:
                    logger.info("[%s] Extracted %d image(s) to send as attachments", self.name, len(images))
                    try:
                        await self.send_multiple_images(
                            chat_id=event.source.chat_id,
                            images=images,
                            metadata=_final_thread_metadata,
                            human_delay=human_delay,
                        )
                    except Exception as batch_err:
                        logger.warning("[%s] Error batching images: %s", self.name, batch_err, exc_info=True)


                # Send extracted media files — route by file type
                _VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.3gp'}
                _IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}

                # Partition images out of media_files + local_files so they
                # can be sent as a single batch (Signal RPC). When
                # ``[[as_document]]`` was set on the original response, image
                # files skip the photo path and route to send_document below
                # so they're delivered with original bytes (no photo
                # recompression).
                from urllib.parse import quote as _quote
                _image_paths: list = []
                _non_image_media: list = []
                for media_path, is_voice in media_files:
                    _ext = Path(media_path).suffix.lower()
                    if (_ext in _IMAGE_EXTS
                            and not is_voice
                            and not force_document_attachments):
                        _image_paths.append(media_path)
                    else:
                        _non_image_media.append((media_path, is_voice))
                _non_image_local: list = []
                for file_path in local_files:
                    if (Path(file_path).suffix.lower() in _IMAGE_EXTS
                            and not force_document_attachments):
                        _image_paths.append(file_path)
                    else:
                        _non_image_local.append(file_path)

                if _image_paths:
                    try:
                        _batch = [(f"file://{_quote(p)}", "") for p in _image_paths]
                        await self.send_multiple_images(
                            chat_id=event.source.chat_id,
                            images=_batch,
                            metadata=_final_thread_metadata,
                            human_delay=human_delay,
                        )
                    except Exception as batch_err:
                        logger.warning("[%s] Error batching images: %s", self.name, batch_err, exc_info=True)

                if _non_image_media:
                    logger.info(
                        "[%s] Delivering %d non-image MEDIA attachment(s)",
                        self.name,
                        len(_non_image_media),
                    )
                for media_path, is_voice in _non_image_media:
                    if human_delay > 0:
                        await asyncio.sleep(human_delay)
                    try:
                        ext = Path(media_path).suffix.lower()
                        if should_send_media_as_audio(self.platform, ext, is_voice=is_voice):
                            media_result = await self.send_voice(
                                chat_id=event.source.chat_id,
                                audio_path=media_path,
                                metadata=_final_thread_metadata,
                            )
                        elif ext in _VIDEO_EXTS:
                            logger.info(
                                "[%s] Sending video attachment (%s) to %s",
                                self.name,
                                ext,
                                event.source.chat_id,
                            )
                            media_result = await self.send_video(
                                chat_id=event.source.chat_id,
                                video_path=media_path,
                                metadata=_final_thread_metadata,
                            )
                        else:
                            media_result = await self.send_document(
                                chat_id=event.source.chat_id,
                                file_path=media_path,
                                metadata=_final_thread_metadata,
                            )

                        if not media_result.success:
                            logger.warning("[%s] Failed to send media (%s): %s", self.name, ext, media_result.error)
                            await self._notify_media_delivery_failure(
                                event.source.chat_id,
                                media_path,
                                is_voice=is_voice,
                                metadata=_final_thread_metadata,
                            )
                    except Exception as media_err:
                        logger.warning("[%s] Error sending media: %s", self.name, media_err)

                # Send auto-detected local non-image files as native attachments
                for file_path in _non_image_local:
                    if human_delay > 0:
                        await asyncio.sleep(human_delay)
                    try:
                        ext = Path(file_path).suffix.lower()
                        if ext in _VIDEO_EXTS:
                            file_result = await self.send_video(
                                chat_id=event.source.chat_id,
                                video_path=file_path,
                                metadata=_final_thread_metadata,
                            )
                        else:
                            file_result = await self.send_document(
                                chat_id=event.source.chat_id,
                                file_path=file_path,
                                metadata=_final_thread_metadata,
                            )
                        if not file_result.success:
                            logger.warning(
                                "[%s] Failed to send local file (%s): %s",
                                self.name,
                                ext,
                                file_result.error,
                            )
                            await self._notify_media_delivery_failure(
                                event.source.chat_id,
                                file_path,
                                metadata=_final_thread_metadata,
                            )
                    except Exception as file_err:
                        logger.error("[%s] Error sending local file %s: %s", self.name, file_path, file_err)

                # A3 (#29346): if a non-empty response produced nothing
                # deliverable, fail loudly rather than dropping it in silence.
                _anything_delivered = (
                    delivery_attempted
                    or images or local_files or media_files
                )
                if not _anything_delivered and _response_pre_extract.strip():
                    logger.error(
                        "[%s] response_delivery_dropped: non-empty response "
                        "(%d chars) produced no delivered message or attachment "
                        "for %s (empty after extract, recovery yielded nothing).",
                        self.name, len(_response_pre_extract), event.source.chat_id,
                    )

            # Determine overall success for the processing hook
            processing_ok = delivery_succeeded if delivery_attempted else not bool(response)
            await self._run_processing_hook(
                "on_processing_complete",
                event,
                ProcessingOutcome.SUCCESS if processing_ok else ProcessingOutcome.FAILURE,
            )

            # The active drain owns debounce state. If a queue-mode timer has
            # not fired yet, force-flush into _pending_messages here and let
            # this task hand off the follow-up.
            await self._flush_text_debounce_now(session_key)

            # Check if there's a pending message that was queued during our processing
            if session_key in self._pending_messages:
                pending_event = self._pending_messages.pop(session_key)
                logger.debug("[%s] Processing queued follow-up message", self.name)
                # Keep the _active_sessions entry live across the turn chain
                # and only CLEAR the interrupt Event — do NOT delete the entry.
                # If we deleted here, a concurrent inbound message arriving
                # during the awaits below would pass the Level-1 guard, spawn
                # its own _process_message_background, and run simultaneously
                # with the recursive drain below.  Two agents on one
                # session_key = duplicate responses, duplicate tool calls.
                # Clearing the Event keeps the guard live so follow-ups take
                # the busy-handler path as intended.
                _active = self._active_sessions.get(session_key)
                if _active is not None:
                    _active.clear()
                await _stop_typing_task()
                # Spawn a fresh task for the pending message instead of
                # recursing.  Issue #17758: `await
                # self._process_message_background(...)` here grew the
                # call stack one frame per chained follow-up, and under
                # sustained pending-queue activity the C stack would
                # exhaust at ~2000 frames and SIGSEGV the process.
                # Mirror the late-arrival drain pattern below: hand off
                # to a new task and return so this frame can unwind.
                drain_task = asyncio.create_task(
                    self._process_message_background(pending_event, session_key)
                )
                # Hand ownership of the session to the drain task so
                # stale-lock detection keeps working while it runs.
                self._session_tasks[session_key] = drain_task
                try:
                    self._background_tasks.add(drain_task)
                    drain_task.add_done_callback(self._background_tasks.discard)
                except TypeError:
                    # Tests stub create_task() with non-hashable sentinels; tolerate.
                    pass
                return  # Drain task owns the session now.
                
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            outcome = ProcessingOutcome.CANCELLED
            if current_task is None or current_task not in self._expected_cancelled_tasks:
                outcome = ProcessingOutcome.FAILURE
            await self._run_processing_hook("on_processing_complete", event, outcome)
            raise
        except BaseException as e:
            await self._run_processing_hook("on_processing_complete", event, ProcessingOutcome.FAILURE)
            logger.error("[%s] Error handling message: %s", self.name, e, exc_info=True)
            # Send the error to the user so they aren't left with radio silence
            try:
                error_type = type(e).__name__
                error_detail = str(e)[:300] if str(e) else "no details available"
                _thread_metadata = _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))
                await self.send(
                    chat_id=event.source.chat_id,
                    content=(
                        f"Sorry, I encountered an error ({error_type}).\n"
                        f"{error_detail}\n"
                        "Try again or use /reset to start a fresh session."
                    ),
                    metadata=_thread_metadata,
                )
            except Exception as notify_err:
                logger.error(
                    "[%s] Failed to send error notification to user: %s",
                    self.name, notify_err, exc_info=True,
                )  # Last resort — don't let error reporting crash the handler
            # Preserve shutdown semantics: SystemExit/KeyboardInterrupt must
            # still propagate after the user-facing failure notification, so
            # the loop's own signal handling can shut down cleanly. Other
            # BaseExceptions (e.g. GeneratorExit) are contained like ordinary
            # failures — this handler is fire-and-forget, so swallowing them
            # here prevents "Task exception was never retrieved" radio silence.
            if isinstance(e, (SystemExit, KeyboardInterrupt)):
                raise
        finally:
            # Stop typing before any deferred callback work.  Post-delivery
            # callbacks may perform platform I/O; a stuck callback must not
            # leave the typing refresh task running indefinitely.
            await _stop_typing_task()
            # Fire any one-shot post-delivery callback registered for this
            # session (e.g. deferred background-review notifications).
            #
            # Snapshot the callback generation HERE (after the agent has run),
            # not at the top of this task.  _son_of_anton_run_generation is set on
            # the interrupt event by GatewayRunner._bind_adapter_run_generation
            # during _handle_message_with_agent — which happens DURING the
            # self._message_handler(event) await above.  Snapshotting earlier
            # always captured None, which bypassed the generation-ownership
            # check in pop_post_delivery_callback and let stale runs fire a
            # fresher run's callbacks.
            _callback_generation = getattr(
                interrupt_event,
                "_son_of_anton_run_generation",
                None,
            )
            if hasattr(self, "pop_post_delivery_callback"):
                _post_cb = self.pop_post_delivery_callback(
                    session_key,
                    generation=_callback_generation,
                )
            else:
                _post_cb = getattr(self, "_post_delivery_callbacks", {}).pop(session_key, None)
            if callable(_post_cb):
                try:
                    _post_result = _post_cb()
                    if inspect.isawaitable(_post_result):
                        await asyncio.wait_for(
                            _post_result,
                            timeout=_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS,
                        )
                except (asyncio.TimeoutError, Exception):
                    pass
            # Some adapters keep platform-level typing tasks.  If callback
            # work or a late refresh recreated one, make one final bounded stop
            # before releasing the session guard.
            await self._stop_typing_refresh(
                event.source.chat_id,
                None,
                metadata=_thread_metadata,
                stop_attempts=1,
            )
            # Final drain/release boundary: force-flush any timer that missed
            # the in-band drain before deciding whether the guard can clear.
            await self._flush_text_debounce_now(session_key)
            # Late-arrival drain: a message may have arrived during the
            # cleanup awaits above (typing_task cancel, stop_typing).  Such
            # messages passed the Level-1 guard (entry still live, Event
            # possibly set) and landed in _pending_messages via the
            # busy-handler path.  Without this block, we would delete the
            # active-session entry and the queued message would be silently
            # dropped (user never gets a reply).
            late_pending = self._pending_messages.pop(session_key, None)
            if late_pending is not None:
                current_task = asyncio.current_task()
                existing_task = self._session_tasks.get(session_key)
                if (
                    existing_task is not None
                    and existing_task is not current_task
                ):
                    # The in-band drain (or an earlier late-arrival drain)
                    # already spawned a follow-up task that owns this
                    # session.  Re-queue the late-arrival event so that
                    # task picks it up — avoids spawning two concurrent
                    # _process_message_background tasks for the same key
                    # (#17758 follow-up: prevents the create_task path
                    # from racing with itself across the in-band/finally
                    # boundary).
                    self._pending_messages[session_key] = late_pending
                else:
                    logger.debug(
                        "[%s] Late-arrival pending message during cleanup — spawning drain task",
                        self.name,
                    )
                    _active = self._active_sessions.get(session_key)
                    if _active is not None:
                        _active.clear()
                    drain_task = asyncio.create_task(
                        self._process_message_background(late_pending, session_key)
                    )
                    # Hand ownership of the session to the drain task so stale-lock
                    # detection keeps working while it runs.
                    self._session_tasks[session_key] = drain_task
                    try:
                        self._background_tasks.add(drain_task)
                        drain_task.add_done_callback(self._background_tasks.discard)
                    except TypeError:
                        # Tests stub create_task() with non-hashable sentinels; tolerate.
                        pass
                # Leave _active_sessions[session_key] populated — the drain
                # task's own lifecycle will clean it up.
            else:
                # Clean up session tracking.  Guard-match both deletes so a
                # reset-like command that already swapped in its own
                # command_guard (and cancelled us) can't be accidentally
                # cleared by our unwind.  The command owns the session now.
                #
                # The owner-check also covers the in-band drain handoff
                # above: when we spawned a drain_task and transferred
                # ownership via ``_session_tasks[session_key] = drain_task``,
                # ``_session_tasks.get(session_key) is current_task`` is
                # False, so we leave _active_sessions populated.  Without
                # this guard, the drain task picks up the same
                # interrupt_event in its own _process_message_background
                # entry, _release_session_guard's guard-match succeeds,
                # and we'd delete the entry while the drain task is still
                # running — letting a concurrent inbound message pass
                # the Level-1 guard and spawn a second handler for the
                # same session.
                current_task = asyncio.current_task()
                if current_task is not None and self._session_tasks.get(session_key) is current_task:
                    self._cleanup_finished_session_task(session_key, interrupt_event)
    
    def _cleanup_finished_session_task(
        self, session_key: str, interrupt_event: Optional[asyncio.Event]
    ) -> None:
        """Release the session guard for a finished owner task, then drop its
        ``_session_tasks`` entry ONLY if the guard was actually released.

        Release-then-conditional-delete is the #48300 fix: when a concurrent
        path (reset/new command, drain handoff) swapped ``_active_sessions[key]``
        to a different guard, ``_release_session_guard`` skips on the guard
        mismatch and the lock stays installed. If we deleted ``_session_tasks``
        unconditionally (the old order), ``_session_task_is_stale`` would later
        see no owner task and report "not stale", so the orphaned guard would
        never be healed — a permanent session deadlock. Keeping the done-task
        entry when the guard survives lets the on-entry self-heal detect the
        stale lock and clear it on the next inbound message.
        """
        self._release_session_guard(session_key, guard=interrupt_event)
        if session_key not in self._active_sessions:
            self._session_tasks.pop(session_key, None)
    
    async def cancel_background_tasks(self) -> None:
        """Cancel any in-flight background message-processing tasks.

        Used during gateway shutdown/replacement so active sessions from the old
        process do not keep running after adapters are being torn down.

        Each cancelled task is awaited with a 5s bound so a wedged finally
        (typing-task cleanup, on_processing_complete hook) can't stall the
        whole shutdown path.  Stragglers are released from our tracking and
        allowed to finish unwinding on their own.
        """
        # Loop until no new tasks appear.  Without this, a message
        # arriving during the `await asyncio.gather` below would spawn
        # a fresh _process_message_background task (added to
        # self._background_tasks at line ~1668 via handle_message),
        # and the _background_tasks.clear() at the end of this method
        # would drop the reference — the task runs untracked against a
        # disconnecting adapter, logs send-failures, and may linger
        # until it completes on its own.  Retrying the drain until the
        # task set stabilizes closes the window.
        MAX_DRAIN_ROUNDS = 5
        for _ in range(MAX_DRAIN_ROUNDS):
            tasks = [task for task in self._background_tasks if not task.done()]
            if not tasks:
                break
            for task in tasks:
                self._expected_cancelled_tasks.add(task)
                task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(asyncio.shield(t) for t in tasks),
                        return_exceptions=True,
                    ),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] %d background task(s) did not exit within 5s; "
                    "releasing tracking and letting them unwind in the background",
                    self.name, len([t for t in tasks if not t.done()]),
                )
                break
            # Loop: late-arrival tasks spawned during the gather above
            # will be in self._background_tasks now.  Re-check.
        self._background_tasks.clear()
        self._expected_cancelled_tasks.clear()
        self._session_tasks.clear()
        # Flush pending messages to disk before clearing (#72680).
        try:
            from gateway.shutdown_flush import flush_pending_to_file
            flush_pending_to_file(self._pending_messages, reason="adapter_shutdown")
        except Exception:
            pass
        self._pending_messages.clear()
        self._active_sessions.clear()
        for state in list(self._text_debounce_store().values()):
            if state.task is not None and not state.task.done():
                state.task.cancel()
        self._text_debounce_store().clear()

    def has_pending_interrupt(self, session_key: str) -> bool:
        """Check if there's a pending interrupt for a session."""
        return session_key in self._active_sessions and self._active_sessions[session_key].is_set()
    
    def get_pending_message(self, session_key: str) -> Optional[MessageEvent]:
        """Get and clear any pending message for a session."""
        return self._pending_messages.pop(session_key, None)
    
    def build_source(
        self,
        chat_id: str,
        chat_name: Optional[str] = None,
        chat_type: str = "dm",
        user_id: Optional[str] = None,
        user_name: Optional[str] = None,
        user_id_alt: Optional[str] = None,
        chat_id_alt: Optional[str] = None,
    ) -> SessionSource:
        """Helper to build a SessionSource for this platform."""
        return SessionSource(
            platform=self.platform,
            chat_id=str(chat_id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(user_id) if user_id else None,
            user_name=user_name,
            user_id_alt=user_id_alt,
            chat_id_alt=chat_id_alt,
        )
    
    @abstractmethod
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """
        Get information about a chat/channel.
        
        Returns dict with at least:
        - name: Chat name
        - type: "dm", "group", "channel"
        """
        pass

    def toolsets_for_source(self, source: "SessionSource") -> Optional[List[str]]:
        """Per-source toolset override for agent runs triggered by this adapter.

        Return a list of configurable toolset keys (e.g. ``["terminal",
        "file", "web"]``) to REPLACE the platform-level toolset resolution
        for this specific source, or ``None`` to use the normal
        ``platform_toolsets.<platform>`` resolution (the default).

        The gateway validates the returned list through the same
        ``_get_platform_tools`` path as platform-level config, so unknown or
        platform-restricted toolset names are dropped rather than trusted.

        Reserved for adapters that pin per-source toolset overrides.
        """
        return None
    
    def format_message(self, content: str) -> str:
        """
        Format a message for this platform.
        
        Override in subclasses to handle platform-specific formatting
        (e.g., Discord markdown).
        
        Default implementation returns content as-is.
        """
        return content
    
    @staticmethod
    def truncate_message(
        content: str,
        max_length: int = 4096,
        len_fn: Optional["Callable[[str], int]"] = None,
    ) -> List[str]:
        """
        Split a long message into chunks, preserving code block boundaries.

        When a split falls inside a triple-backtick code block, the fence is
        closed at the end of the current chunk and reopened (with the original
        language tag) at the start of the next chunk.  Multi-chunk responses
        receive indicators like ``(1/3)``.

        Args:
            content: The full message content
            max_length: Maximum length per chunk (platform-specific)
            len_fn: Optional length function for measuring string length.
                     Defaults to ``len`` (Unicode code-points).  Pass
                     ``utf16_len`` for platforms that measure message
                     length in UTF-16 code units.

        Returns:
            List of message chunks
        """
        _len = len_fn or len
        if _len(content) <= max_length:
            return [content]

        INDICATOR_RESERVE = 10   # room for " (XX/XX)"
        FENCE_CLOSE = "\n```"

        chunks: List[str] = []
        remaining = content
        # When the previous chunk ended mid-code-block, this holds the
        # language tag (possibly "") so we can reopen the fence.
        carry_lang: Optional[str] = None

        while remaining:
            # If we're continuing a code block from the previous chunk,
            # prepend a new opening fence with the same language tag.
            prefix = f"```{carry_lang}\n" if carry_lang is not None else ""

            # How much body text we can fit after accounting for the prefix,
            # a potential closing fence, and the chunk indicator.
            headroom = max_length - INDICATOR_RESERVE - _len(prefix) - _len(FENCE_CLOSE)
            if headroom < 1:
                # Floor at 1 so a pathologically small max_length (0 or 1 —
                # e.g. a relay capability descriptor whose max_message_length
                # is 0/1) can't make headroom 0 and stall the loop below.
                headroom = max(1, max_length // 2)

            # Everything remaining fits in one final chunk
            if _len(prefix) + _len(remaining) <= max_length - INDICATOR_RESERVE:
                final_chunk = prefix + remaining
                # Check fence balance: if carry_lang was set, the chunk
                # starts with an opening fence.  Walk the remaining text
                # to see if the code block was closed; if not, close it.
                _final_in_code = carry_lang is not None
                _final_lang = carry_lang or ""
                if _final_in_code:
                    for _line in remaining.split("\n"):
                        _stripped = _line.strip()
                        if _stripped.startswith("```"):
                            if _final_in_code:
                                _final_in_code = False
                                _final_lang = ""
                            else:
                                _final_in_code = True
                                _tag = _stripped[3:].strip()
                                _final_lang = _tag.split()[0] if _tag else ""
                    if _final_in_code:
                        final_chunk += FENCE_CLOSE
                chunks.append(final_chunk)
                break

            # Find a natural split point (prefer newlines, then spaces).
            # When _len != len (e.g. utf16_len), headroom is
            # measured in the custom unit.  We need codepoint-based slice
            # positions that stay within the custom-unit budget.
            #
            # _safe_slice_pos() maps a custom-unit budget to the largest
            # codepoint offset whose custom length ≤ budget.
            if _len is not len:
                # Map headroom (custom units) → codepoint slice length
                _cp_limit = _custom_unit_to_cp(remaining, headroom, _len)
            else:
                _cp_limit = headroom
            region = remaining[:_cp_limit]
            split_at = region.rfind("\n")
            if split_at < _cp_limit // 2:
                split_at = region.rfind(" ")
            if split_at < 1:
                # Consume at least one codepoint. Without the max(1, …) floor,
                # a zero _cp_limit — reachable when max_length is 0/1, or under
                # utf16_len when the next char is a surrogate pair wider than
                # the whole budget — leaves split_at at 0, so ``remaining``
                # never shrinks and the while-loop spins forever appending
                # empty chunks (an unbounded hang / OOM).
                #
                # Length contract for a degenerate budget: a codepoint is the
                # smallest indivisible unit, so when the budget is smaller than
                # one codepoint (e.g. max_length=1 with a 2-unit surrogate pair
                # under utf16_len) the emitted chunk WILL exceed max_length by
                # that one codepoint. That is intentional — emitting the
                # codepoint whole preserves the content, whereas the only
                # alternatives are dropping it (data loss) or looping forever.
                # Real callers never hit this: platform caps are hundreds/
                # thousands, and the relay path normalizes a 0/negative
                # descriptor bound to 4096 (see gateway/relay/descriptor.py).
                split_at = max(1, _cp_limit)

            # Avoid splitting inside an inline code span (`...`).
            # If the text before split_at has an odd number of unescaped
            # backticks, the split falls inside inline code — the resulting
            # chunk would have an unpaired backtick and any special characters
            # (like parentheses) inside the broken span would be unescaped,
            # causing MarkdownV2 parse errors on the platform.
            candidate = remaining[:split_at]
            backtick_count = candidate.count("`") - candidate.count("\\`")
            if backtick_count % 2 == 1:
                # Find the last unescaped backtick and split before it
                last_bt = candidate.rfind("`")
                while last_bt > 0 and candidate[last_bt - 1] == "\\":
                    last_bt = candidate.rfind("`", 0, last_bt)
                if last_bt > 0:
                    # Try to find a space or newline just before the backtick
                    safe_split = candidate.rfind(" ", 0, last_bt)
                    nl_split = candidate.rfind("\n", 0, last_bt)
                    safe_split = max(safe_split, nl_split)
                    if safe_split > _cp_limit // 4:
                        split_at = safe_split

            chunk_body = remaining[:split_at]
            remaining = remaining[split_at:].lstrip()

            full_chunk = prefix + chunk_body

            # Walk only the chunk_body (not the prefix we prepended) to
            # determine whether we end inside an open code block.
            in_code = carry_lang is not None
            lang = carry_lang or ""
            for line in chunk_body.split("\n"):
                stripped = line.strip()
                if stripped.startswith("```"):
                    if in_code:
                        in_code = False
                        lang = ""
                    else:
                        in_code = True
                        tag = stripped[3:].strip()
                        lang = tag.split()[0] if tag else ""

            if in_code:
                # Close the orphaned fence so the chunk is valid on its own
                full_chunk += FENCE_CLOSE
                carry_lang = lang
            else:
                carry_lang = None

            chunks.append(full_chunk)

        # Append chunk indicators when the response spans multiple messages
        if len(chunks) > 1:
            total = len(chunks)
            chunks = [
                f"{chunk} ({i + 1}/{total})" for i, chunk in enumerate(chunks)
            ]

        return chunks
