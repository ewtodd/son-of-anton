"""
Dump command for son-of-anton CLI.

Outputs a compact, plain-text summary of the user's Son of Anton setup
that can be copy-pasted into Discord/GitHub/Slack for support context.
No ANSI colors, no checkmarks — just data.
"""




def _redact(value: str) -> str:
    """Redact all but first 4 and last 4 chars.

    Thin wrapper over :func:`agent.redact.mask_secret`. Returns ``""`` for
    an empty value (matches the historical behavior of this helper —
    ``son-of-anton dump`` formats empty values as blank, not as ``"(not set)"``).
    """
    from agent.redact import mask_secret
    return mask_secret(value)


def _memory_provider(config: dict) -> str:
    """Return the active memory provider name."""
    mem = config.get("memory", {})
    provider = mem.get("provider", "")
    return provider if provider else "built-in"


