"""Sandboxed Python script execution for the physics mode.

**The interpreter.** Under the sealed Nix install there is no ``python`` on
``PATH`` at all — the gateway unit's ``PATH`` is the son-of-anton wrapper plus
coreutils/git — so the interpreter is resolved explicitly, and the resolution
is inspectable (:func:`resolve_interpreter`, :func:`runtime_summary`) so the
code-execution instructions can advertise the packages that are actually
importable instead of a hardcoded list.

**The sandbox.** Model-authored code runs under `bubblewrap
<https://github.com/containers/bubblewrap>`_ with:

* no network (``--unshare-net``),
* a filesystem containing only ``/nix/store`` (read-only), the run's own
  workspace (read-write), and whatever data directories the policy declares
  (read-only) — no ``$HOME``, no ``~/.ssh``, no ``~/.son-of-anton``,
* a cleared environment: an explicit allowlist, never the parent's secrets,
* fresh PID/IPC/UTS/cgroup namespaces and ``--die-with-parent``,
* ``RLIMIT_CORE``/``RLIMIT_FSIZE`` backstops.

Without ``bwrap`` it fails closed. Policy is read from the ``physics:``
section of config.toml:

``python``
    Interpreter for computations. Should be a Python environment carrying the
    scientific stack (see ``nix/physics-runtime.nix``); the agent's own venv
    ships none of it.
``data_dirs``
    Read-only paths to expose. A run may add more via the problem spec.
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..core.config import physics_config as _physics_config

#: Environment variables forwarded into the sandbox. Everything else is
#: dropped — in particular every ``*_API_KEY`` / ``*_TOKEN`` the agent holds.
ENV_PASSTHROUGH = (
    "LANG",
    "LC_ALL",
    "LC_NUMERIC",
    "TZ",
    "TZDIR",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_MAX_THREADS",
)

#: Packages ``runtime_summary`` probes for, in the order they are reported.
PROBE_PACKAGES = (
    "analysis_utilities",
    "numpy",
    "scipy",
    "sympy",
    "matplotlib",
    "pandas",
    "sklearn",
    "xgboost",
    "h5py",
    "torch",
    "ROOT",
)

#: RLIMIT_FSIZE for one computation.
FILE_SIZE_LIMIT_MB = 8192


@dataclass
class ExecutionResult:
    """Result of a sandboxed Python execution."""

    stdout: str
    stderr: str
    returncode: int
    timed_out: bool


@dataclass
class SandboxPolicy:
    """Confinement policy for one execution."""

    interpreter: str = ""
    #: Read-write root. Defaults to the execution ``cwd``.
    workspace: Path | None = None
    #: Read-only paths made visible inside the sandbox.
    data_dirs: tuple[Path, ...] = ()

    @classmethod
    def from_config(cls, extra_data_dirs=()) -> "SandboxPolicy":
        """Build a policy from the ``physics:`` section of config.toml."""
        cfg = _physics_config()
        dirs: list[Path] = []
        for raw in list(cfg.get("data_dirs") or []) + list(extra_data_dirs or []):
            path = Path(os.path.expanduser(str(raw))).resolve()
            if path.exists() and path not in dirs:
                dirs.append(path)
        return cls(interpreter=resolve_interpreter(cfg), data_dirs=tuple(dirs))


def resolve_interpreter(cfg: dict | None = None) -> str:
    """Return the Python interpreter computations run under.

    ``physics.python`` in config.toml when it exists, else ``sys.executable``.
    A bare ``"python"`` is never used — the installs this runs on do not have
    one on ``PATH``.
    """
    cfg = _physics_config() if cfg is None else cfg
    candidate = os.path.expanduser(str(cfg.get("python") or "").strip())
    if candidate and os.path.isabs(candidate) and os.path.exists(candidate):
        return candidate
    return sys.executable


def _probe_packages(interpreter: str) -> dict[str, str]:
    """Import-probe *interpreter* for the scientific stack. Never raises."""
    script = (
        "import importlib,json\n"
        f"names={list(PROBE_PACKAGES)!r}\n"
        "out={}\n"
        "for n in names:\n"
        "    try:\n"
        "        m=importlib.import_module(n)\n"
        "        out[n]=str(getattr(m,'__version__','') or 'present')\n"
        "    except Exception:\n"
        "        pass\n"
        "print(json.dumps(out))\n"
    )
    try:
        proc = subprocess.run(
            [interpreter, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            return {}
        return json.loads(proc.stdout.strip() or "{}")
    except Exception:
        return {}


_RUNTIME_CACHE: dict[str, dict] = {}


def runtime_summary(interpreter: str | None = None) -> dict:
    """Describe the computation runtime: interpreter, version, packages.

    Cached per interpreter — the probe spawns a process, and the prompts that
    consume this are built once per run.
    """
    interpreter = interpreter or resolve_interpreter()
    if interpreter in _RUNTIME_CACHE:
        return _RUNTIME_CACHE[interpreter]
    packages = _probe_packages(interpreter)
    version = ""
    try:
        proc = subprocess.run(
            [interpreter, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        version = proc.stdout.strip()
    except Exception:
        version = ""
    summary = {
        "interpreter": interpreter,
        "python_version": version,
        "packages": packages,
    }
    _RUNTIME_CACHE[interpreter] = summary
    return summary


def describe_runtime(interpreter: str | None = None) -> str:
    """One-paragraph, model-facing description of what the runtime provides."""
    summary = runtime_summary(interpreter)
    packages = summary["packages"]
    version = summary["python_version"] or "3"
    if packages:
        listed = ", ".join(
            f"{name} {ver}" if ver != "present" else name
            for name, ver in packages.items()
        )
        available = f"Python {version}, standard library, and: {listed}."
    else:
        available = (
            f"Python {version} and the standard library ONLY. No numpy, scipy, "
            "sympy, matplotlib or pandas — write plain-Python computations."
        )
    return available


def runtime_guidance(interpreter: str | None = None, timeout: int = 60) -> str:
    """House-library guidance for whatever the runtime actually provides.

    Separate from :func:`describe_runtime`, which only lists what imports. A
    model told that ``analysis_utilities`` is importable still writes numpy and
    matplotlib, because that is what it has seen; it has to be told what the
    library is for. See :mod:`autophysicist.utils.runtime_notes`.
    """
    from .runtime_notes import notes_for

    return notes_for(runtime_summary(interpreter).get("packages") or {}, timeout)


def bwrap_path() -> str | None:
    """Return the ``bwrap`` executable, or ``None`` when it is not installed."""
    return shutil.which("bwrap")


def _limit_resources() -> None:
    """``preexec_fn``: no core dumps, and a cap on any one file written."""
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass
    size = FILE_SIZE_LIMIT_MB * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
    except (ValueError, OSError):
        pass


def _sandbox_env(interpreter: str) -> dict[str, str]:
    """Build the environment visible inside the sandbox."""
    env = {
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "PATH": f"{Path(interpreter).parent}:/bin",
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": "/tmp/matplotlib",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    for name in ENV_PASSTHROUGH:
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def build_bwrap_command(
    bwrap: str,
    policy: SandboxPolicy,
    script_path: Path,
    cwd: Path,
    interpreter: str,
) -> list[str]:
    """Assemble the ``bwrap`` argv for one execution."""
    workspace = Path(policy.workspace or cwd).resolve()
    argv = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--clearenv",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--unshare-net",
        "--ro-bind", "/nix/store", "/nix/store",
    ]

    # A shell. ROOT's TUnixSystem shells out while initializing its
    # interpreter and segfaults outright when /bin/sh is missing — a stack in
    # TUnixSystem::FindDynamicLibrary with no error message — and any script
    # calling subprocess/os.system needs one too. On NixOS /bin holds this one
    # symlink and nothing else.
    argv += ["--ro-bind-try", "/bin/sh", "/bin/sh"]

    # Read-only data, bound before the workspace so a data dir that happens to
    # sit inside the workspace does not shadow the writable mount.
    for data_dir in policy.data_dirs:
        argv += ["--ro-bind-try", str(data_dir), str(data_dir)]

    argv += ["--bind", str(workspace), str(workspace)]

    for name, value in _sandbox_env(interpreter).items():
        argv += ["--setenv", name, value]

    argv += ["--chdir", str(cwd), interpreter, str(script_path)]
    return argv


def execute_python(
    script_path: str | Path,
    timeout: int = 60,
    cwd: str | Path | None = None,
    policy: SandboxPolicy | None = None,
) -> ExecutionResult:
    """Execute a Python script under the sandbox. Returns an ExecutionResult.

    *policy* defaults to :meth:`SandboxPolicy.from_config`. The script is run
    with ``cwd`` as its working directory; ``policy.workspace`` (defaulting to
    ``cwd``) is the only writable path.
    """
    script_path = Path(script_path).resolve()
    cwd_path = Path(cwd).resolve() if cwd else script_path.parent
    policy = policy or SandboxPolicy.from_config()
    interpreter = policy.interpreter or resolve_interpreter()

    bwrap = bwrap_path()
    if not bwrap:
        return ExecutionResult(
            stdout="",
            stderr=(
                "SANDBOX ERROR: bubblewrap (bwrap) is not installed, so "
                "computations cannot be confined. Install bubblewrap."
            ),
            returncode=-1,
            timed_out=False,
        )
    argv = build_bwrap_command(bwrap, policy, script_path, cwd_path, interpreter)

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd_path),
            start_new_session=True,
            preexec_fn=_limit_resources,  # noqa: PLW1509
            check=False,
        )
        return ExecutionResult(
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            timed_out=False,
        )
    except subprocess.TimeoutExpired:
        return ExecutionResult(
            stdout="",
            stderr=f"TIMEOUT: Script exceeded {timeout}s limit.",
            returncode=-1,
            timed_out=True,
        )
    except Exception as e:
        return ExecutionResult(
            stdout="",
            stderr=f"EXECUTION ERROR: {e}",
            returncode=-1,
            timed_out=False,
        )
