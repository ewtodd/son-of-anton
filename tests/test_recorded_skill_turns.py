"""Sessions recorded while skills were invocable still read as what the user typed.

A ``/skill`` turn (and a stacked ``/a /b`` or bundle turn) was stored as the
expanded message: an activation note, every skill body, then the instruction.
Nothing produces that shape any more, but databases written before the removal
hold it, and every surface that summarizes a stored user turn (listing
previews, titles) must keep showing ``/work — fix it`` rather than the skill's
opening prose. The fixtures below are that historical on-disk format.
"""

from __future__ import annotations

from son_of_anton_state import SessionDB

_BODY = (
    "# Work Skill\n\n"
    + "Kick off a task in a fresh isolated git worktree and keep it there. " * 30
)

SINGLE_SKILL_TURN = (
    '[IMPORTANT: The user has invoked the "work" skill, indicating they want '
    "you to follow its instructions. The full skill content is loaded below.]\n\n"
    f"{_BODY}\n\n"
    "[Skill directory: /home/u/.son-of-anton/skills/software-development/work]\n\n"
    "The user has provided the following instruction alongside the skill "
    "invocation: fix the title leak"
)

STACKED_SKILL_TURN = (
    '[IMPORTANT: The user has invoked the "/spike /plan" stacked skill bundle, '
    "loading 2 skills together. Treat every skill below as active guidance for "
    "this turn.]\n\n"
    "Skills loaded: spike, plan\n\n"
    "User instruction: try the cache\n\n"
    '[Loaded as part of the stacked skill invocation "spike".]\n\n'
    f"{_BODY}"
)


def _listed_preview(content: str) -> str:
    db = SessionDB()
    try:
        db.create_session("s1", "cli")
        db.append_message("s1", "user", content)
        db.append_message("s1", "assistant", "ok")
        (row,) = [r for r in db.list_sessions_rich(limit=10) if r["id"] == "s1"]
        return row["preview"]
    finally:
        db.close()


def test_a_recorded_skill_turn_previews_as_typed() -> None:
    assert _listed_preview(SINGLE_SKILL_TURN) == "/work — fix the title leak"


def test_a_recorded_stacked_skill_turn_previews_as_typed() -> None:
    # The listing query flattens newlines before parsing, which hides the
    # stacked format's instruction marker; the typed commands still show and
    # the skill body never does.
    preview = _listed_preview(STACKED_SKILL_TURN)
    assert preview.startswith("/spike /plan")
    assert "worktree" not in preview and "IMPORTANT" not in preview


def test_an_ordinary_turn_previews_unchanged() -> None:
    assert _listed_preview("why does the build fail") == "why does the build fail"


def test_recorded_skill_turns_title_from_the_instruction() -> None:
    from agent.title_generator import derive_title

    assert derive_title(SINGLE_SKILL_TURN) == "/work — fix the title leak"
    assert derive_title(STACKED_SKILL_TURN) == "/spike /plan — try the cache"
