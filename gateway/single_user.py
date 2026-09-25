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
names exactly one user. A platform's group-routing allowlist holds chat
identifiers, not people (Signal: the group ids the instance answers), so it is
not counted toward that one user; only a user-identity allowlist is. An open
allowlist (``*``), an empty one, several users, or an adapter whose allowlist
cannot be read all refuse it.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = ["allowlist_problem"]

# Attributes adapters use for their USER allowlists. Signal splits the two:
# ``dm_allow_from`` names senders (SIGNAL_ALLOWED_USERS) while
# ``group_allow_from`` names routing keys — the group ids this instance
# answers (SIGNAL_GROUP_ALLOWED_USERS) — so the group list is deliberately
# not part of this tuple. Counting a group id as a user made every grouped
# single-owner instance look plural and silently cleared
# ``gateway.single_user`` on boot.
_USER_ALLOWLIST_ATTRS = ("dm_allow_from", "allow_from", "allowed_users")
# Routing allowlists. Consulted only for adapters that expose no
# user-identity allowlist at all (on some platforms they may hold user ids),
# so the guard still verifies rather than trusting an unread allowlist.
_ROUTING_ALLOWLIST_ATTRS = ("group_allow_from",)


def _collect_allowlist(adapter: Any, attrs: tuple[str, ...], users: set[str]) -> bool:
    """Merge every present *attrs* value from *adapter* into *users*.

    Returns True when at least one attribute was present. Adapters store
    these as iterables of ids; a bare scalar is tolerated so a hand-rolled
    adapter cannot crash the guard.
    """
    found = False
    for attr in attrs:
        value = getattr(adapter, attr, None)
        if value is None:
            continue
        found = True
        try:
            users |= {str(v).strip() for v in value if str(v).strip()}
        except TypeError:
            users.add(str(value).strip())
    return found


def allowlist_problem(adapters: Mapping[Any, Any]) -> Optional[str]:
    """Why ``single_user`` must not apply to *adapters*, or ``None`` when it may.

    *adapters* maps a platform to its connected adapter. The union of every
    adapter's user allowlists has to be exactly one user id.
    """
    users: set[str] = set()
    unverifiable: list[str] = []
    for platform, adapter in adapters.items():
        name = getattr(platform, "value", None) or str(platform)
        found = _collect_allowlist(adapter, _USER_ALLOWLIST_ATTRS, users)
        if not found:
            found = _collect_allowlist(adapter, _ROUTING_ALLOWLIST_ATTRS, users)
        if not found:
            unverifiable.append(name)
    if unverifiable:
        return "cannot read the allowlist for " + ", ".join(sorted(unverifiable))
    if "*" in users:
        return "an allowlist is open (*)"
    if len(users) != 1:
        return f"the allowlists name {len(users)} users, not exactly one"
    return None
