"""Send Message Tool -- cross-channel messaging via platform APIs.

Sends a message to a user or channel on any connected messaging platform
(Discord, Slack, Signal). Supports listing available targets and resolving
human-friendly channel names to IDs. Works in both CLI and gateway contexts.
"""

import asyncio
import json
import logging
import os
import re
import time


from agent.redact import redact_sensitive_text
from agent.secret_scope import get_secret

logger = logging.getLogger(__name__)

_NUMERIC_TOPIC_RE = re.compile(r"^\s*(-?\d+)(?::(\d+))?\s*$")
# Slack conversation IDs: C (public channel), G (private/group channel), D (DM).
# Must be uppercase alphanumeric, 9+ chars. User IDs (U...) are parsed as
# explicit user targets (``user:U...``) and are converted to D... conversations
# via conversations.open before chat.postMessage — posting directly to a U/W
# ID fails because the API requires a conversation ID. ``@handle`` targets are
# resolved through users.list first (``user_name:...``).
_SLACK_TARGET_RE = re.compile(r"^\s*([CGD][A-Z0-9]{8,})\s*$")
_SLACK_USER_ID_RE = re.compile(r"^\s*(U[A-Z0-9]{8,})\s*$")
_SLACK_USER_NAME_RE = re.compile(r"^\s*@([A-Za-z0-9._-]{1,80})\s*$")
_SLACK_MENTION_RE = re.compile(r"^\s*<@(U[A-Z0-9]{8,})(?:\|[^>]+)?>\s*$")
# Session-derived Slack thread targets use "<conversation_id>:<thread_ts>".
_SLACK_THREAD_TARGET_RE = re.compile(r"^\s*([CGD][A-Z0-9]{8,}):([^\s:]+)\s*$")
# Platforms that address recipients by phone number and accept E.164 format
# (with a leading '+'). Without this, "+15551234567" fails the isdigit() check
# below and falls through to channel-name resolution, which has no way to
# resolve a raw phone number. Keeping the '+' preserves the E.164 form that
# downstream adapters (signal, etc.) expect.
_PHONE_PLATFORMS = frozenset({"signal"})
_E164_TARGET_RE = re.compile(r"^\s*\+(\d{7,15})\s*$")
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
_AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".m2a", ".wav", ".m4a", ".flac"}
_VOICE_EXTS = {".ogg", ".opus"}

# Extensions that carry a native caption on the media bubble itself
# (photo/video/document). Voice/audio notes are excluded: a caption on a
# voice note reads as a separate label rather than a bubble caption, and the
# established convention is to keep the accompanying text as its own message.
_CAPTIONABLE_EXTS = _IMAGE_EXTS | _VIDEO_EXTS | {
    ".pdf", ".doc", ".docx", ".txt", ".md", ".csv", ".xlsx", ".zip",
}

# Per-platform native caption length limits (characters). Text longer than
# the limit can't ride on the media bubble and stays a separate body message.
_DEFAULT_CAPTION_LIMIT = 4096

def prepare_send_message_platforms() -> None:
    """Load enabled standalone plugins before tool schemas/cache keys are built."""
    from son_of_anton_cli.plugins import discover_plugins

    discover_plugins()


def _media_caption_split(text, media_files, *, max_caption_len):
    """Decide whether the accompanying text should ride on the media bubble.

    Single enforced chokepoint for the ``MEDIA:<path> caption`` behavior
    across every standalone sender. ``son-of-anton send`` (and the send_message
    tool / cron) strips the ``MEDIA:`` tag and leaves the remaining prose as
    ``text``; historically each platform sent that ``text`` as a *separate*
    message before an uncaptioned media bubble, splitting the reported case
    ``son-of-anton send --to discord "MEDIA:/x.png This Caption"`` into two parts.

    Returns ``(caption, body_text)``:

    * ``(caption, "")`` — attach ``text`` to the media as its native caption
      and send *no* separate body message. Only when there is exactly one
      media file, it is a captionable kind (image/video/document, not a
      voice/audio note), and ``text`` fits ``max_caption_len``.
    * ``(None, text)`` — keep the historical behavior: ``text`` is a separate
      body message and the media carries no caption. Applies to multi-file
      sends (caption→file association is ambiguous), voice/audio notes, empty
      text, or text longer than the caption limit.
    """
    stripped = (text or "").strip()
    media = media_files or []
    if not stripped or len(media) != 1:
        return None, text
    media_path, is_voice = media[0]
    if is_voice:
        return None, text
    ext = os.path.splitext(media_path)[1].lower()
    if ext not in _CAPTIONABLE_EXTS:
        return None, text
    # Measure the caption in Unicode codepoints — a portable upper bound that
    # never under-counts UTF-16 units for BMP text, so an
    # over-count only fails safe (falls back to a separate message).
    if len(stripped) > max_caption_len:
        return None, text
    return stripped, ""
_URL_SECRET_QUERY_RE = re.compile(
    r"([?&](?:access_token|api[_-]?key|auth[_-]?token|token|signature|sig)=)([^&#\s]+)",
    re.IGNORECASE,
)
_GENERIC_SECRET_ASSIGN_RE = re.compile(
    r"\b(access_token|api[_-]?key|auth[_-]?token|signature|sig)\s*=\s*([^\s,;]+)",
    re.IGNORECASE,
)


def _sanitize_error_text(text) -> str:
    """Redact secrets from error text before surfacing it to users/models."""
    redacted = redact_sensitive_text(text)
    redacted = _URL_SECRET_QUERY_RE.sub(lambda m: f"{m.group(1)}***", redacted)
    redacted = _GENERIC_SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=***", redacted)
    return redacted


def _error(message: str) -> dict:
    """Build a standardized error payload with redacted content."""
    return {"error": _sanitize_error_text(message)}


def _display_chat_id(platform_name: str, chat_id: str) -> str:
    """Return a result-safe chat identifier for tool transcripts/log consumers."""
    if platform_name == "signal" and str(chat_id).startswith("group:"):
        return "group:***"
    return chat_id


SEND_MESSAGE_SCHEMA = {
    "name": "send_message",
    "description": (
        "Send a message to a connected messaging platform, or list available targets.\n\n"
        "IMPORTANT: When the user asks to send to a specific channel or person "
        "(not just a bare platform name), call send_message(action='list') FIRST to see "
        "available targets, then send to the correct one.\n"
        "If the user just says a platform name like 'send to discord', send directly "
        "to the home channel without listing first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "list", "react", "unreact"],
                 "description": "Action to perform. 'send' (default) sends a message. 'list' returns all available channels/contacts across connected platforms. 'react' attaches an emoji reaction to a message (platforms that support it). 'unreact' retracts a previously-added reaction."
            },
            "target": {
                "type": "string",
                 "description": "Delivery target. Format: 'platform' (uses home channel), 'platform:#channel-name', 'platform:chat_id', or 'platform:chat_id:thread_id' for threaded platforms. Examples: 'discord', 'discord:999888777:555444333', 'discord:#bot-home', 'slack:#engineering', 'signal:+155****4567'"
            },
            "message": {
                "type": "string",
                "description": "The message text to send. To send an image or file, include MEDIA:<local_path> (e.g. 'MEDIA:/tmp/report.pdf') in the message — the platform will deliver it as a native media attachment."
            },
            "emoji": {
                "type": "string",
                "description": "For action='react': the emoji to react with (e.g. '❤️'). On iMessage, ❤️👍👎😂‼️❓ render as native tapbacks; other emoji use custom-emoji reactions."
            },
            "message_id": {
                "type": "string",
                "description": "For action='react'/'unreact': id of the message to react to. Omit to target the most recent message received in that chat (usually the one being replied to)."
            }
        },
        "required": []
    }
}


def send_message_tool(args, **kw):
    """Handle cross-channel send_message tool calls."""
    action = args.get("action", "send")

    if action == "list":
        return _handle_list()

    if action == "react":
        return _handle_react(args)

    if action == "unreact":
        return _handle_react(args, remove=True)

    return _handle_send(args)


def _handle_list():
    """Return formatted list of available messaging targets."""
    try:
        from gateway.channel_directory import format_directory_for_display
        return json.dumps({"targets": format_directory_for_display()})
    except Exception as e:
        return json.dumps(_error(f"Failed to load channel directory: {e}"))


def _handle_react(args, remove=False):
    """Attach (or with ``remove=True`` retract) an emoji reaction on a message
    via a live gateway adapter.

    Only adapters that expose ``add_reaction(chat_id, emoji, message_id)`` /
    ``remove_reaction(chat_id, message_id)`` coroutines support this (e.g.
    message reactions). Requires the gateway to be running in this
    process — there is no standalone fallback, since reacting needs the
    adapter's live message-id state.
    """
    target = args.get("target", "")
    emoji = (args.get("emoji") or "").strip()
    message_id = (args.get("message_id") or "").strip() or None
    if not target or (not remove and not emoji):
        return tool_error(
            "Both 'target' and 'emoji' are required when action='react'"
            if not remove
            else "'target' is required when action='unreact'"
        )

    parts = target.split(":", 1)
    platform_name = parts[0].strip().lower()
    target_ref = parts[1].strip() if len(parts) > 1 else None
    chat_id = None
    prepare_send_message_platforms()
    if target_ref:
        # Platform-native ids passed through verbatim
        # match no parser pattern and no directory entry, so hand them to
        # the adapter unchanged; it validates them.
        chat_id, _thread_id, resolution_error = resolve_send_target(
            platform_name, target_ref, pass_unresolved_references=True
        )
        if resolution_error:
            return tool_error(resolution_error)

    try:
        from gateway.config import Platform, load_gateway_config
        platform = Platform(platform_name)
    except (ValueError, KeyError):
        return tool_error(f"Unknown platform: {platform_name}")

    if not chat_id:
        try:
            config = load_gateway_config()
            home = config.get_home_channel(platform)
        except Exception:
            home = None
        if not home:
            return tool_error(
                f"No chat specified and no home channel set for {platform_name}. "
                f"Use '{platform_name}:chat_id'."
            )
        chat_id = home.chat_id

    runner = None
    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
    except Exception:
        runner = None
    adapter = runner.adapters.get(platform) if runner is not None else None
    if adapter is None:
        return tool_error(
            f"Reactions require a live {platform_name} adapter in the running "
            "gateway (not available from cron/standalone contexts)."
        )
    fn_name = "remove_reaction" if remove else "add_reaction"
    react_fn = getattr(adapter, fn_name, None)
    if not callable(react_fn):
        return tool_error(
            f"Platform '{platform_name}' does not support message reactions."
        )

    try:
        from model_tools import _run_async
        if remove:
            result = _run_async(
                react_fn(chat_id=chat_id, message_id=message_id)
            )
        else:
            result = _run_async(
                react_fn(chat_id=chat_id, emoji=emoji, message_id=message_id)
            )
    except Exception as e:
        return json.dumps(_error(f"Reaction failed: {e}"))
    if isinstance(result, dict):
        return json.dumps(result)
    return json.dumps({"success": bool(result)})


def _handle_send(args):
    """Send a message to a platform target."""
    target = args.get("target", "")
    message = args.get("message", "")
    if not target or not message:
        return tool_error("Both 'target' and 'message' are required when action='send'")

    parts = target.split(":", 1)
    platform_name = parts[0].strip().lower()
    target_ref = parts[1].strip() if len(parts) > 1 else None
    chat_id = None
    thread_id = None

    prepare_send_message_platforms()
    if target_ref:
        chat_id, thread_id, resolution_error = resolve_send_target(
            platform_name, target_ref
        )
        if resolution_error:
            return tool_error(resolution_error)

    from tools.interrupt import is_interrupted
    if is_interrupted():
        return tool_error("Interrupted")

    try:
        from gateway.config import load_gateway_config, Platform
        config = load_gateway_config()
    except Exception as e:
        return json.dumps(_error(f"Failed to load gateway config: {e}"))

    from gateway.platform_registry import platform_registry

    entry = platform_registry.get(platform_name)
    is_builtin = platform_name in {member.value for member in Platform}
    if not is_builtin and entry is None:
        return tool_error(
            f"Unknown or unregistered plugin platform: {platform_name}"
        )
    try:
        platform = Platform(platform_name)
    except (ValueError, KeyError):
        return tool_error(f"Unknown platform: {platform_name}")

    pconfig = config.platforms.get(platform)
    if not pconfig or not pconfig.enabled:
        return tool_error(f"Platform '{platform_name}' is not configured. Set up credentials in ~/.son-of-anton/config.yaml or environment variables.")

    from gateway.platforms.base import BasePlatformAdapter

    # Capture [[as_document]] directive before extract_media strips it.
    # Image-extension files in this batch will route through send_document
    # instead of send_photo so the original bytes survive (e.g. info-graph
    # JPGs where the photo path recompresses to 1280px).
    force_document_attachments = "[[as_document]]" in message

    media_files, cleaned_message = BasePlatformAdapter.extract_media(message)
    media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
    mirror_text = cleaned_message.strip() or _describe_media_for_mirror(media_files)

    used_home_channel = False
    if not chat_id:
        home = config.get_home_channel(platform)
        if home:
            chat_id = home.chat_id
            used_home_channel = True
        else:
            home_env = _HOME_CHANNEL_ENV_OVERRIDES.get(
                platform_name, f"{platform_name.upper()}_HOME_CHANNEL"
            )
            return tool_error(
                f"No home channel set for {platform_name} to determine where to send the message. "
                f"Either specify a channel directly with '{platform_name}:CHANNEL_NAME', "
                f"or set a home channel via: son-of-anton config set {home_env} <channel_id>"
            )

    duplicate_skip = _maybe_skip_cron_duplicate_send(platform_name, chat_id, thread_id)
    if duplicate_skip:
        return json.dumps(duplicate_skip)

    # Slack: resolve user targets to DM channel IDs before sending.
    # _parse_target_ref emits internal ``user:U...`` / ``user_name:@handle``
    # targets; a bare U... id can also arrive from session metadata or the
    # home-channel config. All are opened via conversations.open (fixes #19236).
    if platform_name == "slack" and chat_id:
        _slack_dm_target = chat_id
        if _slack_dm_target.startswith("U") and _SLACK_USER_ID_RE.fullmatch(_slack_dm_target):
            _slack_dm_target = f"user:{_slack_dm_target}"
        if _slack_dm_target.startswith(("user:", "user_name:")):
            from model_tools import _run_async
            _resolved, _resolve_err = _run_async(
                _resolve_slack_user_target(pconfig.token, _slack_dm_target)
            )
            if _resolve_err:
                return json.dumps(_resolve_err)
            chat_id = _resolved

    try:
        from model_tools import _run_async
        send_kwargs = {
            "thread_id": thread_id,
            "media_files": media_files,
            "force_document": force_document_attachments,
        }
        # Preserve the exact built-in call contract; only custom handlers need
        # the complete typed request.
        if entry is not None and entry.send_message_handler is not None:
            send_kwargs["args"] = args
        result = _run_async(
            _send_to_platform(
                platform,
                pconfig,
                chat_id,
                cleaned_message,
                **send_kwargs,
            )
        )
        if used_home_channel and isinstance(result, dict) and result.get("success"):
            result["note"] = f"Sent to {platform_name} home channel (chat_id: {chat_id})"

        # Mirror the sent message into the target's gateway session
        if isinstance(result, dict) and result.get("success") and mirror_text:
            try:
                from gateway.mirror import mirror_to_session
                from gateway.session_context import get_session_env
                source_label = get_session_env("SON_OF_ANTON_SESSION_PLATFORM", "cli")
                user_id = get_session_env("SON_OF_ANTON_SESSION_USER_ID", "") or None
                if mirror_to_session(
                    platform_name,
                    chat_id,
                    mirror_text,
                    source_label=source_label,
                    thread_id=thread_id,
                    user_id=user_id,
                ):
                    result["mirrored"] = True
            except Exception:
                pass

        if isinstance(result, dict) and "error" in result:
            result["error"] = _sanitize_error_text(result["error"])
        return json.dumps(result)
    except Exception as e:
        return json.dumps(_error(f"Send failed: {e}"))


def _parse_target_ref(platform_name: str, target_ref: str):
    """Parse a tool target into chat_id/thread_id and whether it is explicit."""
    if platform_name == "discord":
        match = _NUMERIC_TOPIC_RE.fullmatch(target_ref)
        if match:
            return match.group(1), match.group(2), True
    if platform_name == "slack":
        match = _SLACK_THREAD_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), match.group(2), True
        match = _SLACK_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), None, True
        match = _SLACK_USER_ID_RE.fullmatch(target_ref) or _SLACK_MENTION_RE.fullmatch(target_ref)
        if match:
            return f"user:{match.group(1)}", None, True
        match = _SLACK_USER_NAME_RE.fullmatch(target_ref)
        if match:
            return f"user_name:{match.group(1)}", None, True
    stripped_target = target_ref.strip()
    if platform_name == "signal" and stripped_target.startswith("group:"):
        group_id = stripped_target[len("group:"):].strip()
        if group_id:
            return f"group:{group_id}", None, True
        return None, None, False
    if platform_name in _PHONE_PLATFORMS:
        match = _E164_TARGET_RE.fullmatch(target_ref)
        if match:
            # Preserve the leading '+' — signal-cli expects E.164 format for
            # direct recipients.
            return target_ref.strip(), None, True
    if target_ref.lstrip("-").isdigit():
        return target_ref, None, True
    return None, None, False


def resolve_send_target(
    platform_name: str, target_ref: str, *, pass_unresolved_references: bool = False
) -> tuple[str | None, str | None, str | None]:
    """Resolve one send target the same way for every caller (model tool, CLI, cron).

    Channel-directory IDs are trusted. Plugin platforms must explicitly parse
    native target syntax; for the model-facing send tool (the default), a
    target that can't be resolved is an error — the model can read the error
    and pick a listed target instead.

    ``pass_unresolved_references=True`` restores the old pass-through behavior for
    callers that have no model in the loop (cron delivering a stored job's
    output, react/unreact on platform-native message ids): if the target
    can't be resolved and the platform is built in, or is a plugin platform
    that declares no parser, the string is handed to the adapter exactly as
    written and the adapter decides whether it's valid. A plugin platform
    that DOES declare a parser stays strict for every caller — its parser is
    the authority on native syntax.

    The optional validator has the final say over parser-normalized,
    directory-resolved, and passed-through IDs alike.
    """
    from gateway.config import Platform
    from gateway.platform_registry import platform_registry

    entry = platform_registry.get(platform_name)

    def _validate(candidate: str) -> str | None:
        if entry is None or entry.validate_target_ref_fn is None:
            return None
        try:
            verdict = entry.validate_target_ref_fn(candidate)
        except Exception:
            logger.debug(
                "Plugin target validator failed for %s", platform_name, exc_info=True
            )
            return f"Target validator failed for platform '{platform_name}'"
        if verdict is True:
            return None
        if isinstance(verdict, str) and verdict:
            return f"Invalid target '{target_ref}' on {platform_name}: {verdict}"
        return f"Invalid target '{target_ref}' on {platform_name}"

    if entry is not None and entry.parse_target_ref_fn is not None:
        try:
            parsed = entry.parse_target_ref_fn(target_ref)
        except Exception:
            logger.debug(
                "Plugin target parser failed for %s", platform_name, exc_info=True
            )
            return None, None, f"Target parser failed for platform '{platform_name}'"
        if parsed is not None:
            if (
                not isinstance(parsed, tuple)
                or len(parsed) != 2
                or not isinstance(parsed[0], str)
                or not parsed[0]
                or (parsed[1] is not None and not isinstance(parsed[1], str))
            ):
                return (
                    None,
                    None,
                    f"Target parser for platform '{platform_name}' returned an invalid result",
                )
            parsed_chat_id, parsed_thread_id = parsed
            error = _validate(parsed_chat_id)
            return (None, None, error) if error else (
                parsed_chat_id,
                parsed_thread_id,
                None,
            )

    parsed_chat_id, parsed_thread_id, explicit = _parse_target_ref(
        platform_name, target_ref
    )
    if explicit and parsed_chat_id is not None:
        error = _validate(parsed_chat_id)
        return (None, None, error) if error else (
            parsed_chat_id,
            parsed_thread_id,
            None,
        )

    resolution_failed = False
    try:
        from gateway.channel_directory import resolve_channel_name

        resolved = resolve_channel_name(platform_name, target_ref)
    except Exception:
        resolved = None
        resolution_failed = True
    if resolved:
        parsed_chat_id, parsed_thread_id, _ = _parse_target_ref(
            platform_name, resolved
        )
        chat_id = parsed_chat_id or resolved
        error = _validate(chat_id)
        return (None, None, error) if error else (
            chat_id,
            parsed_thread_id,
            None,
        )

    is_builtin = platform_name in {member.value for member in Platform}
    if entry is None and not is_builtin:
        return None, None, f"Unknown or unregistered plugin platform: {platform_name}"

    def _pass_through_unresolved():
        """Hand the raw target to the adapter unchanged (it validates)."""
        error = _validate(target_ref)
        if error:
            return None, None, error
        logger.debug(
            "Handing unresolved target '%s' to the %s adapter unchanged "
            "(the adapter validates it)",
            target_ref, platform_name,
        )
        return target_ref, None, None

    if entry is not None and entry.source == "plugin" and not is_builtin:
        if pass_unresolved_references and entry.parse_target_ref_fn is None:
            return _pass_through_unresolved()
        return (
            None,
            None,
            f"Could not resolve '{target_ref}' on {platform_name}. "
            "The plugin parser did not recognize it and no channel-directory entry matched.",
        )
    if pass_unresolved_references:
        return _pass_through_unresolved()
    hint = (
        "Try using a numeric channel ID instead."
        if resolution_failed
        else "Use send_message(action='list') to see available targets."
    )
    return None, None, f"Could not resolve '{target_ref}' on {platform_name}. {hint}"


def _describe_media_for_mirror(media_files):
    """Return a human-readable mirror summary when a message only contains media."""
    if not media_files:
        return ""
    if len(media_files) == 1:
        media_path, is_voice = media_files[0]
        ext = os.path.splitext(media_path)[1].lower()
        if is_voice and ext in _VOICE_EXTS:
            return "[Sent voice message]"
        if ext in _IMAGE_EXTS:
            return "[Sent image attachment]"
        if ext in _VIDEO_EXTS:
            return "[Sent video attachment]"
        if ext in _AUDIO_EXTS:
            return "[Sent audio attachment]"
        return "[Sent document attachment]"
    return f"[Sent {len(media_files)} media attachments]"


def _get_cron_auto_delivery_target():
    """Return the cron scheduler's auto-delivery target for the current run, if any."""
    from gateway.session_context import get_session_env
    platform = get_session_env("SON_OF_ANTON_CRON_AUTO_DELIVER_PLATFORM", "").strip().lower()
    chat_id = get_session_env("SON_OF_ANTON_CRON_AUTO_DELIVER_CHAT_ID", "").strip()
    if not platform or not chat_id:
        return None
    thread_id = get_session_env("SON_OF_ANTON_CRON_AUTO_DELIVER_THREAD_ID", "").strip() or None
    return {
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": thread_id,
    }


def _maybe_skip_cron_duplicate_send(platform_name: str, chat_id: str, thread_id: str | None):
    """Skip redundant cron send_message calls when the scheduler will auto-deliver there."""
    auto_target = _get_cron_auto_delivery_target()
    if not auto_target:
        return None

    same_target = (
        auto_target["platform"] == platform_name
        and str(auto_target["chat_id"]) == str(chat_id)
        and auto_target.get("thread_id") == thread_id
    )
    if not same_target:
        return None

    target_label = f"{platform_name}:{chat_id}"
    if thread_id is not None:
        target_label += f":{thread_id}"

    return {
        "success": True,
        "skipped": True,
        "reason": "cron_auto_delivery_duplicate_target",
        "target": target_label,
        "note": (
            f"Skipped send_message to {target_label}. This cron job will already auto-deliver "
            "its final response to that same target. Put the intended user-facing content in "
            "your final response instead, or use a different target if you want an additional message."
        ),
    }


async def _send_via_adapter(
    platform,
    pconfig,
    chat_id,
    chunk,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Send a message via a live gateway adapter, with a standalone fallback
    for out-of-process callers (e.g. cron running separately from the gateway).

    Order of attempts:
      1. Live in-process adapter via ``_gateway_runner_ref()`` (the path that
         existed before this change).
      2. The plugin's ``standalone_sender_fn`` registered on its
         ``PlatformEntry`` (used when the gateway is not in this process, so
         the runner weakref is ``None``).
      3. A descriptive error explaining both options.
    """
    platform_name = platform.value if hasattr(platform, "value") else str(platform)
    runner = None
    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
    except Exception:
        runner = None

    if runner is not None:
        try:
            adapter = runner.adapters.get(platform)
        except Exception:
            adapter = None
        if adapter is not None:
            try:
                metadata = {}
                if thread_id:
                    metadata["thread_id"] = thread_id
                if chat_id:
                    metadata["publish_topic"] = chat_id
                if not metadata:
                    metadata = None
                result = await adapter.send(chat_id=chat_id, content=chunk, metadata=metadata)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return {"error": f"Plugin platform send failed: {e}"}
            if result.success:
                return {"success": True, "message_id": result.message_id}
            return {"error": f"Adapter send failed: {result.error}"}

    entry = None
    try:
        from gateway.platform_registry import platform_registry
        entry = platform_registry.get(platform_name)
    except Exception:
        entry = None

    if entry is not None and entry.standalone_sender_fn is not None:
        try:
            result = await entry.standalone_sender_fn(
                pconfig,
                chat_id,
                chunk,
                thread_id=thread_id,
                media_files=media_files,
                force_document=force_document,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Plugin standalone send for %s raised", platform_name, exc_info=True)
            return {"error": f"Plugin standalone send failed: {e}"}

        if isinstance(result, dict) and (result.get("success") or result.get("error")):
            return result
        return {
            "error": (
                f"Plugin standalone send for '{platform_name}' returned an "
                f"invalid result: expected a dict with 'success' or 'error' "
                f"keys, got {type(result).__name__}"
            )
        }

    return {
        "error": (
            f"No live adapter for platform '{platform_name}'. Is the gateway "
            f"running with this platform connected? For out-of-process delivery "
            f"(e.g. cron in a separate process), the platform plugin must "
            f"register a standalone_sender_fn on its PlatformEntry."
        )
    }


async def _send_to_platform(platform, pconfig, chat_id, message, thread_id=None, media_files=None, force_document=False, args=None):
    """Route a message to the appropriate platform sender.

    Long messages are automatically chunked to fit within platform limits
    using the same smart-splitting algorithm as the gateway adapters
    (preserves code-block boundaries, adds part indicators).
    """
    from gateway.config import Platform

    platform_name = platform.value if hasattr(platform, "value") else str(platform)

    media_files = media_files or []

    from gateway.platforms.base import BasePlatformAdapter, utf16_len

    media_files = media_files or []

    # Platform message length limits come from the plugin registry's
    # PlatformEntry.max_message_length for plugin platforms.
    _MAX_LENGTHS: dict = {}

    # Check plugin registry for max_message_length
    if platform not in _MAX_LENGTHS:
        try:
            from gateway.platform_registry import platform_registry
            entry = platform_registry.get(platform.value)
            if entry and entry.max_message_length > 0:
                _MAX_LENGTHS[platform] = entry.max_message_length
        except Exception:
            pass

    # Smart-chunk the message to fit within platform limits.
    # For short messages or platforms without a known limit this is a no-op.
    max_len = _MAX_LENGTHS.get(platform)
    if max_len:
        chunks = BasePlatformAdapter.truncate_message(message, max_len)
    else:
        chunks = [message]

    # --- Discord: chunked delivery via the registry's standalone_sender_fn.
    # The plugin's ``_standalone_send`` (registered in
    # plugins/platforms/discord/adapter.py) handles forum channels, threads,
    # and multipart media uploads.  ``_send_via_adapter`` tries the live
    # in-process adapter first via ``adapter.send()``, but Discord's elif
    # historically went straight to the HTTP path; we preserve that by
    # explicitly invoking the registry hook here so behavior is unchanged.
    if platform == Platform.DISCORD:
        from gateway.platform_registry import platform_registry
        entry = platform_registry.get("discord")
        if entry is None or entry.standalone_sender_fn is None:
            return {"error": "Discord plugin not registered or missing standalone_sender_fn"}
        # MEDIA:<path> caption: single captionable file + short text rides as
        # the media message content instead of a separate message before the
        # attachment (single enforced decision in _media_caption_split). Cap on
        # the platform's own message limit so the caption is always deliverable.
        _dc_caption, _ = _media_caption_split(
            message, media_files,
            max_caption_len=(max_len or _DEFAULT_CAPTION_LIMIT),
        )
        if _dc_caption is not None:
            result = await entry.standalone_sender_fn(
                pconfig,
                chat_id,
                "",
                thread_id=thread_id,
                media_files=media_files,
                caption=_dc_caption,
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            return result
        last_result = None
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            result = await entry.standalone_sender_fn(
                pconfig,
                chat_id,
                chunk,
                thread_id=thread_id,
                media_files=media_files if is_last else [],
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            last_result = result
        return last_result

    # --- Signal: native attachment support via JSON-RPC attachments param ---
    if platform == Platform.SIGNAL and media_files:
        last_result = None
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            result = await _send_signal(
                pconfig.extra,
                chat_id,
                chunk,
                media_files=media_files if is_last else [],
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            last_result = result
        return last_result

    # --- Slack: native media via files_upload_v2 in the plugin's
    # standalone_sender_fn (plugins/platforms/slack/adapter.py::_standalone_send).
    # Gateway in-channel MEDIA: delivery already worked; send_message previously
    # omitted Slack attachments and told the model media was unsupported.
    if platform == Platform.SLACK and media_files:
        from gateway.platform_registry import platform_registry as _pr_slack
        from son_of_anton_cli.plugins import discover_plugins as _dp_slack
        _dp_slack()
        _slack_entry = _pr_slack.get("slack")
        if _slack_entry is None or _slack_entry.standalone_sender_fn is None:
            return {"error": "Slack plugin not registered or missing standalone_sender_fn"}
        _sl_caption, _ = _media_caption_split(
            message, media_files,
            max_caption_len=(max_len or _DEFAULT_CAPTION_LIMIT),
        )
        if _sl_caption is not None:
            result = await _slack_entry.standalone_sender_fn(
                pconfig,
                chat_id,
                "",
                thread_id=thread_id,
                media_files=media_files,
                caption=_sl_caption,
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            return result
        last_result = None
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            result = await _slack_entry.standalone_sender_fn(
                pconfig,
                chat_id,
                chunk,
                thread_id=thread_id,
                media_files=media_files if is_last else [],
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            last_result = result
        return last_result

    # --- Slack: prefer the live gateway adapter, then the plugin's
    # standalone sender.  The live adapter is multi-workspace aware (it maps
    # channels to the workspace client that owns them) and honors adapter-side
    # gates like ignored_channels; the standalone Web-API path may only have a
    # comma-separated token list.  ``_send_via_adapter`` tries the in-process
    # adapter first and falls back to the registry standalone sender for
    # out-of-process cron runs, preserving MEDIA delivery on the fallback
    # (media-bearing sends were already intercepted by the branch above).
    if platform == Platform.SLACK:
        last_result = None
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            result = await _send_via_adapter(
                platform,
                pconfig,
                chat_id,
                chunk,
                thread_id=thread_id,
                media_files=media_files if is_last else [],
                force_document=force_document,
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            last_result = result
        return last_result

    # --- Non-media platforms ---
    if media_files and not message.strip():
        return {
            "error": (
                f"send_message MEDIA delivery is currently only supported for discord, signal and slack; "
                f"target {platform.value} had only media attachments"
            )
        }
    warning = None
    if media_files:
        warning = (
            f"MEDIA attachments were omitted for {platform.value}; "
            "native send_message media delivery is currently only supported for discord, signal and slack"
        )

    last_result = None
    for chunk in chunks:
        if platform == Platform.SIGNAL:
            result = await _send_signal(pconfig.extra, chat_id, chunk)
        else:
            from gateway.platform_registry import platform_registry

            entry = platform_registry.get(platform_name)
            handler = entry.send_message_handler if entry is not None else None
            if handler is not None:
                try:
                    import inspect

                    result = handler(args or {}, chat_id, platform_name, pconfig)
                    if inspect.isawaitable(result):
                        result = await result
                    return result
                except Exception as e:
                    return {"error": f"Plugin send_message handler failed: {e}"}
            # Plugin platform: route through the gateway's live adapter if
            # available, otherwise the plugin's standalone_sender_fn.
            result = await _send_via_adapter(
                platform,
                pconfig,
                chat_id,
                chunk,
                thread_id=thread_id,
                media_files=media_files,
                force_document=force_document,
            )

        if isinstance(result, dict) and result.get("error"):
            return result
        last_result = result

    if warning and isinstance(last_result, dict) and last_result.get("success"):
        warnings = list(last_result.get("warnings", []))
        warnings.append(warning)
        last_result["warnings"] = warnings
    return last_result



# wired via standalone_sender_fn and reached through _registry_standalone_send. #41112.


async def _resolve_slack_user_target(token, chat_id):
    """Resolve a Slack user target to a D... DM conversation ID.

    ``chat_id`` may be a Slack conversation ID (C/G/D...) — returned unchanged —
    or an internal user target (``user:U...`` / ``user_name:<handle>``). User
    targets are opened as DMs via conversations.open because Slack
    chat.postMessage requires a conversation ID. ``user_name:`` targets are
    first resolved to a user ID through users.list (stable handle match only).

    Returns ``(chat_id, None)`` on success or ``(None, error_dict)`` on failure.
    """
    if not (chat_id.startswith("user:") or chat_id.startswith("user_name:")):
        return chat_id, None
    try:
        import aiohttp
    except ImportError:
        return None, {"error": "aiohttp not installed. Run: pip install aiohttp"}
    try:
        from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
        _proxy = resolve_proxy_url()
        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
        base_url = "https://slack.com/api"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        async def post_api(session, method, payload):
            async with session.post(f"{base_url}/{method}", headers=headers, json=payload, **_req_kw) as resp:
                return await resp.json()

        async def resolve_user_name(session, name):
            query = name.strip().lstrip("@").lower()
            matches = []
            cursor = None
            for _page in range(20):
                payload = {"limit": 200}
                if cursor:
                    payload["cursor"] = cursor
                data = await post_api(session, "users.list", payload)
                if not data.get("ok"):
                    return None, f"Slack users.list error: {data.get('error', 'unknown')}"
                for member in data.get("members", []):
                    if member.get("deleted") or member.get("is_bot"):
                        continue
                    # ``@name`` should match the stable Slack handle only. Display
                    # and real names are mutable/non-unique enough that using them
                    # could DM the wrong person with sensitive content.
                    if str(member.get("name", "")).strip().lower() == query:
                        matches.append(member)
                cursor = (data.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
            if not matches:
                return None, f"Could not resolve Slack user '@{name}'."
            if len(matches) > 1:
                return None, f"Slack user '@{name}' matched multiple Slack users. Use a Slack user ID instead."
            return matches[0].get("id"), None

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), **_sess_kw) as session:
            if chat_id.startswith("user_name:"):
                user_id, error = await resolve_user_name(session, chat_id[len("user_name:"):])
                if error:
                    return None, _error(error)
                chat_id = f"user:{user_id}"

            user_id = chat_id[len("user:"):]
            opened = await post_api(session, "conversations.open", {"users": user_id})
            if not opened.get("ok"):
                return None, _error(
                    f"Slack conversations.open error: {opened.get('error', 'unknown')}. "
                    "Check bot permissions (im:write)."
                )
            dm_id = (opened.get("channel") or {}).get("id")
            if not dm_id:
                return None, _error("Slack conversations.open did not return a DM channel ID")
            return dm_id, None
    except Exception as e:
        return None, _error(f"Slack DM resolution failed: {e}")


async def _send_signal(extra, chat_id, message, media_files=None):
    """Send via signal-cli JSON-RPC API.

    Supports both text-only and text-with-attachments (images/audio/documents).
    Multi-attachment sends are chunked into batches of
    SIGNAL_MAX_ATTACHMENTS_PER_MSG and metered by the process-wide
    SignalAttachmentScheduler — same bucket the gateway adapter uses, so
    sends from this tool and inbound-driven replies share rate-limit state.
    """
    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed"}

    from gateway.platforms.signal_rate_limit import (
        SIGNAL_BATCH_PACING_NOTICE_THRESHOLD,
        SIGNAL_MAX_ATTACHMENTS_PER_MSG,
        SIGNAL_RATE_LIMIT_MAX_ATTEMPTS,
        _extract_retry_after_seconds,
        _format_wait,
        _is_signal_rate_limit_error,
        _signal_send_timeout,
        get_scheduler,
    )
    from gateway.platforms.signal_format import markdown_to_signal

    try:
        http_url = extra.get("http_url", "http://127.0.0.1:8080").rstrip("/")
        account = extra.get("account", "")
        if not account:
            return {"error": "Signal account not configured"}

        valid_media = media_files or []
        attachment_paths = []
        for media_path, _is_voice in valid_media:
            if os.path.exists(media_path):
                attachment_paths.append(media_path)
            else:
                logger.warning("Signal media file not found, skipping: %s", media_path)

        # Chunk attachments. With no attachments we still emit one batch
        # (text only). With attachments, the text rides on batch #0 so the
        # caption isn't repeated across every chunk.
        if attachment_paths:
            att_batches = [
                attachment_paths[i:i + SIGNAL_MAX_ATTACHMENTS_PER_MSG]
                for i in range(0, len(attachment_paths), SIGNAL_MAX_ATTACHMENTS_PER_MSG)
            ]
        else:
            att_batches = [[]]

        plain_text, text_styles = markdown_to_signal(message)

        async def _post(batch_attachments, batch_message):
            params = {"account": account, "message": batch_message}
            if batch_message and text_styles:
                if len(text_styles) == 1:
                    params["textStyle"] = text_styles[0]
                else:
                    params["textStyles"] = text_styles
            if chat_id.startswith("group:"):
                params["groupId"] = chat_id[6:]
            else:
                params["recipient"] = [chat_id]
            if batch_attachments:
                params["attachments"] = batch_attachments

            payload = {
                "jsonrpc": "2.0",
                "method": "send",
                "params": params,
                "id": f"send_{int(time.time() * 1000)}",
            }
            timeout = _signal_send_timeout(len(batch_attachments) if batch_attachments else 0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{http_url}/api/v1/rpc", json=payload)
                resp.raise_for_status()
                return resp.json()

        async def _send_inline_notice(text: str) -> None:
            """Best-effort one-shot RPC for a user-facing pacing notice."""
            notice_params = {"account": account, "message": text}
            if chat_id.startswith("group:"):
                notice_params["groupId"] = chat_id[6:]
            else:
                notice_params["recipient"] = [chat_id]
            try:
                async with httpx.AsyncClient(timeout=30.0) as _client:
                    await _client.post(
                        f"{http_url}/api/v1/rpc",
                        json={
                            "jsonrpc": "2.0",
                            "method": "send",
                            "params": notice_params,
                            "id": f"notice_{int(time.time() * 1000)}",
                        },
                    )
            except Exception as _e:
                logger.warning("Signal: inline notice failed: %s", _e)

        scheduler = get_scheduler()
        logger.info(
            "send_message Signal: scheduler state=%s, %d attachment(s) in %d batch(es)",
            scheduler.state(), len(attachment_paths), len(att_batches),
        )
        failed_batches: list[int] = []
        for idx, att_batch in enumerate(att_batches):
            n = len(att_batch)
            if n > 0:
                estimated = scheduler.estimate_wait(n)
                if estimated >= SIGNAL_BATCH_PACING_NOTICE_THRESHOLD:
                    await _send_inline_notice(
                        f"(More images coming — pausing ~{_format_wait(estimated)} "
                        f"for Signal rate limit, batch {idx + 1}/{len(att_batches)}.)"
                    )

            batch_message = plain_text if idx == 0 else ""

            for attempt in range(1, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS + 1):
                try:
                    await scheduler.acquire(n)
                    _rpc_t0 = time.monotonic()
                    data = await _post(att_batch, batch_message)
                    _rpc_duration = time.monotonic() - _rpc_t0
                    if "error" not in data:
                        await scheduler.report_rpc_duration(_rpc_duration, n)
                        break

                    err = data["error"]

                    if not _is_signal_rate_limit_error(err):
                        return _error(f"Signal RPC error on batch {idx + 1}/{len(att_batches)}: {err}")

                    server_retry_after = _extract_retry_after_seconds(err)
                    scheduler.feedback(server_retry_after, n)

                    if attempt >= SIGNAL_RATE_LIMIT_MAX_ATTEMPTS:
                        failed_batches.append(idx + 1)
                        logger.error(
                            "Signal: rate-limit retries exhausted on batch %d/%d "
                            "(%d attachments lost, server retry_after=%s)",
                            idx + 1, len(att_batches), n,
                            f"{server_retry_after:.0f}s" if server_retry_after else "unknown",
                        )
                        break
                    logger.warning(
                        "Signal: rate-limited on batch %d/%d "
                        "(attempt %d/%d, server retry_after=%s); "
                        "scheduler will pace the retry",
                        idx + 1, len(att_batches),
                        attempt, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS,
                        f"{server_retry_after:.0f}s" if server_retry_after else "unknown",
                    )
                except Exception as e:
                    if attempt >= SIGNAL_RATE_LIMIT_MAX_ATTEMPTS:
                        failed_batches.append(idx + 1)
                        logger.error(
                            "Signal: send error on batch %d/%d after %d attempts: %s",
                            idx + 1, len(att_batches), attempt, str(e)
                        )
                        break
                    logger.warning(
                        "Signal: transient error on batch %d/%d (attempt %d/%d): %s; will retry",
                        idx + 1, len(att_batches), attempt, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS, str(e)
                    )

        warnings = []
        if len(attachment_paths) < len(valid_media):
            warnings.append("Some media files were skipped (not found on disk)")
        if failed_batches:
            warnings.append(
                f"Signal rate-limited {len(failed_batches)} batch(es) "
                f"(#{', #'.join(str(b) for b in failed_batches)})"
            )

        if failed_batches and len(failed_batches) == len(att_batches):
            return _error(
                f"Signal: every batch ({len(att_batches)}) hit rate limit; "
                f"no attachments delivered"
            )

        result = {"success": True, "platform": "signal", "chat_id": _display_chat_id("signal", chat_id)}
        if warnings:
            result["warnings"] = warnings
        return result
    except Exception as e:
        return _error(f"Signal send failed: {e}")














def _check_send_message():
    """Gate send_message on gateway running (always available on messaging platforms).

    Also passes for kanban workers — the dispatcher sets ``SON_OF_ANTON_KANBAN_TASK``
    on every spawned worker, but those workers run with the assignee profile's
    ``SON_OF_ANTON_HOME`` which has no ``gateway.pid``, so the gateway-running check
    would fail even though the parent gateway is alive. Honoring the env var
    lets workers call ``send_message`` to deliver rich content directly to the
    originating chat (paired with ``kanban_complete`` for the short notifier
    summary), which is the canonical pattern for any worker that needs to
    reply with more than the ~200-char first-line truncation the kanban
    notifier applies.
    """
    if os.environ.get("SON_OF_ANTON_KANBAN_TASK"):
        return True
    from gateway.session_context import get_session_env
    platform = get_session_env("SON_OF_ANTON_SESSION_PLATFORM", "")
    if platform and platform != "local":
        return True
    try:
        from gateway.status import is_gateway_running
        return is_gateway_running()
    except Exception:
        return False


# --- Registry ---
from tools.registry import tool_error

# NOTE: ``send_message`` is intentionally NOT registered as an agent-callable
# model tool. The agent should not decide on its own to fire off cross-platform
# messages or reactions. The send engine in this module (``_send_to_platform``,
# ``_send_via_adapter``, ``_parse_target_ref``, the per-platform ``_send_*``
# helpers) remains the shared transport used by:
#   - cron delivery (cron/scheduler.py)
#   - the ``son-of-anton send`` CLI command (son_of_anton_cli/send_cmd.py)
#   - the gateway kanban notifier (dashboard-toggled, outside agent control)
#   - the standalone MCP server (mcp_serve.py), which is an opt-in surface
# Those callers import the helpers directly; none of them need the registry
# entry.
