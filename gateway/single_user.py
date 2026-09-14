"""``gateway.single_user`` — one account, one person, one view of its sessions.

A gateway instance is normally multi-tenant: one process answers every chat
on every platform, and the session table behind it carries titles and
previews of everyone's conversations. ``/sessions`` and ``/resume`` are
therefore scoped to the caller's own origin, and crossing that line needs an
explicitly configured admin.

The work and play instances are one person each, sharing that person's
``SON_OF_ANTON_HOME`` with their terminal. For them the scoping only hides
their own TUI sessions from Signal. ``single_user: true`` says so, and the
gateway then browses and resumes the whole account database exactly as the
terminal does.

The flag must not become a way to reopen the enumeration hole on a shared
instance, so it is honoured only when every connected platform's allowlist
names exactly one user. An open allowlist (``*``), an empty one, several
users, or an adapter whose allowlist cannot be read all refuse it.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = ["allowlist_problem"]

# Attributes adapters use for their allowlists (Signal: dm_allow_from /
# group_allow_from from SIGNAL_ALLOWED_USERS / SIGNAL_GROUP_ALLOWED_USERS).
_ALLOWLIST_ATTRS = ("dm_allow_from", "group_allow_from", "allow_from", "allowed_users")


def allowlist_problem(adapters: Mapping[Any, Any]) -> Optional[str]:
    """Why ``single_user`` must not apply to *adapters*, or ``None`` when it may.

    *adapters* maps a platform to its connected adapter. The union of every
    adapter's allowlists has to be exactly one user id.
    """
    users: set[str] = set()
    unverifiable: list[str] = []
    for platform, adapter in adapters.items():
        name = getattr(platform, "value", None) or str(platform)
        found = False
        for attr in _ALLOWLIST_ATTRS:
            value = getattr(adapter, attr, None)
            if value is None:
                continue
            found = True
            try:
                users |= {str(v).strip() for v in value if str(v).strip()}
            except TypeError:
                users.add(str(value).strip())
        if not found:
            unverifiable.append(name)
    if unverifiable:
        return "cannot read the allowlist for " + ", ".join(sorted(unverifiable))
    if "*" in users:
        return "an allowlist is open (*)"
    if len(users) != 1:
        return f"the allowlists name {len(users)} users, not exactly one"
    return None
