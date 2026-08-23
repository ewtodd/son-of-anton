"""i18n contract — the fork is English-only, and every key the gateway uses
must resolve to real English text, never the raw dotted key (the raw key
leak on /reset was a live gateway bug after the locale prune).
"""

from __future__ import annotations

from agent.i18n import t


def test_english_catalog_resolves_gateway_keys() -> None:
    for key in (
        "gateway.reset.header_default",
        "gateway.reset.tip",
        "gateway.reset.header_new",
        "approval.choose_long",
        "gateway.draining",
        "gateway.update.son_of_anton_cmd_not_found",
    ):
        text = t(key)
        assert text, f"{key}: empty translation"
        assert text != key, f"{key}: raw key leaked instead of English text"


def test_missing_keys_fall_back_gracefully() -> None:
    # Unknown keys return themselves (documented fallback) but never raise.
    assert t("gateway.not_a_real_key_123") == "gateway.not_a_real_key_123"
