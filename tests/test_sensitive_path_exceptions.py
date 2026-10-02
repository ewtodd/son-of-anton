"""Tests for the ``security.sensitive_path_exceptions`` config key.

The file-tools sensitive-path guard refuses writes to ``/etc/``, ``/boot/``,
and other system prefixes unconditionally (even in yolo mode) — by design,
to stop a prompt-injected agent from silently mutating OS-critical files.
On a nixos-managed host, ``/etc/nixos/`` is the declarative config source
of truth and *should* be agent-editable. This key lets the user whitelist
path prefixes without weakening the blanket guard for everything else.

Invariants tested (behavior contracts, not snapshots):

- With no exceptions configured, ``/etc/nixos/...`` is refused (current
  default behavior preserved).
- With ``/etc/nixos/`` in the exception list, writes under that prefix
  are allowed.
- With ``/etc/nixos/`` in the exception list, writes to other ``/etc/``
  paths (e.g. ``/etc/passwd``) are still refused — the exception is
  narrow, not a blanket /etc/ disable.
- A malformed (non-list) exception value is treated as empty (fail-safe:
  the guard stays on).
"""


def _make_write(target: str) -> str:
    """Return the sensitive-path guard's verdict for ``target`` (empty = allowed)."""
    from tools.file_tools import _check_sensitive_path
    err = _check_sensitive_path(target, task_id="default")
    return err or ""


class TestSensitivePathExceptions:
    def test_default_refuses_etc_nixos(self, monkeypatch):
        """Without exceptions, /etc/nixos is still refused."""
        monkeypatch.setattr(
            "tools.file_tools._sensitive_path_exceptions", lambda: [],
        )
        err = _make_write("/etc/nixos/hosts/foo/configuration.nix")
        assert "sensitive system path" in err

    def test_exception_allows_etc_nixos(self, monkeypatch):
        """With /etc/nixos/ in the list, that prefix is allowed."""
        monkeypatch.setattr(
            "tools.file_tools._sensitive_path_exceptions",
            lambda: ["/etc/nixos/"],
        )
        err = _make_write("/etc/nixos/hosts/foo/configuration.nix")
        assert err == ""

    def test_exception_does_not_open_all_etc(self, monkeypatch):
        """Whitelisting /etc/nixos/ must NOT open /etc/passwd."""
        monkeypatch.setattr(
            "tools.file_tools._sensitive_path_exceptions",
            lambda: ["/etc/nixos/"],
        )
        err = _make_write("/etc/passwd")
        assert "sensitive system path" in err

    def test_exception_does_not_open_boot(self, monkeypatch):
        """Whitelisting /etc/nixos/ must NOT open /boot/."""
        monkeypatch.setattr(
            "tools.file_tools._sensitive_path_exceptions",
            lambda: ["/etc/nixos/"],
        )
        err = _make_write("/boot/loader/entries/foo.conf")
        assert "sensitive system path" in err

    def test_malformed_exceptions_fail_safe(self, monkeypatch):
        """A non-list value is treated as empty — guard stays on."""
        monkeypatch.setattr(
            "tools.file_tools._sensitive_path_exceptions", lambda: "not-a-list",
        )
        err = _make_write("/etc/nixos/hosts/foo/configuration.nix")
        assert "sensitive system path" in err

    def test_empty_list_preserves_default(self, monkeypatch):
        """An empty list is the same as no config — guard stays on."""
        monkeypatch.setattr(
            "tools.file_tools._sensitive_path_exceptions", lambda: [],
        )
        err = _make_write("/etc/nixos/hosts/foo/configuration.nix")
        assert "sensitive system path" in err

    def test_multiple_exceptions(self, monkeypatch):
        """Multiple prefixes in the list are all honored."""
        monkeypatch.setattr(
            "tools.file_tools._sensitive_path_exceptions",
            lambda: ["/etc/nixos/", "/home/user/config/"],
        )
        assert _make_write("/etc/nixos/flake.nix") == ""
        assert _make_write("/home/user/config/app.yaml") == ""
        # /etc/passwd still refused
        assert "sensitive system path" in _make_write("/etc/passwd")

    def test_exception_prefix_boundary(self, monkeypatch):
        """/etc/nixos/ must not match /etc/nixos-evil/ (prefix is exact)."""
        monkeypatch.setattr(
            "tools.file_tools._sensitive_path_exceptions",
            lambda: ["/etc/nixos/"],
        )
        # /etc/nixos-evil/ does NOT start with /etc/nixos/
        err = _make_write("/etc/nixos-evil/configuration.nix")
        assert "sensitive system path" in err
