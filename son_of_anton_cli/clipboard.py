"""Clipboard image extraction for macOS and Linux.

Provides a single function `save_clipboard_image(dest)` that checks the
system clipboard for image data, saves it to *dest* as PNG, and returns
True on success.  No external Python dependencies — uses only OS-level
CLI tools that ship with the platform (or are commonly installed).

Platform support:
  macOS   — osascript (always available), pngpaste (if installed)
  Linux   — wl-paste (Wayland), xclip (X11)
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def save_clipboard_image(dest: Path) -> bool:
    """Extract an image from the system clipboard and save it as PNG.

    Returns True if an image was found and saved, False otherwise.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        return _macos_save(dest)
    return _linux_save(dest)


def has_clipboard_image() -> bool:
    """Quick check: does the clipboard currently contain an image?

    Lighter than save_clipboard_image — doesn't extract or write anything.
    """
    if sys.platform == "darwin":
        return _macos_has_image()
    # Match _linux_save fallthrough order: Wayland → X11
    if os.environ.get("WAYLAND_DISPLAY") and _wayland_has_image():
        return True
    return _xclip_has_image()


# ── Text write (native tools, mirrors ui-tui/src/lib/clipboard.ts) ──────

def _write_clipboard_commands() -> list:
    """Return (cmd_argv, use_stdin) candidates in platform fallback order."""
    if sys.platform == "darwin":
        return [(["pbcopy"], True)]
    attempts = []
    if os.environ.get("WAYLAND_DISPLAY"):
        attempts.append((["wl-copy", "--type", "text/plain"], True))
    attempts.append((["xclip", "-selection", "clipboard", "-in"], True))
    attempts.append((["xsel", "--clipboard", "--input"], True))
    return attempts


def is_remote_shell_session(env=None) -> bool:
    """True when running inside an SSH session.

    Mirrors ui-tui/src/lib/terminalSetup.ts isRemoteShellSession().  Over
    SSH, native clipboard tools write the REMOTE machine's clipboard (or
    an X-forwarded one), which is almost never what the user wants —
    OSC 52 reaches the LOCAL terminal emulator instead.
    """
    e = os.environ if env is None else env
    return bool(
        e.get("SSH_CONNECTION") or e.get("SSH_TTY") or e.get("SSH_CLIENT")
    )


def write_clipboard_text(text: str) -> bool:
    """Write *text* to the system clipboard via native platform tools.

    Fallback order matches the TUI (ui-tui/src/lib/clipboard.ts):
    macOS pbcopy → wl-copy → xclip → xsel.  Returns True if any backend
    succeeded; callers should fall back to OSC 52 on False.
    """
    for argv, use_stdin in _write_clipboard_commands():
        try:
            proc = subprocess.run(
                argv, input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if proc.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


# ── macOS ────────────────────────────────────────────────────────────────

def _macos_save(dest: Path) -> bool:
    """Try pngpaste first (fast, handles more formats), fall back to osascript."""
    return _macos_pngpaste(dest) or _macos_osascript(dest)


def _macos_has_image() -> bool:
    """Check if macOS clipboard contains image data."""
    try:
        info = subprocess.run(
            ["osascript", "-e", "clipboard info"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
        )
        return "«class PNGf»" in info.stdout or "«class TIFF»" in info.stdout
    except Exception:
        return False


def _macos_pngpaste(dest: Path) -> bool:
    """Use pngpaste (brew install pngpaste) — fastest, cleanest."""
    try:
        r = subprocess.run(
            ["pngpaste", str(dest)],
            capture_output=True, timeout=3,
        )
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return True
    except FileNotFoundError:
        pass  # pngpaste not installed
    except Exception as e:
        logger.debug("pngpaste failed: %s", e)
    return False


def _macos_osascript(dest: Path) -> bool:
    """Use osascript to extract PNG data from clipboard (always available)."""
    if not _macos_has_image():
        return False

    # Extract as PNG
    script = (
        'try\n'
        '  set imgData to the clipboard as «class PNGf»\n'
        f'  set f to open for access POSIX file "{dest}" with write permission\n'
        '  write imgData to f\n'
        '  close access f\n'
        'on error\n'
        '  return "fail"\n'
        'end try\n'
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
        )
        if r.returncode == 0 and "fail" not in r.stdout and dest.exists() and dest.stat().st_size > 0:
            return True
    except Exception as e:
        logger.debug("osascript clipboard extract failed: %s", e)
    return False


# ── Linux ────────────────────────────────────────────────────────────────

def _linux_save(dest: Path) -> bool:
    """Try clipboard backends in priority order: Wayland → X11."""
    if os.environ.get("WAYLAND_DISPLAY"):
        if _wayland_save(dest):
            return True

    return _xclip_save(dest)


# ── Wayland (wl-paste) ──────────────────────────────────────────────────

def _wayland_has_image() -> bool:
    """Check if Wayland clipboard has image content."""
    try:
        r = subprocess.run(
            ["wl-paste", "--list-types"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
        )
        return r.returncode == 0 and any(
            t.startswith("image/") for t in r.stdout.splitlines()
        )
    except FileNotFoundError:
        logger.debug("wl-paste not installed — Wayland clipboard unavailable")
    except Exception:
        pass
    return False


def _wayland_save(dest: Path) -> bool:
    """Use wl-paste to extract clipboard image (Wayland sessions)."""
    try:
        # Check available MIME types
        types_r = subprocess.run(
            ["wl-paste", "--list-types"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
        )
        if types_r.returncode != 0:
            return False
        types = types_r.stdout.splitlines()

        # Prefer PNG, fall back to other image formats
        mime = None
        for preferred in ("image/png", "image/jpeg", "image/bmp",
                          "image/gif", "image/webp"):
            if preferred in types:
                mime = preferred
                break

        if not mime:
            return False

        # Extract the image data
        with open(dest, "wb") as f:
            subprocess.run(
                ["wl-paste", "--type", mime],
                stdout=f, stderr=subprocess.DEVNULL, timeout=5, check=True,
            )

        if not dest.exists() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            return False

        # save_clipboard_image() promises a PNG output path. Wayland can offer
        # JPEG/GIF/WebP/BMP payloads, so normalize every non-PNG result before
        # returning success.
        if mime != "image/png":
            if not _convert_to_png(dest) or not _is_png_file(dest):
                dest.unlink(missing_ok=True)
                return False

        return True

    except FileNotFoundError:
        logger.debug("wl-paste not installed — Wayland clipboard unavailable")
    except Exception as e:
        logger.debug("wl-paste clipboard extraction failed: %s", e)
        dest.unlink(missing_ok=True)
    return False


def _convert_to_png(path: Path) -> bool:
    """Convert an image file to PNG in-place (requires Pillow or ImageMagick)."""
    # Try Pillow first (likely installed in the venv)
    try:
        from PIL import Image
        img = Image.open(path)
        img.save(path, "PNG")
        return True
    except ImportError:
        pass
    except Exception as e:
        logger.debug("Pillow BMP→PNG conversion failed: %s", e)

    # Fall back to ImageMagick convert
    tmp = path.with_suffix(".bmp")
    try:
        path.rename(tmp)
        r = subprocess.run(
            ["convert", str(tmp), "png:" + str(path)],
            capture_output=True, timeout=5,
        )
        if r.returncode == 0 and path.exists() and path.stat().st_size > 0:
            tmp.unlink(missing_ok=True)
            return True
        else:
            # Convert failed — restore the original file
            tmp.rename(path)
    except FileNotFoundError:
        logger.debug("ImageMagick not installed — cannot convert BMP to PNG")
        if tmp.exists() and not path.exists():
            tmp.rename(path)
    except Exception as e:
        logger.debug("ImageMagick BMP→PNG conversion failed: %s", e)
        if tmp.exists() and not path.exists():
            tmp.rename(path)

    # Can't convert — BMP is still usable as-is for most APIs
    return path.exists() and path.stat().st_size > 0


def _is_png_file(path: Path) -> bool:
    """Return True when *path* starts with the PNG file signature."""
    try:
        with path.open("rb") as f:
            return f.read(len(_PNG_SIGNATURE)) == _PNG_SIGNATURE
    except OSError:
        return False


# ── X11 (xclip) ─────────────────────────────────────────────────────────

def _xclip_has_image() -> bool:
    """Check if X11 clipboard has image content."""
    try:
        r = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
        )
        return r.returncode == 0 and "image/png" in r.stdout
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return False


def _xclip_save(dest: Path) -> bool:
    """Use xclip to extract clipboard image (X11 sessions)."""
    # Check if clipboard has image content
    try:
        targets = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
        )
        if "image/png" not in targets.stdout:
            return False
    except FileNotFoundError:
        logger.debug("xclip not installed — X11 clipboard image paste unavailable")
        return False
    except Exception:
        return False

    # Extract PNG data
    try:
        with open(dest, "wb") as f:
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                stdout=f, stderr=subprocess.DEVNULL, timeout=5, check=True,
            )
        if dest.exists() and dest.stat().st_size > 0:
            return True
    except Exception as e:
        logger.debug("xclip image extraction failed: %s", e)
        dest.unlink(missing_ok=True)
    return False
