"""``son-of-anton plugins`` CLI subcommand — install, update, remove, and list plugins.

Plugins are installed from Git repositories into ``~/.son-of-anton/plugins/``.
Supports full URLs and ``owner/repo`` shorthand (resolves to GitHub).

After install, if the plugin ships an ``after-install.md`` file it is
rendered with Rich Markdown.  Otherwise a default confirmation is shown.
"""

from __future__ import annotations
from son_of_anton_cli.cli_output import line_input

import functools
import importlib.metadata
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Optional

from son_of_anton_constants import get_son_of_anton_home
from son_of_anton_cli._subprocess_compat import noninteractive_git_env
from son_of_anton_cli.config import cfg_get
from son_of_anton_cli.secret_prompt import masked_secret_prompt
from utils import atomic_write_text

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _resolve_git_executable() -> Optional[str]:
    """Resolve a git binary for subprocess use when ``PATH`` may be minimal.

    Matches other Son of Anton subprocess resolution: :func:`shutil.which` first,
    then common Git for Windows install paths and POSIX defaults.
    """
    found = shutil.which("git")
    if found:
        return found
    if os.name == "nt":
        prog = os.environ.get("ProgramFiles", r"C:\Program Files")
        prog_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(prog, "Git", "cmd", "git.exe"),
            os.path.join(prog, "Git", "bin", "git.exe"),
            os.path.join(prog_x86, "Git", "cmd", "git.exe"),
            os.path.join(prog_x86, "Git", "bin", "git.exe"),
        ]
        if local:
            candidates.extend(
                (
                    os.path.join(local, "Programs", "Git", "cmd", "git.exe"),
                    os.path.join(local, "Programs", "Git", "bin", "git.exe"),
                )
            )
    else:
        candidates = ["/usr/bin/git", "/usr/local/bin/git", "/bin/git"]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


class PluginOperationError(Exception):
    """Recoverable plugin install/update failure (CLI exits; HTTP maps to 4xx)."""


class PluginScanBlocked(PluginOperationError):
    """Plugin failed the security scan and was not installed.

    Carries the ScanResult so callers (CLI, dashboard) can render the
    findings report alongside the error message.
    """

    def __init__(self, message: str, scan_result=None):
        super().__init__(message)
        self.scan_result = scan_result


def _scan_on_install_enabled() -> bool:
    """Whether install/update-time plugin security scanning is enabled.

    On by default (inspired by Claude Cowork's skill & plugin security
    scanning). Disable via ``plugins.scan_on_install: false`` in config.toml.
    """
    try:
        from son_of_anton_cli.config import load_config
        config = load_config()
        return bool(cfg_get(config, "plugins", "scan_on_install", default=True))
    except Exception:
        return True


def _scan_plugin_tree(plugin_dir: Path, identifier: str, *, force: bool, scan_decision_cb=None):
    """Scan *plugin_dir* and enforce the install policy.

    Verdicts: safe → proceed; caution → needs confirmation (``force=True``
    or a truthy ``scan_decision_cb(result)``); dangerous → always blocked.
    Raises :class:`PluginScanBlocked` when the plugin may not be installed.
    Returns the ScanResult (or None when scanning is disabled).
    """
    if not _scan_on_install_enabled():
        return None

    from tools.plugin_guard import (
        format_scan_report,
        scan_plugin,
        should_allow_plugin_install,
    )

    result = scan_plugin(plugin_dir, source=identifier)
    allowed, reason = should_allow_plugin_install(result, force=force)

    if allowed is None and scan_decision_cb is not None:
        try:
            if scan_decision_cb(result):
                allowed = True
                reason = "Caution verdict accepted by user"
        except Exception:
            logger.exception("plugin scan decision callback failed")

    if allowed is not True:
        raise PluginScanBlocked(
            f"Security scan blocked plugin install: {reason}\n\n"
            f"{format_scan_report(result)}\n"
            "Review the findings above. Install only plugins from sources "
            "you trust. (Scanning can be configured via "
            "plugins.scan_on_install in config.toml.)",
            scan_result=result,
        )
    logger.info("plugin scan passed for %s: %s", plugin_dir.name, reason)
    return result


# Minimum manifest version this installer understands.
# Plugins may declare ``manifest_version: 1`` in plugin.toml;
# future breaking changes to the manifest schema bump this.
_SUPPORTED_MANIFEST_VERSION = 1


def _plugins_dir() -> Path:
    """Return the user plugins directory, creating it if needed."""
    plugins = get_son_of_anton_home() / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    return plugins


def _sanitize_plugin_name(
    name: str,
    plugins_dir: Path,
    *,
    allow_subdir: bool = False,
) -> Path:
    """Validate a plugin name and return the safe target path inside *plugins_dir*.

    Raises ``ValueError`` if the name contains path-traversal sequences or would
    resolve outside the plugins directory.

    ``allow_subdir=True`` permits a single forward slash inside *name* so
    category-namespaced plugin keys like ``observability/langfuse`` or
    ``image_gen/openai`` (the registry keys emitted by ``_discover_all_plugins``)
    can be looked up. ``..`` and backslash are still rejected, leading and
    trailing slashes are stripped, and the resolved target must still live
    inside *plugins_dir*. Install paths leave this at the default ``False``
    because a freshly-cloned plugin always lands top-level under
    ``~/.son-of-anton/plugins/<name>/``.
    """
    if not name:
        raise ValueError("Plugin name must not be empty.")

    if allow_subdir:
        name = name.strip("/")
        if not name:
            raise ValueError("Plugin name must not be empty.")

    if name in {".", ".."}:
        raise ValueError(
            f"Invalid plugin name '{name}': must not reference the plugins directory itself."
        )

    # Reject obvious traversal characters
    bad_chars = ("\\", "..") if allow_subdir else ("/", "\\", "..")
    for bad in bad_chars:
        if bad in name:
            raise ValueError(f"Invalid plugin name '{name}': must not contain '{bad}'.")

    target = (plugins_dir / name).resolve()
    plugins_resolved = plugins_dir.resolve()

    if target == plugins_resolved:
        raise ValueError(
            f"Invalid plugin name '{name}': resolves to the plugins directory itself."
        )

    try:
        target.relative_to(plugins_resolved)
    except ValueError:
        raise ValueError(
            f"Invalid plugin name '{name}': resolves outside the plugins directory."
        )

    return target


_GITHUB_BROWSER_SEGMENTS = {
    "actions",
    "blob",
    "commit",
    "commits",
    "issues",
    "pull",
    "pulls",
    "releases",
    "tree",
    "wiki",
}


def _resolve_git_url(identifier: str) -> tuple[str, Optional[str]]:
    """Turn an identifier into a cloneable Git URL and optional subdirectory.

    Returns ``(git_url, subdir)`` where ``subdir`` is the path within the
    cloned repository that contains the plugin (``None`` when the plugin lives
    at the repo root).

    Accepted formats:
    - Full URL: https://github.com/owner/repo.git
    - Full URL: git@github.com:owner/repo.git
    - Full URL: ssh://git@github.com/owner/repo.git
    - Browser URL: https://github.com/owner/repo/tree/main/path
      →  (https://github.com/owner/repo.git, "path")
    - Shorthand: owner/repo  →  https://github.com/owner/repo.git
    - Shorthand w/ subdir: owner/repo/path/to/plugin
      →  (https://github.com/owner/repo.git, "path/to/plugin")
    - Full URL w/ subdir (``.git`` boundary):
      https://github.com/owner/repo.git/path/to/plugin
      →  (https://github.com/owner/repo.git, "path/to/plugin")
    - Any URL w/ explicit subdir fragment (works for every scheme, incl.
      ``file://`` and ssh): <url>#path/to/plugin
      →  (<url>, "path/to/plugin")

    NOTE: ``http://`` and ``file://`` schemes are accepted but will trigger a
    security warning at install time.
    """
    # Already a URL.
    if identifier.startswith(("https://", "http://", "git@", "ssh://", "file://")):
        if identifier.startswith("https://github.com/"):
            path = identifier[len("https://github.com/") :]
            path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
            parts = path.split("/")
            if len(parts) >= 3 and all(parts[:2]) and parts[2] in _GITHUB_BROWSER_SEGMENTS:
                repo = parts[1].removesuffix(".git")
                subdir = None
                if parts[2] == "tree" and len(parts) >= 5:
                    subdir = "/".join(p for p in parts[4:] if p).strip("/") or None
                return f"https://github.com/{parts[0]}/{repo}.git", subdir

        # Explicit ``#subdir`` fragment — unambiguous for any scheme.
        if "#" in identifier:
            git_url, _, frag = identifier.partition("#")
            return git_url, (frag.strip("/") or None)
        # Natural ``.git/`` boundary (GitHub-style URLs).
        marker = ".git/"
        idx = identifier.find(marker)
        if idx != -1:
            git_url = identifier[: idx + len(".git")]
            subdir = identifier[idx + len(marker) :].strip("/")
            return git_url, (subdir or None)
        return identifier, None

    # owner/repo[/subdir...] shorthand
    parts = [p for p in identifier.strip("/").split("/") if p]
    if len(parts) >= 2:
        owner, repo = parts[0], parts[1]
        subdir = "/".join(parts[2:]).strip("/")
        git_url = f"https://github.com/{owner}/{repo}.git"
        return git_url, (subdir or None)

    raise ValueError(
        f"Invalid plugin identifier: '{identifier}'. "
        "Use a Git URL or 'owner/repo' shorthand (optionally with a subdirectory: "
        "'owner/repo/path/to/plugin')."
    )


def _resolve_subdir_within(clone_root: Path, subdir: str) -> Path:
    """Resolve ``subdir`` inside ``clone_root``, rejecting path traversal.

    Guards against ``..`` segments, absolute paths, and symlinks that would
    escape the cloned repository. Returns the resolved directory path.
    Raises ``PluginOperationError`` if the path escapes the clone, doesn't
    exist, or is not a directory.
    """
    clone_root = clone_root.resolve()
    candidate = (clone_root / subdir).resolve()

    # The resolved candidate must stay within the clone root.
    if candidate != clone_root and clone_root not in candidate.parents:
        raise PluginOperationError(
            f"Plugin subdirectory '{subdir}' escapes the repository.",
        )

    if not candidate.exists():
        raise PluginOperationError(
            f"Plugin subdirectory '{subdir}' does not exist in the repository.",
        )
    if not candidate.is_dir():
        raise PluginOperationError(
            f"Plugin subdirectory '{subdir}' is not a directory.",
        )

    return candidate


def _repo_name_from_url(url: str) -> str:
    """Extract the repo name from a Git URL for the plugin directory name."""
    # Strip trailing .git and slashes
    name = url.rstrip("/")
    if name.endswith(".git"):
        name = name[:-4]
    # Get last path component
    name = name.rsplit("/", 1)[-1]
    # Handle ssh-style urls: git@github.com:owner/repo
    if ":" in name:
        name = name.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    return name


def _read_manifest(plugin_dir: Path) -> dict:
    """Read a native or portable manifest, preferring native TOML."""
    manifest_file = plugin_dir / "plugin.toml"
    if not manifest_file.exists():
        manifest_file = plugin_dir / "plugin.toml"
    if not manifest_file.exists():
        portable_file = plugin_dir / "plugin.json"
        if not portable_file.exists() and not portable_file.is_symlink():
            return {}
        try:
            from son_of_anton_cli.agent_plugins import read_agent_plugin_manifest

            manifest, _ = read_agent_plugin_manifest(plugin_dir)
            return manifest
        except Exception as e:
            logger.warning("Failed to read plugin.json in %s: %s", plugin_dir, e)
            return {}
    try:
        import tomllib

        with open(manifest_file, encoding="utf-8") as f:
            return tomllib.loads(f.read()) or {}
    except Exception as e:
        logger.warning("Failed to read plugin.toml in %s: %s", plugin_dir, e)
        return {}


def _copy_example_files(plugin_dir: Path, console) -> None:
    """Copy any .example files to their real names if they don't already exist.

    For example, ``config.toml.example`` becomes ``config.toml``.
    Skips files that already exist to avoid overwriting user config on reinstall.
    """
    for example_file in plugin_dir.glob("*.example"):
        real_name = example_file.stem  # e.g. "config.toml" from "config.toml.example"
        real_path = plugin_dir / real_name
        if not real_path.exists():
            try:
                shutil.copy2(example_file, real_path)
                console.print(
                    f"[dim]  Created {real_name} from {example_file.name}[/dim]"
                )
            except OSError as e:
                console.print(
                    f"[yellow]Warning:[/yellow] Failed to copy {example_file.name}: {e}"
                )


def _prompt_plugin_env_vars(manifest: dict, console) -> None:
    """Prompt for required environment variables declared in plugin.toml.

    ``requires_env`` accepts two formats:

    Simple list (backwards-compatible)::

        requires_env:
          - MY_API_KEY

    Rich list with metadata::

        requires_env:
          - name: MY_API_KEY
            description: "API key for Acme service"
            url: "https://acme.com/keys"
            secret: true

    Already-set variables are skipped.  Values are saved to the user's ``.env``.
    """
    requires_env = manifest.get("requires_env") or []
    if not requires_env:
        return

    from son_of_anton_cli.config import get_env_value, save_env_value  # noqa: F811
    from son_of_anton_constants import display_son_of_anton_home

    # Normalise to list-of-dicts
    env_specs: list[dict] = []
    for entry in requires_env:
        if isinstance(entry, str):
            env_specs.append({"name": entry})
        elif isinstance(entry, dict) and entry.get("name"):
            env_specs.append(entry)

    # Filter to only vars that aren't already set
    missing = [s for s in env_specs if not get_env_value(s["name"])]
    if not missing:
        return

    plugin_name = manifest.get("name", "this plugin")
    console.print(f"\n[bold]{plugin_name}[/bold] requires the following environment variables:\n")

    for spec in missing:
        name = spec["name"]
        desc = spec.get("description", "")
        url = spec.get("url", "")
        secret = spec.get("secret", False)

        label = f"  {name}"
        if desc:
            label += f" — {desc}"
        console.print(label)
        if url:
            console.print(f"  [dim]Get yours at: {url}[/dim]")

        try:
            if secret:
                value = masked_secret_prompt(f"  {name}: ").strip()
            else:
                value = line_input(f"  {name}: ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print(f"\n[dim]  Skipped (you can set these later in {display_son_of_anton_home()}/.env)[/dim]")
            return

        if value:
            save_env_value(name, value)
            os.environ[name] = value
            console.print(f"  [green]✓[/green] Saved to {display_son_of_anton_home()}/.env")
        else:
            console.print(f"  [dim]  Skipped (set {name} in {display_son_of_anton_home()}/.env later)[/dim]")

    console.print()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


_EXACT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_INSTALL_METADATA_FILE = ".install-metadata.json"

def _install_metadata_path() -> Path:
    return get_son_of_anton_home() / "plugins" / _INSTALL_METADATA_FILE


def _read_install_metadata() -> dict[str, dict[str, object]]:
    """Read profile-local, non-secret plugin source metadata from disk."""
    path = _install_metadata_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginOperationError(f"Could not read plugin install metadata: {exc}") from exc
    if not isinstance(value, dict):
        raise PluginOperationError("Plugin install metadata must be a JSON object.")
    return value


def _write_install_metadata(metadata: dict[str, dict[str, object]]) -> None:
    """Atomically replace the profile-local plugin install metadata sidecar."""
    path = _install_metadata_path()
    atomic_write_text(
        path,
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        tmp_prefix=f"{path.name}.tmp-",
    )


def _normalize_exact_revision(ref: str) -> str:
    if not isinstance(ref, str) or not _EXACT_COMMIT_RE.fullmatch(ref):
        raise PluginOperationError("--ref must be a full 40-character commit SHA.")
    return ref.lower()


def _safe_git_error(result: subprocess.CompletedProcess, source_url: str = "") -> str:
    """Return diagnosable Git output without echoing embedded credentials."""
    from agent.redact import redact_sensitive_text

    error = (result.stderr or result.stdout or "").strip()
    if source_url:
        error = error.replace(source_url, _scrub_git_url(source_url))
    return redact_sensitive_text(error)


def _git_head_revision(repo: Path, git_exe: str) -> str:
    result = subprocess.run(
        [git_exe, "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=noninteractive_git_env(),
    )
    if result.returncode != 0:
        err = _safe_git_error(result)
        raise PluginOperationError(f"Could not determine installed Git revision:\n{err}")
    return result.stdout.strip().lower()


def _checkout_exact_revision(repo: Path, git_exe: str, revision: str) -> None:
    """Fetch and detach at one immutable commit, then verify the resulting HEAD."""
    try:
        fetched = subprocess.run(
            [git_exe, "fetch", "--depth", "1", "origin", revision],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            stdin=subprocess.DEVNULL,
            env=noninteractive_git_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise PluginOperationError(
            f"Git fetch of commit '{revision}' timed out after 60 seconds."
        ) from exc
    if fetched.returncode != 0:
        err = _safe_git_error(fetched)
        raise PluginOperationError(
            f"Git commit '{revision}' could not be fetched:\n{err}"
        )
    try:
        checked_out = subprocess.run(
            [git_exe, "checkout", "--detach", revision],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            stdin=subprocess.DEVNULL,
            env=noninteractive_git_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise PluginOperationError(
            f"Git checkout of commit '{revision}' timed out after 60 seconds."
        ) from exc
    if checked_out.returncode != 0:
        err = _safe_git_error(checked_out)
        raise PluginOperationError(
            f"Git checkout of commit '{revision}' failed:\n{err}"
        )
    actual = _git_head_revision(repo, git_exe)
    if actual != revision:
        raise PluginOperationError(
            f"Checked-out revision '{actual}' does not match requested commit '{revision}'."
        )


def _scrub_git_url(git_url: str) -> str:
    """Strip credentials and query/fragment data from an HTTP Git URL."""
    parsed = urllib.parse.urlsplit(git_url)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urllib.parse.urlunsplit(
            (parsed.scheme, host, parsed.path, "", "")
        )
    return git_url


def _canonical_source(git_url: str, subdir: Optional[str]) -> str:
    scrubbed = _scrub_git_url(git_url)
    return f"{scrubbed}#{subdir}" if subdir else scrubbed


def _scrub_cloned_origin(repo: Path, git_exe: str, git_url: str) -> None:
    """Ensure credentials used for cloning do not survive in ``.git/config``."""
    scrubbed = _scrub_git_url(git_url)
    if scrubbed == git_url:
        return
    result = subprocess.run(
        [git_exe, "remote", "set-url", "origin", scrubbed],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=noninteractive_git_env(),
    )
    if result.returncode != 0:
        err = _safe_git_error(result, git_url)
        raise PluginOperationError(f"Could not sanitize installed Git remote:\n{err}")


def _install_plugin_core(
    identifier: str,
    *,
    force: bool,
    ref: Optional[str] = None,
    scan_decision_cb=None,
) -> tuple[Path, dict, str]:
    """Clone a Git plugin and atomically record its source and exact revision."""
    requested_revision = _normalize_exact_revision(ref) if ref is not None else None
    try:
        git_url, subdir = _resolve_git_url(identifier)
    except ValueError as e:
        raise PluginOperationError(str(e)) from e

    plugins_dir = _plugins_dir()
    source = _canonical_source(git_url, subdir)
    old_metadata = _read_install_metadata()

    # Reinstalling the same pinned source retains its pin, even if its plugin
    # directory was manually removed. Moving a pin requires an explicit --ref.
    if requested_revision is None:
        matching_pins = [
            entry
            for entry in old_metadata.values()
            if entry.get("source") == source and entry.get("pinned") is True
        ]
        if len(matching_pins) == 1:
            revision = matching_pins[0].get("revision")
            if isinstance(revision, str):
                requested_revision = _normalize_exact_revision(revision)

    with tempfile.TemporaryDirectory(prefix=".install-", dir=plugins_dir) as tmp:
        tmp_clone = Path(tmp) / "plugin"
        git_exe = _resolve_git_executable()
        if not git_exe:
            raise PluginOperationError("git is not installed or not in PATH.")

        clone_args = [git_exe, "clone", "--depth", "1"]
        if requested_revision:
            clone_args.append("--no-checkout")
        clone_args.extend([git_url, str(tmp_clone)])
        try:
            result = subprocess.run(
                clone_args,
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=60,
                stdin=subprocess.DEVNULL,
                env=noninteractive_git_env(),
            )
        except FileNotFoundError as e:
            raise PluginOperationError("git is not installed or not in PATH.") from e
        except subprocess.TimeoutExpired as e:
            raise PluginOperationError("Git clone timed out after 60 seconds.") from e
        if result.returncode != 0:
            err = _safe_git_error(result, git_url)
            raise PluginOperationError(f"Git clone failed:\n{err}")

        _scrub_cloned_origin(tmp_clone, git_exe, git_url)
        if requested_revision:
            _checkout_exact_revision(tmp_clone, git_exe, requested_revision)
        installed_revision = _git_head_revision(tmp_clone, git_exe)

        tmp_target = (
            _resolve_subdir_within(tmp_clone, subdir) if subdir else tmp_clone
        )
        has_native_manifest = (tmp_target / "plugin.toml").exists() or (
            tmp_target / "plugin.toml"
        ).exists()
        has_portable_manifest = (tmp_target / "plugin.json").exists() or (
            tmp_target / "plugin.json"
        ).is_symlink()
        if not has_native_manifest and has_portable_manifest:
            try:
                from son_of_anton_cli.agent_plugins import read_agent_plugin_manifest

                manifest, diagnostics = read_agent_plugin_manifest(tmp_target)
                for diagnostic in diagnostics:
                    logger.warning("Agent Plugin install: %s", diagnostic.message)
            except Exception as exc:
                raise PluginOperationError(
                    f"Portable plugin manifest validation failed: {exc}"
                ) from exc
        else:
            manifest = _read_manifest(tmp_target)
        plugin_name = manifest.get("name") or (
            subdir.rstrip("/").rsplit("/", 1)[-1] if subdir else _repo_name_from_url(git_url)
        )
        try:
            target = _sanitize_plugin_name(plugin_name, plugins_dir)
        except ValueError as e:
            raise PluginOperationError(str(e)) from e

        mv = manifest.get("manifest_version")
        if mv is not None:
            try:
                mv_int = int(mv)
            except (ValueError, TypeError):
                raise PluginOperationError(
                    f"Plugin '{plugin_name}' has invalid manifest_version "
                    f"'{mv}' (expected an integer).",
                ) from None
            if mv_int > _SUPPORTED_MANIFEST_VERSION:
                from son_of_anton_cli.config import recommended_update_command

                raise PluginOperationError(
                    f"Plugin '{plugin_name}' requires manifest_version {mv}, "
                    f"but this installer only supports up to {_SUPPORTED_MANIFEST_VERSION}. "
                    f"Run {recommended_update_command()} to update Son of Anton.",
                ) from None

        # Security scan the clone BEFORE anything is moved into place
        # (see ``tools/plugin_guard.py``; inspired by Claude Cowork's skill
        # & plugin scanning). ``scan_decision_cb`` is called with the
        # ScanResult for caution verdicts and may return True to accept the
        # risk interactively. Raises PluginScanBlocked when blocked.
        _scan_plugin_tree(
            tmp_target,
            identifier,
            force=force,
            scan_decision_cb=scan_decision_cb,
        )

        if target.exists() and not force:
            raise PluginOperationError(
                f"Plugin '{plugin_name}' already exists. Use force reinstall "
                f"or run `son-of-anton plugins update {plugin_name}`."
            )
        prior = old_metadata.get(plugin_name)
        if (
            target.exists()
            and requested_revision is None
            and isinstance(prior, dict)
            and prior.get("pinned") is True
        ):
            raise PluginOperationError(
                f"Plugin '{plugin_name}' is pinned. Reinstall it with an explicit "
                "--ref <40-character commit SHA> to change its source or revision."
            )

        new_metadata = dict(old_metadata)
        new_metadata[plugin_name] = {
            "pinned": requested_revision is not None,
            "revision": installed_revision,
            "source": source,
        }
        backup = Path(tmp) / "previous-plugin"
        replaced_existing = target.exists()
        if replaced_existing:
            os.replace(target, backup)
        try:
            os.replace(tmp_target, target)
            _write_install_metadata(new_metadata)
        except Exception:
            if target.exists():
                shutil.rmtree(target)
            if replaced_existing and backup.exists():
                os.replace(backup, target)
            if old_metadata:
                _write_install_metadata(old_metadata)
            else:
                _install_metadata_path().unlink(missing_ok=True)
            raise

    has_yaml = (target / "plugin.toml").exists() or (target / "plugin.toml").exists()
    has_portable = (target / "plugin.json").exists()
    if not has_yaml and not has_portable and not (target / "__init__.py").exists():
        logger.warning(
            "%s has no plugin.toml / __init__.py; may not be a valid plugin",
            plugin_name,
        )

    from rich.console import Console

    _copy_example_files(target, Console())
    installed_manifest = _read_manifest(target)
    installed_name = installed_manifest.get("name") or target.name
    return target, installed_manifest, installed_name


def _get_disabled_set() -> set:
    """Read the disabled plugins set from config.toml.

    An explicit deny-list. A plugin name here never loads, even if also
    listed in ``plugins.enabled``.
    """
    try:
        from son_of_anton_cli.config import load_config
        config = load_config()
        disabled = cfg_get(config, "plugins", "disabled", default=[])
        return set(disabled) if isinstance(disabled, list) else set()
    except Exception:
        return set()


def _save_disabled_set(disabled: set) -> None:
    """Write the disabled plugins list to config.toml."""
    from son_of_anton_cli.config import load_config, save_config
    config = load_config()
    if "plugins" not in config:
        config["plugins"] = {}
    config["plugins"]["disabled"] = sorted(disabled)
    save_config(config)


def _get_enabled_set() -> set:
    """Read the enabled plugins allow-list from config.toml.

    Plugins are opt-in: only names here are loaded. Returns ``set()`` if
    the key is missing (same behaviour as "nothing enabled yet").
    """
    try:
        from son_of_anton_cli.config import load_config
        config = load_config()
        plugins_cfg = config.get("plugins", {})
        if not isinstance(plugins_cfg, dict):
            return set()
        enabled = plugins_cfg.get("enabled", [])
        return set(enabled) if isinstance(enabled, list) else set()
    except Exception:
        return set()


def _save_enabled_set(enabled: set) -> None:
    """Write the enabled plugins list to config.toml."""
    from son_of_anton_cli.config import load_config, save_config
    config = load_config()
    if "plugins" not in config:
        config["plugins"] = {}
    config["plugins"]["enabled"] = sorted(enabled)
    save_config(config)


def _resolve_plugin_key(name: str) -> Optional[str]:
    """Resolve a user-supplied plugin identifier to its canonical registry key.

    Accepts either the bare manifest name (``langfuse``), the directory
    name, or the full path-derived key (``observability/langfuse``) and
    returns the canonical key the loader gates on (``manifest.key`` or, for a
    flat plugin, the bare name). Returns ``None`` when no plugin matches.

    This is the single normalization point so ``son-of-anton plugins enable`` /
    ``disable`` write the same key that ``PluginManager`` matches against —
    nested category plugins (e.g. ``observability/langfuse``) included.
    """
    entries = _discover_all_plugins()
    # 1. Exact match on canonical key or manifest name — always unambiguous.
    for entry in entries:
        # entry = (name, version, description, source, dir_path, key)
        if name == entry[5] or name == entry[0]:
            return entry[5]
    # 2. Fall back to a bare leaf-name match (e.g. "langfuse" ->
    #    "observability/langfuse"), but only when it resolves to exactly one
    #    plugin so we never silently pick the wrong same-named nested plugin.
    leaf_matches = [entry[5] for entry in entries if name == entry[5].split("/")[-1]]
    if len(leaf_matches) == 1:
        return leaf_matches[0]
    return None


def _resolve_plugin_key_and_source(name: str) -> Optional[tuple]:
    """Resolve *name* to ``(canonical_key, source)`` or ``None`` if no match.

    Mirrors :func:`_resolve_plugin_key`'s normalization but also returns the
    plugin's source (``"bundled"``, ``"user"``, ``"project"``, ...) so the
    enable path can tell whether a built-in-override consent prompt is needed.
    """
    entries = _discover_all_plugins()
    for entry in entries:
        # entry = (name, version, description, source, dir_path, key)
        if name == entry[5] or name == entry[0]:
            return (entry[5], entry[3])
    leaf_matches = [
        (entry[5], entry[3]) for entry in entries
        if name == entry[5].split("/")[-1]
    ]
    if len(leaf_matches) == 1:
        return leaf_matches[0]
    return None


def _set_plugin_entry_flag(plugin_id: str, key: str, value: bool) -> None:
    """Write ``plugins.entries.<plugin_id>.<key> = value`` into config.toml."""
    from son_of_anton_cli.config import load_config, save_config
    config = load_config()
    plugins_cfg = config.setdefault("plugins", {})
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
        config["plugins"] = plugins_cfg
    entries = plugins_cfg.setdefault("entries", {})
    if not isinstance(entries, dict):
        entries = {}
        plugins_cfg["entries"] = entries
    entry = entries.setdefault(plugin_id, {})
    if not isinstance(entry, dict):
        entry = {}
        entries[plugin_id] = entry
    entry[key] = bool(value)
    save_config(config)


def cmd_enable(name: str, allow_tool_override: Optional[bool] = None) -> None:
    """Add a plugin to the enabled allow-list (and remove it from disabled).

    For non-bundled plugins, prompt the operator about granting the
    privileged ``allow_tool_override`` capability (replacing built-in tools
    like ``shell_exec`` / ``write_file``). ``allow_tool_override`` is a
    tri-state: ``True`` grants without prompting, ``False`` declines without
    prompting, ``None`` (default) asks interactively. Bundled plugins are
    trusted and never prompted.
    """
    from rich.console import Console
    from son_of_anton_cli.relay_plugin_cutover import (
        LEGACY_RELAY_PLUGIN_KEYS,
        RELAY_PLUGINS_CONFIG_ENV,
    )

    console = Console()
    if name in LEGACY_RELAY_PLUGIN_KEYS:
        console.print(
            f"[red]Plugin '{name}' was removed.[/red] Relay lifecycle is owned "
            f"by Son of Anton core; configure {RELAY_PLUGINS_CONFIG_ENV} instead."
        )
        sys.exit(1)

    # Discover the plugin — check installed (user) AND bundled, including
    # nested category plugins — and normalize to its canonical registry key.
    resolved = _resolve_plugin_key_and_source(name)
    if resolved is None:
        console.print(f"[red]Plugin '{name}' is not installed or bundled.[/red]")
        sys.exit(1)
    key, source = resolved

    if key in LEGACY_RELAY_PLUGIN_KEYS:
        console.print(
            f"[red]Plugin '{key}' was removed.[/red] Relay lifecycle is owned "
            f"by Son of Anton core; configure {RELAY_PLUGINS_CONFIG_ENV} instead."
        )
        sys.exit(1)

    enabled = _get_enabled_set()
    disabled = _get_disabled_set()

    already_enabled = key in enabled and key not in disabled

    if not already_enabled:
        enabled.add(key)
        disabled.discard(key)
        # Drop every alias of this plugin from the disabled list so an
        # explicit disable under a different form can't keep it off. The
        # loader's disable check matches on BOTH the canonical key
        # (``web/firecrawl``) AND the manifest name (``web-firecrawl``);
        # a stale entry under either form makes "explicit disable wins"
        # (plugins.py) silently veto this enable. Discard the key, its
        # bare leaf, and the manifest name. (#40190 follow-up.)
        bare = key.split("/")[-1]
        if bare != key:
            disabled.discard(bare)
        for entry in _discover_all_plugins():
            # entry = (name, version, description, source, dir_path, key)
            if entry[5] == key:
                disabled.discard(entry[0])
                break
        _save_enabled_set(enabled)
        _save_disabled_set(disabled)
        console.print(
            f"[green]✓[/green] Plugin [bold]{key}[/bold] enabled. "
            "Takes effect on next session."
        )
    else:
        console.print(f"[dim]Plugin '{key}' is already enabled.[/dim]")

    # Built-in tool override is a privileged grant. Bundled plugins ship with
    # Son of Anton core and are trusted; every other source needs operator opt-in.
    if source == "bundled":
        return

    # Capability consent (#64228): when the manifest declares capabilities,
    # the consent screen is the canonical grant path — it covers
    # tools.override too, so skip the legacy standalone prompt unless the
    # operator explicitly passed --allow-tool-override/--no-allow-tool-override.
    declared_caps = _declared_capabilities_for_key(key)
    if declared_caps:
        _run_capability_consent(console, key, declared_caps, context="enable")
        if allow_tool_override is not None:
            _resolve_tool_override_grant(console, key, allow_tool_override)
        return

    _resolve_tool_override_grant(console, key, allow_tool_override)


# ── Capability consent flow (#64228) ─────────────────────────────────────────


def _declared_capabilities_from_manifest(manifest: dict, plugin_name: str = "?") -> list:
    """Extract + normalize the ``capabilities:`` declaration from a manifest."""
    from son_of_anton_cli.plugin_capabilities import parse_declared_capabilities

    return parse_declared_capabilities(
        (manifest or {}).get("capabilities"), plugin_name
    )


def _declared_capabilities_for_key(key: str) -> list:
    """Read the declared capabilities for an installed/bundled plugin by key."""
    for entry in _discover_all_plugins():
        # entry = (name, version, description, source, dir_path, key)
        if entry[5] == key or entry[0] == key:
            if entry[3] == "entrypoint":
                from son_of_anton_cli.plugins import discover_entrypoint_manifests

                for manifest in discover_entrypoint_manifests():
                    if key in (manifest.key, manifest.name):
                        return list(manifest.capabilities)
                return []
            dir_path = entry[4]
            if not dir_path:
                return []
            manifest = _read_manifest(Path(dir_path))
            return _declared_capabilities_from_manifest(manifest, entry[0])
    return []


def _print_capability_list(console, capabilities: list) -> None:
    """Render the consent screen body: one line per capability."""
    from son_of_anton_cli.plugin_capabilities import CAPABILITY_REGISTRY

    for cap in capabilities:
        spec = CAPABILITY_REGISTRY.get(cap)
        desc = spec.description if spec else ""
        console.print(f"    [bold]{cap}[/bold] — {desc}")


def _run_capability_consent(
    console,
    plugin_id: str,
    declared: list,
    *,
    context: str = "install",
) -> bool:
    """Show the capability consent screen and record the decision.

    Prints the declared capability list with one-line risk descriptions and
    asks a single Y/n. On consent, the *pending* capabilities are granted
    (recorded under ``plugins.entries.<plugin_id>.granted_capabilities`` with
    a consent hash of the declared set). On decline — or in ANY
    non-interactive context — capabilities stay ungranted (fail closed) and
    the plugin must degrade gracefully via ``ctx.has_capability()``.

    The consent wording deliberately does not imply a code audit: granting a
    capability trusts the plugin author. This is consent + audit, NOT a
    sandbox — an in-process plugin can run arbitrary Python regardless.

    Returns True when consent was granted.
    """
    from son_of_anton_cli.plugin_capabilities import (
        pending_capabilities,
        record_consent,
    )

    pending = pending_capabilities(plugin_id, declared)
    if not pending:
        # Everything declared is already granted — refresh the consent hash
        # so a later declaration change is detected against the current set.
        if declared:
            record_consent(plugin_id, [], declared)
        return True

    verb = "requests" if context == "install" else "now requests"
    console.print(
        f"\n  [yellow]Plugin [bold]{plugin_id}[/bold] {verb} the following "
        "capabilities:[/yellow]"
    )
    _print_capability_list(console, pending)
    console.print(
        "  [dim]Granting trusts the plugin author with these host surfaces. "
        "This is consent, not a sandbox — plugins run as regular Python "
        "in-process.[/dim]"
    )

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        console.print(
            "  [yellow]Non-interactive session: capabilities NOT granted "
            "(fail closed).[/yellow] Run "
            f"`son-of-anton plugins capabilities {plugin_id}` to review and "
            f"`son-of-anton plugins enable {plugin_id}` to grant interactively."
        )
        return False

    try:
        answer = console.input("  Grant these capabilities? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer in {"y", "yes"}:
        record_consent(plugin_id, pending, declared)
        console.print(
            f"  [green]✓[/green] Granted: {', '.join(pending)} "
            f"([dim]plugins.entries.{plugin_id}.granted_capabilities[/dim])"
        )
        return True

    console.print(
        f"  [dim]Declined. {plugin_id} stays enabled with these capabilities "
        "off; it should degrade gracefully (ctx.has_capability()). Re-run "
        f"`son-of-anton plugins enable {plugin_id}` to grant later.[/dim]"
    )
    return False


def _resolve_tool_override_grant(
    console, key: str, allow_tool_override: Optional[bool]
) -> None:
    """Resolve and persist the ``allow_tool_override`` grant for a plugin.

    ``allow_tool_override`` tri-state: True grants, False declines, None
    prompts interactively (defaulting to deny on a non-interactive stdin).
    """
    if allow_tool_override is None:
        # Interactive consent. Default to NO so a blind Enter doesn't grant
        # a privileged capability, and a non-interactive stdin denies safely.
        prompt = (
            "[yellow]Allow this plugin to replace built-in tools "
            "(e.g. shell_exec, write_file)?[/yellow]\n"
            "  This is a privileged capability: an override can intercept "
            "everything the agent routes through that tool.\n"
            "  Grant it? [y/N] "
        )
        try:
            answer = console.input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        allow_tool_override = answer in {"y", "yes"}

    plugin_id = key
    _set_plugin_entry_flag(plugin_id, "allow_tool_override", allow_tool_override)
    if allow_tool_override:
        console.print(
            f"[green]✓[/green] Granted [bold]{key}[/bold] permission to "
            "override built-in tools "
            f"([dim]plugins.entries.{plugin_id}.allow_tool_override: true[/dim])."
        )
    else:
        console.print(
            f"[dim]{key} may not override built-in tools. Re-run "
            f"`son-of-anton plugins enable {key} --allow-tool-override` to grant "
            "this later.[/dim]"
        )


def cmd_disable(name: str) -> None:
    """Remove a plugin from the enabled allow-list (and add to disabled)."""
    from rich.console import Console

    console = Console()
    key = _resolve_plugin_key(name)
    if key is None:
        console.print(f"[red]Plugin '{name}' is not installed or bundled.[/red]")
        sys.exit(1)

    enabled = _get_enabled_set()
    disabled = _get_disabled_set()

    if key not in enabled and key in disabled:
        console.print(f"[dim]Plugin '{key}' is already disabled.[/dim]")
        return

    enabled.discard(key)
    # Drop any legacy bare-name entry from the allow-list too, so a stale
    # bare name can't keep a nested plugin loading after an explicit disable.
    bare = key.split("/")[-1]
    if bare != key:
        enabled.discard(bare)
    disabled.add(key)
    _save_enabled_set(enabled)
    _save_disabled_set(disabled)
    console.print(
        f"[yellow]\u2298[/yellow] Plugin [bold]{key}[/bold] disabled. "
        "Takes effect on next session."
    )


def _read_manifest_info(d: Path, prefix: str):
    """Read a native or portable manifest and return display metadata.

    Returns None if no manifest file exists.
    """
    manifest_file = d / "plugin.toml"
    if not manifest_file.exists():
        manifest_file = d / "plugin.toml"
    if not manifest_file.exists():
        portable_file = d / "plugin.json"
        if not portable_file.exists() and not portable_file.is_symlink():
            return None
        try:
            from son_of_anton_cli.agent_plugins import read_agent_plugin_manifest

            manifest, _ = read_agent_plugin_manifest(d)
            name = manifest["name"]
            key = f"{prefix}/{d.name}" if prefix else name
            return (
                name,
                manifest.get("version", ""),
                manifest.get("description", ""),
                key,
            )
        except Exception:
            return None
    name = d.name
    version = ""
    description = ""
    try:
        import tomllib

        with open(manifest_file, encoding="utf-8") as f:
            manifest = tomllib.loads(f.read()) or {}
        name = manifest.get("name", d.name)
        version = manifest.get("version", "")
        description = manifest.get("description", "")
    except Exception:
        pass
    key = f"{prefix}/{d.name}" if prefix else name
    return name, version, description, key


# Manifest kinds that are active-by-default when bundled: backends auto-load,
# platforms register lazily but are available out of the box, model providers
# run through providers/ discovery (see PluginManager.discover_and_load).


def _scan_level(
    base: Path,
    source: str,
    skip_names: set,
    prefix: str,
    depth: int,
    seen: dict,
) -> None:
    """Recursive directory scan matching PluginManager._scan_directory_level.

    Populates *seen* with key -> (name, version, description, source, dir, key).
    """
    if not base.is_dir():
        return
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        if depth == 0 and skip_names and d.name in skip_names:
            continue
        info = _read_manifest_info(d, prefix)
        if info is not None:
            name, version, description, key = info
            if key in seen and source == "bundled":
                continue
            src_label = source
            if source == "user" and (d / ".git").exists():
                src_label = "git"
            seen[key] = (name, version, description, src_label, d, key)
            continue
        if depth >= 1:
            continue
        sub_prefix = f"{prefix}/{d.name}" if prefix else d.name
        _scan_level(d, source, set(), sub_prefix, depth + 1, seen)


def _discover_all_plugins() -> list:
    """Return a list of (name, version, description, source, dir_path, key) for
    every plugin the loader can see — user + bundled + project + entry point.

    Matches the ordering/dedup of ``PluginManager.discover_and_load``:
    bundled first, then user, then project, then entry points. Later sources
    override earlier ones on key collision.
    """
    seen: dict = {}  # key -> (name, version, description, source, path, key)

    # Bundled (<repo>/plugins/<name>/), excluding memory/, context_engine/
    # and model-providers/ — model providers load through the dedicated
    # provider registry (providers/__init__.py), not the general PluginManager
    # opt-in surface, so listing them as toggleable plugins is misleading.
    from son_of_anton_cli.plugins import get_bundled_plugins_dir
    repo_plugins = get_bundled_plugins_dir()
    for base, source, skip in (
        (repo_plugins, "bundled", {"memory", "context_engine", "model-providers"}),
        (_plugins_dir(), "user", set()),
    ):
        _scan_level(base, source, skip, "", 0, seen)

    # Entry-point plugins (installed as Python packages; no plugin directory).
    for name, version, description, path in _discover_entrypoint_plugins():
        seen[name] = (name, version, description, "entrypoint", path, name)
    return list(seen.values())


def _discover_entrypoint_plugins() -> list[tuple[str, str, str, str]]:
    """Return plugin entries advertised through ``son_of_anton_agent.plugins``.

    Entry-point plugins are installed as Python packages, so they do not have a
    plugin directory under ``~/.son-of-anton/plugins``. Include package metadata here
    so ``son-of-anton plugins list`` can show and enable them.
    """
    from son_of_anton_cli.plugins import ENTRY_POINTS_GROUP

    try:
        eps = importlib.metadata.entry_points()
        if hasattr(eps, "select"):
            group_eps = eps.select(group=ENTRY_POINTS_GROUP)
        elif isinstance(eps, dict):
            group_eps = eps.get(ENTRY_POINTS_GROUP, [])
        else:
            group_eps = [ep for ep in eps if ep.group == ENTRY_POINTS_GROUP]
    except Exception as exc:
        logger.debug("Entry-point plugin discovery failed: %s", exc)
        return []

    entries: list[tuple[str, str, str, str]] = []
    for ep in group_eps:
        version = ""
        description = ""
        dist = getattr(ep, "dist", None)
        metadata = getattr(dist, "metadata", None)
        if metadata is not None:
            version = str(getattr(dist, "version", "") or "")
            description = str(metadata.get("Summary", "") or "")
        entries.append((ep.name, version, description, ep.value))
    return entries


def _plugin_status(name: str, enabled: set, disabled: set, key: str = "") -> str:
    """Return the user-facing activation state for a plugin name or key."""
    if name in disabled or key in disabled:
        return "disabled"
    if name in enabled or key in enabled:
        return "enabled"
    return "not enabled"


# ---------------------------------------------------------------------------
# Provider plugin discovery helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Composite plugins UI
# ---------------------------------------------------------------------------


