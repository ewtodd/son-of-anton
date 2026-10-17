"""Auto-generate short session titles from the user's opening message.

Two stages, both off the critical path:

1. **Instant** — a deterministic title derived from the first user message,
   written before the model is even called. Costs nothing, cannot fail, and
   means a session is named the moment it starts instead of after the first
   turn finishes (which measured p50 151s / p90 1212s on real sessions).
2. **Upgrade** — one small-model call that replaces the derived title with a
   proper one. Runs on a cheap/fast tier, with thinking disabled and the
   response constrained to a JSON object, so there is no reasoning preamble to
   strip and nothing to parse out of prose.

Provenance (``derived`` < ``llm`` < ``user``) is enforced by the storage layer,
so stage 2 can only ever replace stage 1, and neither can replace a name the
user typed.
"""

import json
import logging
import re
import threading
from typing import Any, Callable, Optional

from agent.auxiliary_client import call_llm
from agent.context_compactor import LEGACY_SUMMARY_PREFIX
from agent.message_content import flatten_message_text

logger = logging.getLogger(__name__)

# Callback signature: (task_name, exception) -> None. Used to surface
# auxiliary failures to the user through AIAgent._emit_auxiliary_failure
# instead of piling up as NULL session titles.
FailureCallback = Callable[[str, BaseException], None]

# Callback signature: (title, source) -> None, where source is the provenance
# the title was persisted under (``derived`` for the instant slice of the user's
# own words, ``llm`` for the model's upgrade of it).
TitleCallback = Callable[[str, str], None]

# Validation callback: () -> bool. Called right before the LLM request in
# generate_title(). Return False to skip — e.g. the user switched models
# after this background thread captured its runtime snapshot, and sending
# the request would reload a model the runtime already evicted.
RuntimeValidator = Callable[[], bool]

# Cap on the text handed to the model: a title needs the opening intent, not
# a pasted stack trace.
MAX_TITLE_INPUT_CHARS = 1000

# Cap on the instant derived title. Deliberately shorter than the model's
# budget: a raw sentence fragment reads worse the longer it runs.
MAX_DERIVED_TITLE_CHARS = 48

# Upper bound on accepted title word count. Titling is a 3-7 word task; a
# small model sometimes ignores the task and answers the user's message
# instead — that answer must never become the session title (see the
# answer-shaped output guard in generate_title). 12 leaves headroom for
# legitimate wordy titles while excluding full-sentence answers.
_MAX_TITLE_WORDS = 12

_TITLE_PROMPT_TEMPLATE = (
    "You name chat sessions. Given the user's opening message, write a title "
    "that lets them find this conversation again in a list.\n\n"
    "Rules:\n"
    "- 3 to 7 words, sentence case (capitalize only the first word and proper nouns).\n"
    "- Name what the user wants DONE, not that they asked a question.\n"
    "- Keep technical terms, filenames, numbers, and error codes exact.\n"
    "- Drop filler words: the, this, my, a, an.\n"
    "- No trailing punctuation, no quotes, no tool names, no 'Title:' prefix.\n"
    "- Never answer the message. Name it.\n"
    "- Always produce something, even for a bare greeting.\n"
    "- Write the title in the same language as the user's message.\n"
    'Good: {"title": "Fix login button on mobile"}\n'
    'Good: {"title": "Postgres connection pool exhaustion"}\n'
    'Good: {"title": "Friendly greeting"}\n'
    'Too vague: {"title": "Code changes"}\n'
    'Too long: {"title": "Investigate and fix the issue where the login button '
    'does not respond on mobile devices"}\n\n'
    'Reply with JSON only: {"title": "..."}'
)

# JSON schema constraining the response to a single title field. Removes the
# whole class of "model answered the prompt instead of titling it" failures
# that produced titles like "<title>...</title>" and "User: Yep, that's the
# catch —" in real session history.
_TITLE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "session_title",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        },
    },
}

# Control-tag wrappers that surround machine-authored content inside what is
# nominally a "user" message. Titling from these is what produces a session
# named after a slash command or an injected reminder rather than the user's
# actual request; they are stripped (and titling continues) rather than
# refused outright.
_CONTROL_WRAPPERS = (
    ("<command-message>", "</command-message>"),
    ("<command-name>", "</command-name>"),
    ("<command-args>", "</command-args>"),
    ("<local-command-caveat>", "</local-command-caveat>"),
    ("<local-command-stderr>", "</local-command-stderr>"),
    ("<local-command-stdout>", "</local-command-stdout>"),
    ("<task-notification>", "</task-notification>"),
    ("<system-reminder>", "</system-reminder>"),
    ("<ide_opened_file>", "</ide_opened_file>"),
    ("<ide_selection>", "</ide_selection>"),
)

# Son of Anton' own machine-authored openers. A compaction handoff or a resumed
# session must not be titled after the scaffolding that carried it. The legacy
# summary prefix comes from the compactor rather than a fourth local copy —
# compaction still emits it, and a session named after it is named after us.
_MACHINE_PREFIXES = (
    "[CONTEXT COMPACTION",
    LEGACY_SUMMARY_PREFIX,
    "[Runtime note:",
    "[System note:",
    "[SYSTEM]",
)


def strip_control_wrappers(text: str) -> str:
    """Remove leading machine-authored control wrappers, including nested ones.

    Loops so ``<command-message><command-name>/work</command-name></command-message>``
    reduces to the prose the user actually typed. Unlike a refusal check, this
    still yields usable text, so a slash-command turn gets a real title instead
    of staying untitled.
    """
    if not text:
        return ""
    current = text.strip()
    # Bounded: each pass must remove at least one wrapper or we stop.
    for _ in range(len(_CONTROL_WRAPPERS) * 2):
        stripped = current
        for open_tag, close_tag in _CONTROL_WRAPPERS:
            if not stripped.lower().startswith(open_tag):
                continue
            end = stripped.lower().find(close_tag)
            if end == -1:
                # Unterminated wrapper: drop the opening tag and keep the body.
                stripped = stripped[len(open_tag):].strip()
            else:
                inner = stripped[len(open_tag):end].strip()
                rest = stripped[end + len(close_tag):].strip()
                # Prefer the trailing prose when there is any; otherwise the
                # wrapper's own body is the only content we have.
                stripped = (rest or inner).strip()
            break
        if stripped == current:
            break
        current = stripped
    return current


def _summarize_user_message(user_message: str) -> str:
    """Reduce a user turn to the text worth titling.

    A ``/skill`` invocation recorded before skill commands were removed
    embeds the whole skill body, so feeding it to the titler verbatim titles
    the session after the *skill's* prose — "Kick off a task in a fresh
    isolated git worktree" — not after the user's request. Reuse the canonical
    scaffolding parser so the model sees ``/work — fix the title leak``
    instead, then strip any control wrappers left around it.
    """
    if not user_message:
        return ""
    described = None
    try:
        from son_of_anton_state_common import describe_skill_invocation

        described = describe_skill_invocation(user_message)
    except Exception:
        logger.debug("Skill-scaffolding summary failed; titling raw", exc_info=True)
    text = described if described is not None else user_message
    return strip_control_wrappers(text)


def is_titleable_user_message(user_message: str) -> bool:
    """Return whether *user_message* carries real user intent to title from.

    False for machine-authored openers (compaction handoffs, runtime notes) and
    for turns that reduce to nothing once control scaffolding is stripped.
    """
    if not isinstance(user_message, str) or not user_message.strip():
        return False
    for prefix in _MACHINE_PREFIXES:
        if user_message.lstrip().startswith(prefix):
            return False
    return bool(_summarize_user_message(user_message).strip())


def derive_title(user_message: str) -> Optional[str]:
    """Build an instant title from the user's message. No model, never fails.

    This is what the user sees within milliseconds of sending their first
    message. It is intentionally dumb — first meaningful line, trimmed to a
    word boundary — because its job is to beat the model to the screen, not to
    beat it on quality. The model's title replaces it moments later.
    """
    text = _summarize_user_message(user_message)
    if not text:
        return None
    # First non-empty line: a pasted log or a multi-paragraph brief still gets
    # named after its opening intent.
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if not line:
        return None
    line = " ".join(line.split())
    if len(line) > MAX_DERIVED_TITLE_CHARS:
        cut = line[:MAX_DERIVED_TITLE_CHARS]
        # Prefer a word boundary so the title doesn't end mid-token.
        space = cut.rfind(" ")
        if space > MAX_DERIVED_TITLE_CHARS // 2:
            cut = cut[:space]
        line = cut.rstrip(" ,.;:—-") + "…"
    return line or None


def _extract_title_text(content: str) -> str:
    """Pull the title out of a model response.

    The JSON schema makes the object shape the expected case, but not every
    provider honors ``response_format``; fall back through a loose JSON scan
    and finally to first-line prose so a non-compliant provider still titles.
    """
    if not content:
        return ""
    raw = content.strip()
    # Fenced JSON from providers that wrap structured output in markdown.
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("title"), str):
            return parsed["title"].strip()
    except (ValueError, TypeError):
        pass
    # Loose scan: a compliant object embedded in surrounding chatter.
    match = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    if match:
        try:
            return json.loads(f'"{match.group(1)}"').strip()
        except ValueError:
            return match.group(1).strip()
    # Truncated structured output (max_tokens cut the reply mid-value, so
    # there is no closing quote): salvage the value up to the first quote /
    # brace / end of text. Degenerate output is rejected later — this just
    # keeps the JSON envelope out of the candidate title.
    match = re.search(r'"title"\s*:\s*"?((?:[^"\\{}]|\\.)*)', raw)
    if match:
        value = match.group(1).strip()
        if value:
            try:
                return json.loads(f'"{value}"').strip()
            except ValueError:
                return value.strip()
    # Prose fallback. Reuse the canonical scrubber so reasoning-model output
    # (<think>…) can't leak into a title, then keep the first real line.
    try:
        from agent.agent_runtime_helpers import strip_think_blocks

        raw = strip_think_blocks(None, raw).strip()
    except Exception:
        logger.debug("strip_think_blocks unavailable for title output", exc_info=True)
    raw = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    if raw.lower().startswith("title:"):
        raw = raw[6:].strip()
    return raw.strip("\"'").strip()


def _clean_title(text: str) -> Optional[str]:
    """Normalize a model-produced title, or None when nothing usable remains."""
    title = " ".join((text or "").split())
    title = title.strip("\"'").strip()
    if title.lower().startswith("title:"):
        title = title[6:].strip()
    # Trailing sentence punctuation reads wrong in a sidebar list.
    title = title.rstrip(".!,;:")
    if not title:
        return None
    if len(title) > 80:
        title = title[:77].rstrip() + "..."
    return title


def _is_degenerate_title(title: str) -> bool:
    """True for token-loop garbage, never a real session title.

    A tiny title model stuck in a repetition loop emits runs of the same
    word ("Response Response Response ..."), and a truncated structured
    reply can leak its JSON envelope. Both must be rejected rather than
    persisted as the visible sidebar name.
    """
    if any(ch in title for ch in "{}"):
        return True
    words = [w.lower() for w in title.split()]
    if len(words) >= 4:
        best = 1
        run = 1
        for i in range(1, len(words)):
            run = run + 1 if words[i] == words[i - 1] else 1
            if run > best:
                best = run
        if best >= 4:
            return True
    return False


def generate_title(
    user_message: str,
    timeout: Optional[float] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> Optional[str]:
    """Generate a session title from the user's opening message.

    Runs on the ``title_generation`` auxiliary task, with the response
    constrained to ``{"title": "..."}`` so there is no preamble to strip.

    Titles come from the user's message alone: waiting for the assistant is
    what made this slow, and the opening message already states the intent
    worth naming.

    ``failure_callback`` is invoked with ``(task, exception)`` when the
    auxiliary call raises — the caller typically wires this to
    ``AIAgent._emit_auxiliary_failure`` so the user sees a warning instead
    of silently accumulating untitled sessions.

    ``runtime_validator`` is called right before the LLM request. If it
    returns False (e.g. the user's model was switched since the background
    thread captured its runtime snapshot), the call is skipped silently —
    no request is sent, so a stale title request can't reload a model the
    runtime already unloaded.
    """
    if runtime_validator is not None:
        try:
            if not runtime_validator():
                logger.debug("Title generation skipped: runtime validator returned False")
                return None
        except Exception:
            # Fail open: a broken validator must not disable titling.
            logger.debug("Title runtime validator raised; proceeding", exc_info=True)

    user_snippet = _summarize_user_message(user_message)[:MAX_TITLE_INPUT_CHARS]
    if not user_snippet.strip():
        return None

    messages = [
        {"role": "system", "content": _TITLE_PROMPT_TEMPLATE},
        {"role": "user", "content": user_snippet},
    ]

    try:
        response = call_llm(
            task="title_generation",
            messages=messages,
            # A title is a handful of tokens; a higher ceiling lets a chatty
            # model burn seconds generating prose we then throw away.
            max_tokens=64,
            temperature=0.3,
            timeout=timeout,
            main_runtime=main_runtime,
            extra_body={"response_format": _TITLE_RESPONSE_FORMAT},
        )
        content = response.choices[0].message.content or ""
        title = _clean_title(_extract_title_text(content))
        # Answer-shaped output guard: titling is a 3-7 word task, so a title
        # with many words is a model that ignored the task and answered
        # the user's message instead ("I don't have context on X — that's
        # not something I recognize..."). Truncating would store half an
        # assistant blob as the session title, which is still an assistant
        # blob — reject instead so the caller retries on the next exchange
        # (maybe_auto_title fires for the first two exchanges).
        if title is not None and len(title.split()) > _MAX_TITLE_WORDS:
            logger.debug(
                "Rejecting answer-shaped title output (%d words > %d)",
                len(title.split()), _MAX_TITLE_WORDS,
            )
            return None
        if title is not None and _is_degenerate_title(title):
            logger.debug("Rejecting degenerate title output: %r", title)
            return None
        return title
    except Exception as e:
        # Log at WARNING so this shows up in agent.log without debug mode.
        # Full detail at debug level for operators who need the stack.
        logger.warning("Title generation failed: %s", e)
        logger.debug("Title generation traceback", exc_info=True)
        if failure_callback is not None:
            try:
                failure_callback("title generation", e)
            except Exception:
                logger.debug("Title generation failure_callback raised", exc_info=True)
        return None


def _persist_session_title(session_db, session_id, title, *, source, dedupe=True):
    """Persist a title at *source* authority, recovering from name collisions.

    The write goes through ``set_auto_title`` (precedence check + write in one
    transaction) so a manual ``/title`` set while generation was in flight is
    never overwritten. ``ValueError`` means the name is taken by an unrelated
    session (the unique-title index); rather than leave the session untitled,
    append a ``#N`` suffix via ``get_next_title_in_lineage``.

    ``dedupe=False`` re-raises that collision instead. The derived title is the
    one write on the turn's critical path, and it is also the one that collides
    constantly — it is a slice of the user's own words, and people open sessions
    with "hi" and "help me debug this". Scanning the lineage for the next free
    "hi #N" is a widening scan, run inline, for a name the model replaces a
    second later. The background stage picks the collision back up, so nothing
    is lost by declining it here.

    Returns the title actually persisted, or None when a higher-authority
    title already held the row (nothing was written).
    """
    def _set(candidate):
        if not session_db.set_auto_title(session_id, candidate, source=source):
            logger.debug(
                "Skipping %s title: a higher-authority title already holds "
                "session %s",
                source, session_id,
            )
            return None
        return candidate

    try:
        return _set(title)
    except ValueError:
        if not dedupe:
            raise
        deduped = session_db.get_next_title_in_lineage(title)
        if not deduped or deduped == title:
            raise
        return _set(deduped)


def apply_instant_title(
    session_db,
    session_id: str,
    user_message: str,
    title_callback: Optional[TitleCallback] = None,
) -> Optional[str]:
    """Write the derived title synchronously. Cheap enough to run inline.

    Returns the title written, or None when nothing was written (no usable
    text, or the session already carries a title of at least ``derived``
    authority). Never raises: a titling failure must not affect the turn.
    """
    if not session_db or not session_id:
        return None
    try:
        if not is_titleable_user_message(user_message):
            return None
        title = derive_title(user_message)
        if not title:
            return None
        persisted = _persist_session_title(
            session_db, session_id, title, source="derived", dedupe=False
        )
        if persisted and title_callback is not None:
            try:
                title_callback(persisted, "derived")
            except Exception:
                logger.debug("Instant-title callback failed", exc_info=True)
        return persisted
    except Exception:
        logger.debug("Instant title failed", exc_info=True)
        return None


def auto_title_session(
    session_db,
    session_id: str,
    user_message: str,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> None:
    """Generate and store the model title for a session.

    Called on a background thread. Silently skips if:
    - session_db is None
    - the session already carries an ``llm`` or ``user`` title
    - title generation fails
    - runtime_validator returns False (model was switched)

    Never lets an exception escape: this is a daemon-thread target, and an
    escaping exception would spray a raw traceback into the user's terminal
    via the default threading excepthook.
    """
    try:
        _auto_title_session(
            session_db,
            session_id,
            user_message,
            failure_callback=failure_callback,
            main_runtime=main_runtime,
            title_callback=title_callback,
            runtime_validator=runtime_validator,
        )
    except Exception as e:
        # WARNING (not debug) so operators see it in agent.log.
        logger.warning("Auto-title failed (harmless): %s", e)
        logger.debug("Auto-title traceback", exc_info=True)
        if failure_callback is not None:
            try:
                failure_callback("title generation", e)
            except Exception:
                logger.debug("Auto-title failure_callback raised", exc_info=True)


def _auto_title_session(
    session_db,
    session_id: str,
    user_message: str,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> None:
    """Body of :func:`auto_title_session` — see its docstring."""
    if not session_db or not session_id:
        return

    # Skip when a title of at least LLM authority is already stored. A derived
    # title is expected here — upgrading it is the whole point of this call.
    try:
        existing_source = session_db.get_session_title_source(session_id)
        if existing_source is not None and existing_source != "derived":
            return
    except Exception:
        return

    # This runs on a bare daemon thread spawned after the turn's accounting
    # context was reset, so publish it here: the title call's token usage is
    # then recorded against this session (task='title_generation').
    from agent.aux_accounting import set_accounting_context

    set_accounting_context(session_db, session_id)

    title = generate_title(
        user_message,
        failure_callback=failure_callback,
        main_runtime=main_runtime,
        runtime_validator=runtime_validator,
    )
    source = "llm"
    if not title:
        # No model title, so the derived one has to hold — and it may never have
        # been written, since the inline attempt declines a name collision
        # rather than scan the lineage on the turn's critical path. Off that
        # path the scan is affordable, so spend it here and leave the session
        # named rather than nameless.
        title = derive_title(user_message)
        source = "derived"
        if not title:
            return

    try:
        persisted = _persist_session_title(session_db, session_id, title, source=source)
        if persisted is None:
            return
        logger.debug("Auto-generated session title: %s", persisted)
        if title_callback is not None:
            try:
                title_callback(persisted, source)
            except Exception:
                logger.debug("Auto-title callback failed", exc_info=True)
    except Exception as e:
        logger.debug("Failed to set auto-generated title: %s", e)


def _is_real_user_turn(message: Any) -> bool:
    """Whether a history entry is a question a person actually asked.

    Son of Anton persists a lot of machinery under ``role="user"`` — compaction
    handoffs, model-switch markers, background-process notices — because strict
    OpenAI-compatible providers reject a system message that isn't first.
    Counting those as turns is what made a session that merely *opened* with one
    look like it was already past the point where titling applies.

    A multimodal turn is judged on its text, so "here's a screenshot, fix the
    login" counts as the real question it is.
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")

    return is_titleable_user_message(
        content if isinstance(content, str) else flatten_message_text(content)
    )


def _session_is_untitled(session_db, session_id: str) -> bool:
    """Whether the session still carries no title of any provenance.

    Titling normally reads the opening message and nothing else, but an opener
    isn't always titleable: an image with no caption, a compaction handoff, a
    bare slash command. Those sessions stayed nameless for life — the same guard
    that stops us re-titling on every turn also stopped us ever trying again.
    This reopens the question on later turns, and only while the answer is still
    missing, so a named session asks nothing and pays nothing.

    Answers False when it can't tell: an unreadable title is not a reason to
    start spending a model call per turn.
    """
    getter = getattr(session_db, "get_session_title", None)
    if not callable(getter):
        return False
    try:
        return not str(getter(session_id) or "").strip()
    except Exception:
        logger.debug("Untitled check failed for %s", session_id, exc_info=True)
        return False


def maybe_auto_title(
    session_db,
    session_id: str,
    user_message: str,
    conversation_history: Optional[list] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> None:
    """Title a session from its opening message: instant, then upgraded.

    Call this at the START of a turn, before the model is invoked. The derived
    title is written inline (sub-millisecond) and the model upgrade is forked
    onto a daemon thread, so nothing here is on the critical path.

    Only acts on the session's opening exchange, and only when the message
    carries real user intent (machine-authored compaction handoffs are skipped).
    """
    if not session_db or not session_id or not user_message:
        return

    # Count the real questions behind us to detect the opening turn.
    # ``conversation_history`` is the state BEFORE this turn's message is
    # appended when called from the turn prologue, and after it when called
    # post-response, so accept both.
    #
    # Two things have to be true to skip: we are past the opening turn AND the
    # session already has a name. Either alone gets it wrong. The count alone
    # left a session that opened with machinery permanently nameless, because
    # nothing reconsidered it. The title alone would never title at all on a
    # store too old to report one.
    user_msg_count = sum(1 for m in (conversation_history or []) if _is_real_user_turn(m))
    if user_msg_count > 1 and not _session_is_untitled(session_db, session_id):
        return

    if not is_titleable_user_message(user_message):
        return

    apply_instant_title(session_db, session_id, user_message, title_callback)

    thread = threading.Thread(
        target=auto_title_session,
        args=(session_db, session_id, user_message),
        kwargs={
            "failure_callback": failure_callback,
            "main_runtime": main_runtime,
            "title_callback": title_callback,
            "runtime_validator": runtime_validator,
        },
        daemon=True,
        name="auto-title",
    )
    thread.start()
