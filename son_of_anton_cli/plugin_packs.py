"""Plugin packs — declarative, shareable plugin sets (#64166).

A pack is a single TOML file (``son-of-anton-pack.yaml``) that pins a set of
plugins (source + exact commit SHA + optional non-secret config seeds).
Installing a pack is nothing new at runtime: it fans out to N ordinary
plugin installs through the existing pinned-ref install path, then seeds
``plugins.entries.<id>`` config keys.

Format (canonical)::

    name: voice-assistant-pack
    description: STT + streaming TTS + approval relay
    author: hyper
    version: 1.0.0
    plugins:
      - name: son-of-anton-media-studio          # bare community-index name…
        ref: e8d59971d2b7901405b39dac7b03bdd616272d0d
      - repo: owner/approval-relay         # …or explicit owner/repo / git URL
        ref: 8f3c2d1a9b4e5f6071829304a5b6c7d8e9f00112
        subdir: plugins/relay              # optional path within the repo
    config:                                # optional plugins.entries seeds
      son-of-anton-media-studio:
        default_model: flux-3
    skills: []                             # declared seam — NOT auto-installed

Supply-chain posture:

* Every plugin entry MUST pin an exact 40-character commit SHA in ``ref``.
  Tags and branch names are rejected with an error naming the entry.
* ``config`` seeds are limited to ``plugins.entries.<id>.*`` keys and may
  never carry secrets (secret-shaped key names are rejected) nor
  capability-grant keys (a pack cannot pre-consent capabilities).
* Capability consent is NEVER bulk-granted: after each plugin installs,
  its declared capabilities ride the exact same per-plugin consent flow
  as a normal ``son-of-anton plugins install`` (#64228).

``skills:`` is parsed and displayed but not installed — wiring skill-hub
ids into the skills installer is a documented follow-up seam.
"""

from __future__ import annotations


logger = __import__("logging").getLogger(__name__)


# Key names that look like secrets are refused in pack config seeds and
# stripped from exports. Packs declare needed secrets via each plugin's own
# ``requires_env`` manifest field, which prompts at install time.

# plugins.entries.<id> keys a pack may never set: consent/capability state
# and the deprecated allow_* trust gates. A pack cannot pre-grant anything.

_FETCH_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Parse + validate
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Resolution (bare index names → owner/repo) + review screen
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Install fan-out
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


