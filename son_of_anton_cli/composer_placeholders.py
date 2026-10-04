"""Task-oriented placeholder prompts shown in the empty composer.

Short example actions that nudge a new user toward high-value first moves,
inspired by opencode/codex rotating placeholders. Kept generic so they fit
any project or none — Son of Anton is not a coding-only agent.
"""

import random

COMPOSER_PLACEHOLDERS = [
    "Ask anything, or type / for commands…",
    "Summarize what's in this folder",
    "Draft a reply to the last email in my inbox",
    "Plan a feature, then build it step by step",
    "Find and fix a failing test",
    "Research this topic and write me a brief",
    "What changed in this repo recently?",
    "Turn these notes into a to-do list",
    "Explain this error and how to fix it",
    "Set a reminder or schedule a recurring task",
    "Type / to browse commands, or Ctrl+P for the palette",
]


def get_random_composer_placeholder() -> str:
    """Return a rotating task-oriented placeholder for the empty composer."""
    return random.choice(COMPOSER_PLACEHOLDERS)
