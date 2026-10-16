"""Getting attachment bytes to signal-cli when it cannot open our paths.

signal-cli's ``send`` takes attachment *paths* and opens them itself, as its own
user on its own host. Two deployments make a path the gateway just stat()ed
unreadable to the daemon:

- the daemon runs on another host (``SIGNAL_HTTP_URL`` off-box):
  ``... (No such file or directory) (AttachmentInvalidException)``;
- the daemon runs on this host as its own user (the NixOS ``signal-cli``
  service) and the file sits somewhere that user may not enter, such as a home
  directory: ``... (Permission denied) (AttachmentInvalidException)``.

signal-cli also accepts an RFC 2397 data URI in place of a path (its own
``--attachment`` help documents ``data:<MIME>;filename=<NAME>;base64,<DATA>``),
so a sender that gets this error resends with the bytes inline. The resend is
gated on that error alone: after a transient failure the first send may have
gone through, and the recipient would get the attachment twice.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import quote

from gateway.platforms.signal_rate_limit import SIGNAL_MAX_ATTACHMENTS_PER_MSG

logger = logging.getLogger(__name__)

# Cap on the attachment bytes one inline send carries. Deliberately far below
# Signal's 100 MB attachment ceiling: inlining base64-encodes the files (+33%)
# into a single JSON body, so the process holds several copies at once.
SIGNAL_MAX_INLINE_ATTACHMENT_SIZE = 16 * 1024 * 1024  # 16 MB


def is_unreadable_attachment_error(err: Any) -> bool:
    """Whether signal-cli rejected an attachment it could not open."""
    if not err:
        return False
    message = err.get("message", "") if isinstance(err, dict) else str(err)
    message = str(message)
    return "AttachmentInvalidException" in message or "No such file" in message


def inline_attachment_uri(file_path: str) -> Optional[str]:
    """The file as a data URI for signal-cli, or None if it cannot be read.

    The filename is what Signal shows the recipient; without it the
    attachment arrives unnamed. It is percent-encoded because it sits in a
    ';'-delimited field, where a ';' or ',' would truncate the payload.
    """
    path = Path(file_path)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        logger.warning("Signal: could not read %s for inline send: %s", file_path, exc)
        return None
    return f"data:{mime};filename={quote(path.name)};base64,{payload}"


def inline_batches(paths: List[str]) -> Tuple[List[List[str]], List[str]]:
    """Split ``paths`` into inline sends, keeping their order.

    Each send carries at most ``SIGNAL_MAX_ATTACHMENTS_PER_MSG`` files and
    ``SIGNAL_MAX_INLINE_ATTACHMENT_SIZE`` bytes. Returns ``(batches,
    too_large)``: a file bigger than the cap on its own cannot go inline.
    """
    batches: List[List[str]] = []
    too_large: List[str] = []
    current: List[str] = []
    current_bytes = 0
    for path in paths:
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = 0  # inline_attachment_uri reports it when the send reads it
        if size > SIGNAL_MAX_INLINE_ATTACHMENT_SIZE:
            too_large.append(path)
            continue
        if current and (
            len(current) >= SIGNAL_MAX_ATTACHMENTS_PER_MSG
            or current_bytes + size > SIGNAL_MAX_INLINE_ATTACHMENT_SIZE
        ):
            batches.append(current)
            current, current_bytes = [], 0
        current.append(path)
        current_bytes += size
    if current:
        batches.append(current)
    return batches, too_large
