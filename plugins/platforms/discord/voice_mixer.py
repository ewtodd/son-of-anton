from __future__ import annotations

"""
Continuous PCM audio mixer for Discord voice channels.

discord.py (Rapptz) ships no audio mixer: ``VoiceClient.play()`` accepts a
single :class:`discord.AudioSource` and raises ``ClientException`` if called
while already playing.  One opus stream per connection, one source feeding it.

This module adds software mixing *upstream* of that single stream.  A
:class:`VoiceMixer` is itself a ``discord.AudioSource`` that discord.py polls
every 20 ms via :meth:`read`.  Internally it sums the 20 ms PCM frames of any
number of child sources, clamps to int16, and returns one blended frame.
discord.py never knows several streams were combined underneath — it just
encodes and sends the single mixed frame.

This gives us, for one voice connection at once:

  * an always-on low-volume **ambient/idle loop** (the "thinking" sound),
  * a **speech** channel (TTS replies, verbal acknowledgements) that plays
    *over* the ambient bed, automatically **ducking** the ambient gain down
    while speech is active and restoring it when speech ends — the smooth
    Grok-voice-mode feel, instead of stop-and-swap.

Design notes
------------
* The mixer is installed **once** per guild on join (``vc.play(mixer)``) and
  runs continuously until the bot leaves.  Children come and go; the mixer
  itself never stops, so there is no ``is_playing()`` race between an
  acknowledgement and the final reply.
* Frame format is Discord-native: 48 kHz, 2 channels, signed 16-bit LE,
  20 ms per frame == ``discord.opus.Encoder.FRAME_SIZE`` bytes
  (3840 = 960 samples * 2 channels * 2 bytes).
* Mixing is a single vectorised int32 add + clip per 20 ms frame (numpy,
  already a core dependency).  CPU cost is negligible.
* :meth:`read` is called from discord.py's audio sender **thread**, while
  children are added/removed from the asyncio event loop thread, so all
  shared state is guarded by a plain ``threading.Lock``.

The mixer NEVER touches the inbound receive path: it only produces the bot's
*outgoing* stream.  The :class:`VoiceReceiver` decodes incoming SSRCs only, so
the mixer's output cannot echo back into transcription.
"""

import logging
import threading
from typing import TYPE_CHECKING, List, Optional

import discord

try:
    from .ffmpeg_utils import resolve_ffmpeg_executable
except ImportError:
    from ffmpeg_utils import resolve_ffmpeg_executable

if TYPE_CHECKING:  # numpy is an optional ("voice" extra) dep — never import at runtime top-level
    import numpy as np

logger = logging.getLogger(__name__)


def _require_numpy():
    """Import numpy lazily.

    numpy ships in the optional ``voice`` extra, not the base install, so this
    module must import cleanly without it (the Discord adapter imports this
    file unconditionally).  Callers that actually mix audio call this; if the
    voice extra isn't installed they get a clear error instead of a top-level
    ImportError that would break the whole adapter import.
    """
    import numpy as np  # noqa: PLC0415 — intentional lazy import
    return np

# Discord-native frame geometry (matches discord.opus.Encoder).
SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2                       # bytes per sample (s16)
BYTES_PER_MS = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH // 1000  # 192


# ----------------------------------------------------------------------
# PCM helpers
# ----------------------------------------------------------------------

def decode_to_pcm(path: str, *, timeout: float = 30.0) -> Optional[bytes]:
    """Decode any audio file to 48 kHz / stereo / s16le PCM via ffmpeg.

    Returns the raw PCM bytes, or None on failure.  ffmpeg is already a hard
    requirement of the voice path (see ``VoiceReceiver.pcm_to_wav``).
    """
    import subprocess

    try:
        proc = subprocess.run(
            [
                resolve_ffmpeg_executable(), "-y", "-loglevel", "error",
                "-i", path,
                "-f", "s16le",
                "-ar", str(SAMPLE_RATE),
                "-ac", str(CHANNELS),
                "pipe:1",
            ],
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("decode_to_pcm failed for %s: %s", path, e)
        return None
    if proc.returncode != 0:
        logger.warning(
            "ffmpeg decode failed for %s (rc=%d): %s",
            path, proc.returncode, (proc.stderr or b"").decode("utf-8", "replace")[:200],
        )
        return None
    return proc.stdout or None


