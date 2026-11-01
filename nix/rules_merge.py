"""Seed memories/rules.toml from the Nix-declared `memoryRules`.

Nix CREATES the file; after that it stays mutable (the agent adds rules, with
the user's approval, and the user edits it by hand). So the merge is three-way
by rule id, with a state file (`memories/.nix-rules.json`) recording what Nix
last wrote, the same idea as config_merge.py:

  declared, missing on disk                          -> add
  declared, on disk equal to what Nix last wrote     -> write the declared value
  declared, on disk changed since Nix wrote it       -> keep disk, report it
  no longer declared, on disk equal to last written  -> remove
  no longer declared, changed since                  -> keep, report it
  ids Nix never declared                             -> untouched

Without a state file (first run) nothing can be attributed to Nix: missing ids
are added, a differing on-disk value is kept and reported, and nothing is ever
removed. A rules.toml that does not parse is never overwritten.

`merge_rules` is the pure function; `main` is the thin file glue.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tomllib
from pathlib import Path

# The script ships as a standalone file next to config_merge.py (see
# rulesMergeScript.nix); tests import it by path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_merge import dump_toml, load_state  # noqa: E402

# What Nix owns of a rule. Anything else on a disk entry (added, notes the user
# wrote, ...) is the user's and is left alone.
MANAGED_KEYS = ("text", "why", "scope", "source")


def _managed(entry: dict) -> dict:
    """The Nix-owned view of an entry, with the defaults Nix would have written."""
    view = {"text": entry.get("text"), "scope": entry.get("scope", "common")}
    if entry.get("why"):
        view["why"] = entry["why"]
    if entry.get("source") is not None:
        view["source"] = entry["source"]
    return view


def canonical(entry: dict) -> dict:
    """A declared rule as it is compared and recorded: id plus the managed view."""
    view = _managed({"source": "user", **entry})
    return {"id": str(entry["id"]), **view}


def merge_rules(
    declared: list[dict],
    on_disk: list[dict],
    last_written: dict | None,
) -> tuple[list[dict], dict, list[str]]:
    """Return ``(result, new_state, report)``.

    *declared*: the rules Nix declares now. *on_disk*: the `[[rule]]` tables in
    rules.toml. *last_written*: ``{id: canonical entry}`` Nix wrote last time,
    or ``None`` when there is no state file.
    """
    want = {}
    for entry in declared:
        canon = canonical(entry)
        want[canon["id"]] = canon
    last = last_written or {}
    result = [copy.deepcopy(r) for r in on_disk]
    by_id = {}
    for rule in result:
        by_id.setdefault(rule.get("id"), rule)
    state: dict = {}
    report: list[str] = []

    for rid, canon in want.items():
        disk = by_id.get(rid)
        if disk is None:
            fresh = {k: v for k, v in canon.items() if v is not None}
            result.append(fresh)
            by_id[rid] = fresh
            state[rid] = canon
            report.append(f"rules.toml: added {rid}")
            continue
        current = {"id": rid, **_managed(disk)}
        if current == canon:
            state[rid] = canon
        elif rid in last and current == last[rid]:
            for key in MANAGED_KEYS:
                if key in canon:
                    disk[key] = canon[key]
                else:
                    disk.pop(key, None)
            state[rid] = canon
            report.append(f"rules.toml: updated {rid} to the declared value")
        else:
            why = (
                "changed after Nix wrote it"
                if rid in last
                else "no record of Nix writing it"
            )
            report.append(
                f"rules.toml: kept {rid} as it is on disk — it differs from the "
                f"declared value ({why})"
            )
            if rid in last:
                state[rid] = last[rid]

    for rid, was in last.items():
        if rid in want:
            continue
        disk = by_id.get(rid)
        if disk is None:
            continue
        if {"id": rid, **_managed(disk)} == was:
            result.remove(disk)
            report.append(f"rules.toml: removed {rid} (retired in Nix)")
        else:
            report.append(
                f"rules.toml: kept {rid} — no longer declared in Nix, but it "
                "changed after Nix wrote it"
            )

    return result, state, report


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="son-of-anton-rules-merge",
        description="Seed memories/rules.toml from the Nix-declared memoryRules.",
    )
    parser.add_argument("nix_rules", type=Path, help="Declared rules, as TOML ([[rule]]).")
    parser.add_argument("rules_path", type=Path, help="The rules.toml on disk.")
    parser.add_argument("--state", type=Path, required=True, help="What Nix last wrote (JSON).")
    args = parser.parse_args()

    declared = tomllib.loads(args.nix_rules.read_text(encoding="utf-8")).get("rule", [])
    state = load_state(args.state)
    exists = args.rules_path.exists()
    on_disk: list = []
    if exists:
        try:
            on_disk = tomllib.loads(args.rules_path.read_text(encoding="utf-8")).get("rule", [])
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            print(
                f"rules.toml: left untouched — it does not parse ({e}). Fix it by "
                "hand; the next activation will merge the declared rules.",
                file=sys.stderr,
            )
            return 0
    if not declared and not state and not exists:
        return 0

    result, new_state, report = merge_rules(declared, on_disk, state)
    for line in report:
        print(line, file=sys.stderr)
    if result != on_disk or not exists:
        _write(args.rules_path, dump_toml({"rule": result}))
    _write(args.state, json.dumps(new_state, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
