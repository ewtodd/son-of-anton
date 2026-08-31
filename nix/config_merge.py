"""Merge the Nix-declared settings into the config.yaml on disk.

This is a THREE-way merge, and the third input is the point of it.

A two-way deep merge — on-disk as the base, the Nix settings layered over it —
can add a key and can change a key, but it can never take one away. So a key
that Nix used to declare and no longer does survives on disk forever, and the
module stops being declarative the moment anything is retired. The symptom is
undramatic and durable: a nixos-rebuild that says it removed a model leaves the
model in config.yaml, the agent goes on offering it, and every request against
it fails at the provider. Nothing in the rebuild reports a problem, because as
far as the rebuild is concerned there wasn't one.

The fix is to remember what Nix wrote last time. Each run records its own
output in a state file next to config.yaml, so the next run has a base to
compare against and can tell two situations apart that a two-way merge cannot:

  Nix declared it, no longer declares it, and the on-disk value is still the
  one Nix wrote.  -> Nix retired it. Remove it.

  Nix declared it, no longer declares it, but the on-disk value has changed
  since.            -> Something wrote it at runtime after Nix did. Keep it.

Everything Nix never declared is untouched, which is the property the merge
existed for in the first place: `son-of-anton config set`, the TUI settings
panes and the desktop app all write this file, and a rebuild must not clobber
them.

Adoption. The first run after this script is introduced has no state file, so
no key can be attributed to Nix and nothing is pruned — a first run is never
destructive. It reports the keys on disk that Nix does not declare, since on
an install that predates the state file those are exactly where a stale
Nix-written key would be hiding, and an operator can then re-run with --adopt
to treat the whole on-disk file as Nix's and prune in one pass. --dry-run
prints the plan and writes nothing.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

import yaml

MISSING = object()


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data if isinstance(data, dict) else {}


def load_state(path: Path | None) -> dict | None:
    """Return the previous Nix output, or None when there is no usable state."""
    if path is None or not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def leaf_paths(tree: dict, prefix: tuple = ()) -> list[tuple]:
    """Every path that ends at a non-dict value, or at an empty dict."""
    paths: list[tuple] = []
    for key, value in tree.items():
        path = prefix + (key,)
        if isinstance(value, dict) and value:
            paths.extend(leaf_paths(value, path))
        else:
            paths.append(path)
    return paths


def get_path(tree: dict, path: tuple):
    node = tree
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return MISSING
        node = node[key]
    return node


def delete_path(tree: dict, path: tuple) -> bool:
    """Remove *path*, then any dicts it leaves empty. True if anything went."""
    parents = []
    node = tree
    for key in path[:-1]:
        if not isinstance(node, dict) or key not in node:
            return False
        parents.append((node, key))
        node = node[key]
    if not isinstance(node, dict) or path[-1] not in node:
        return False
    del node[path[-1]]
    for parent, key in reversed(parents):
        if isinstance(parent.get(key), dict) and not parent[key]:
            del parent[key]
        else:
            break
    return True


def render(path: tuple) -> str:
    return ".".join(str(part) for part in path)


def collapse(paths: list[tuple], before: dict, after: dict) -> list[str]:
    """Report the outermost key that actually disappeared, not every leaf.

    Removing the only setting under a model entry removes the entry. Saying
    "removed custom_providers.custom.models.qwen3.6-35b-a3b.context_length"
    when the whole model is gone reads as a much smaller change than it was.
    """
    reported: list[str] = []
    seen: set[tuple] = set()
    for path in paths:
        shortest = path
        for stop in range(1, len(path)):
            prefix = path[:stop]
            if get_path(before, prefix) is not MISSING and (
                get_path(after, prefix) is MISSING
            ):
                shortest = prefix
                break
        if shortest not in seen:
            seen.add(shortest)
            reported.append(render(shortest))
    return reported


def plan_removals(base: dict, theirs: dict, ours: dict) -> tuple[list, list]:
    """Split the retired paths into (remove, keep-because-edited)."""
    remove: list[tuple] = []
    keep: list[tuple] = []
    for path in leaf_paths(base):
        if get_path(theirs, path) is not MISSING:
            continue
        on_disk = get_path(ours, path)
        if on_disk is MISSING:
            continue
        if on_disk == get_path(base, path):
            remove.append(path)
        else:
            keep.append(path)
    return remove, keep


def unmanaged_paths(ours: dict, theirs: dict) -> list[tuple]:
    """On-disk paths Nix does not declare."""
    return [p for p in leaf_paths(ours) if get_path(theirs, p) is MISSING]


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="son-of-anton-config-merge",
        description="Merge Nix-declared settings into config.yaml on disk.",
    )
    parser.add_argument("nix_json", type=Path, help="The generated Nix settings.")
    parser.add_argument("config_path", type=Path, help="The config.yaml on disk.")
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="Where the previous Nix output is recorded. Without it the merge "
        "cannot retract anything and degrades to the old two-way behaviour.",
    )
    parser.add_argument(
        "--adopt",
        action="store_true",
        help="One-time migration: treat the whole on-disk file as Nix's, so "
        "every key Nix no longer declares is pruned. Run --dry-run first.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change and write nothing.",
    )
    args = parser.parse_args()

    with args.nix_json.open(encoding="utf-8") as handle:
        theirs = json.load(handle)
    ours = load_yaml(args.config_path)

    state = load_state(args.state)
    first_run = state is None
    base = copy.deepcopy(ours) if args.adopt else (state or {})

    merged = deep_merge(ours, theirs)

    remove, kept = plan_removals(base, theirs, ours)
    before = copy.deepcopy(merged)
    for path in remove:
        delete_path(merged, path)

    for name in collapse(remove, before, merged):
        print(f"config.yaml: removed {name} (retired in Nix)", file=sys.stderr)
    for path in kept:
        print(
            f"config.yaml: kept {render(path)} — no longer declared in Nix, but "
            "its value changed after Nix wrote it",
            file=sys.stderr,
        )

    if first_run and not args.adopt:
        orphans = unmanaged_paths(ours, theirs)
        if orphans:
            print(
                "config.yaml: no record of what Nix wrote previously, so nothing "
                "was retracted on this run. These keys are on disk and not "
                "declared in Nix — runtime settings, or leftovers from a Nix "
                "generation that predates this state file:",
                file=sys.stderr,
            )
            for path in orphans:
                print(f"  {render(path)}", file=sys.stderr)
            print(
                "If they are leftovers, re-run this script with --adopt "
                "(add --dry-run first) to prune them in one pass. Otherwise "
                "ignore this: from now on, retiring a key in Nix removes it.",
                file=sys.stderr,
            )

    if args.dry_run:
        print("--dry-run: nothing written.", file=sys.stderr)
        return 0

    with args.config_path.open("w", encoding="utf-8") as handle:
        yaml.dump(merged, handle, default_flow_style=False, sort_keys=False)

    if args.state is not None:
        args.state.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.state.with_suffix(args.state.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(theirs, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp.replace(args.state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
