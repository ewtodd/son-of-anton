"""`renco update` must self-heal the ``renco-acp`` launcher.

ACP hosts (Zed, JetBrains, Buzz Desktop) resolve the agent by the
``renco-acp`` command name on the login-shell PATH. Fresh installs get the
launcher from ``scripts/install.sh``; existing installs get it from
``_ensure_acp_launcher()`` during ``renco update``.
"""

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from renco_cli.main import _ensure_acp_launcher


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("RENCO_HOME", str(tmp_path / ".renco"))
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    return bin_dir








def test_does_not_follow_symlink_into_venv(fake_home, tmp_path):
    """#21454 failure mode: never write through a symlinked renco-acp."""
    (fake_home / "renco").write_text("#!/bin/sh\n", encoding="utf-8")
    console_script = tmp_path / "venv" / "bin" / "renco-acp"
    console_script.parent.mkdir(parents=True)
    marker = "#!/usr/bin/env python\n# real console script\n"
    console_script.write_text(marker, encoding="utf-8")
    (fake_home / "renco-acp").symlink_to(console_script)

    _ensure_acp_launcher()

    assert console_script.read_text(encoding="utf-8") == marker
    assert (fake_home / "renco-acp").is_symlink()






def test_unwritable_bin_dir_is_skipped(fake_home):
    (fake_home / "renco").write_text("#!/bin/sh\n", encoding="utf-8")
    if os.geteuid() == 0:
        pytest.skip("root ignores directory write permissions")
    fake_home.chmod(0o555)
    try:
        _ensure_acp_launcher()  # must not raise
        assert not (fake_home / "renco-acp").exists()
    finally:
        fake_home.chmod(0o755)
