"""Sandboxed Python script execution for the physics modes.

Two problems this module exists to solve.

**The interpreter.** The old implementation ran ``["python", script]``. Under
the sealed Nix install there is no ``python`` on ``PATH`` at all — the gateway
unit's ``PATH`` is the son-of-anton wrapper plus coreutils/git — so every
computation a physics run attempted died with ``[Errno 2] No such file or
directory: 'python'``. The interpreter is now resolved explicitly, and the
resolution is inspectable (:func:`resolve_interpreter`, :func:`runtime_summary`)
so the ``execute_python`` tool description can advertise the packages that are
actually importable instead of a hardcoded list.

**The sandbox.** The old implementation was named "sandbox" but was a bare
``subprocess.run`` inheriting ``os.environ`` — model-authored code ran as the
user with the whole home directory writable and every API key in the
environment. Scripts now run under `bubblewrap
<https://github.com/containers/bubblewrap>`_ with:

* no network (``--unshare-net``) unless the policy opts in,
* a filesystem containing only the interpreter's store paths (read-only), the
  run's own workspace (read-write), and whatever data directories the policy
  declares (read-only) — no ``$HOME``, no ``~/.ssh``, no ``~/.son-of-anton``,
* a cleared environment: an explicit allowlist, never the parent's secrets,
* fresh PID/IPC/UTS/cgroup namespaces and ``--die-with-parent``,
* ``RLIMIT_CORE``/``RLIMIT_FSIZE``/``RLIMIT_CPU`` backstops.

Policy is read from the ``physics:`` section of config.yaml:

``python``
    Interpreter for computations. Should be a Python environment carrying the
    scientific stack (see ``nix/physics-runtime.nix``); the agent's own venv
    ships none of it.
``sandbox``
    ``auto`` (default — sandbox when ``bwrap`` is present, else refuse),
    ``bwrap`` (require it), or ``off`` (run unconfined; opt-in only).
``data_dirs``
    Read-only paths to expose. A run may add more via the problem spec.
``sandbox_net``
    ``true`` to leave the network reachable. Default ``false``.
``memory_limit_mb`` / ``file_size_limit_mb`` / ``cpu_seconds``
    ``RLIMIT_AS`` / ``RLIMIT_FSIZE`` / ``RLIMIT_CPU``. ``0`` disables the limit.
    ``cpu_seconds`` is off by default: it counts CPU-seconds summed across
    threads, so a threaded BLAS job burns it many times faster than wall
    time and the wall-clock timeout is the honest limit for those.
"""

from __future__ import annotations

import os
import resource
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

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

_DEFAULT_FILE_SIZE_LIMIT_MB = 8192


class SandboxUnavailableError(RuntimeError):
    """Raised when a sandbox is required but ``bwrap`` is not usable."""


@dataclass
class ExecutionResult:
    """Result of a sandboxed Python execution."""

    stdout: str
    stderr: str
    returncode: int
    timed_out: bool
    sandboxed: bool = False
    interpreter: str = ""


@dataclass
class SandboxPolicy:
    """Confinement policy for one execution."""

    interpreter: str = ""
    #: Read-write root. Defaults to the execution ``cwd``.
    workspace: Path | None = None
    #: Read-only paths made visible inside the sandbox.
    data_dirs: tuple[Path, ...] = ()
    mode: str = "auto"  # auto | bwrap | off
    network: bool = False
    memory_limit_mb: int = 0
    file_size_limit_mb: int = _DEFAULT_FILE_SIZE_LIMIT_MB
    cpu_seconds: int = 0
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, extra_data_dirs=()) -> "SandboxPolicy":
        """Build a policy from the ``physics:`` section of config.yaml."""
        cfg = _physics_config()
        dirs: list[Path] = []
        for raw in list(cfg.get("data_dirs") or []) + list(extra_data_dirs or []):
            path = Path(os.path.expanduser(str(raw))).resolve()
            if path.exists() and path not in dirs:
                dirs.append(path)
        mode = str(cfg.get("sandbox") or "auto").strip().lower() or "auto"
        if mode not in {"auto", "bwrap", "off"}:
            mode = "auto"
        return cls(
            interpreter=resolve_interpreter(cfg),
            data_dirs=tuple(dirs),
            mode=mode,
            network=bool(cfg.get("sandbox_net") or False),
            memory_limit_mb=int(cfg.get("memory_limit_mb") or 0),
            file_size_limit_mb=int(
                cfg.get("file_size_limit_mb") or _DEFAULT_FILE_SIZE_LIMIT_MB
            ),
            cpu_seconds=int(cfg.get("cpu_seconds") or 0),
        )


def _physics_config() -> dict:
    """Return the ``physics:`` mapping from config.yaml, or ``{}``."""
    try:
        from son_of_anton_cli.config import load_config

        section = (load_config() or {}).get("physics")
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def resolve_interpreter(cfg: dict | None = None) -> str:
    """Return the Python interpreter computations run under.

    Resolution order: ``physics.python`` in config.yaml,
    ``SON_OF_ANTON_PHYSICS_PYTHON``, ``SON_OF_ANTON_PYTHON`` (set by the Nix
    wrapper), then ``sys.executable``. A bare ``"python"`` is never used — the
    installs this runs on do not have one on ``PATH``.
    """
    cfg = _physics_config() if cfg is None else cfg
    candidates = (
        str(cfg.get("python") or ""),
        os.environ.get("SON_OF_ANTON_PHYSICS_PYTHON", ""),
        os.environ.get("SON_OF_ANTON_PYTHON", ""),
    )
    for candidate in candidates:
        candidate = os.path.expanduser(candidate.strip())
        if not candidate:
            continue
        if os.path.isabs(candidate):
            if os.path.exists(candidate):
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
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
        import json

        return json.loads(proc.stdout.strip() or "{}")
    except Exception:
        return {}


_RUNTIME_CACHE: dict[str, dict] = {}


def runtime_summary(interpreter: str | None = None) -> dict:
    """Describe the computation runtime: interpreter, packages, confinement.

    Cached per interpreter — the probe spawns a process, and the tool
    descriptions that consume this are built once per agent construction.
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
        "bwrap": bwrap_path() or "",
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


def runtime_guidance(
    interpreter: str | None = None, extra: str = "", timeout: int = 60
) -> str:
    """House-library guidance for whatever the runtime actually provides.

    Separate from :func:`describe_runtime`, which only lists what imports. A
    model told that ``analysis_utilities`` is importable still writes numpy and
    matplotlib, because that is what it has seen; it has to be told what the
    library is for. See :mod:`physics_intern.utils.runtime_notes`.
    """
    from .runtime_notes import notes_for

    if not extra:
        extra = str(_physics_config().get("runtime_notes") or "")
    return notes_for(
        runtime_summary(interpreter).get("packages") or {}, extra, timeout
    )


def bwrap_path() -> str | None:
    """Return the ``bwrap`` executable, or ``None`` when it is not installed."""
    return shutil.which("bwrap")


def _ro_bind_roots(interpreter: str) -> list[Path]:
    """Read-only filesystem roots the interpreter needs in order to start."""
    roots: list[Path] = []

    def add(path: Path) -> None:
        path = Path(path)
        if path.exists() and path not in roots:
            roots.append(path)

    store = Path("/nix/store")
    if store.is_dir():
        add(store)
    real = Path(interpreter).resolve()
    # The interpreter's own prefix, when it is not already inside the store.
    if not str(real).startswith("/nix/store"):
        add(real.parent.parent)
        for base in ("/usr", "/lib", "/lib64", "/bin"):
            add(Path(base))
    return roots


def _rlimit_setter(policy: SandboxPolicy):
    """Return a ``preexec_fn`` applying the policy's resource limits."""

    def _apply() -> None:
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ValueError, OSError):
            pass
        if policy.file_size_limit_mb > 0:
            size = policy.file_size_limit_mb * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
            except (ValueError, OSError):
                pass
        if policy.memory_limit_mb > 0:
            mem = policy.memory_limit_mb * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            except (ValueError, OSError):
                pass
        # Opt-in CPU-time backstop. Off by default: RLIMIT_CPU sums CPU time
        # across threads, so a 16-thread BLAS job exhausts it in a sixteenth of
        # the wall time and legitimate work gets killed. The wall-clock timeout
        # is the limit that actually matches what the caller asked for.
        if policy.cpu_seconds > 0:
            try:
                resource.setrlimit(
                    resource.RLIMIT_CPU, (policy.cpu_seconds, policy.cpu_seconds + 5)
                )
            except (ValueError, OSError):
                pass

    return _apply


def _sandbox_env(policy: SandboxPolicy, interpreter: str) -> dict[str, str]:
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
    env.update(policy.extra_env)
    return env


def build_bwrap_command(
    policy: SandboxPolicy,
    script_path: Path,
    cwd: Path,
    interpreter: str,
) -> list[str]:
    """Assemble the ``bwrap`` argv for one execution."""
    bwrap = bwrap_path()
    if not bwrap:
        raise SandboxUnavailableError("bwrap is not installed")

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
    ]
    if not policy.network:
        argv.append("--unshare-net")
    else:
        # Only what TLS needs; still no home, no config, no credentials.
        for path in ("/etc/resolv.conf", "/etc/ssl", "/etc/static/ssl", "/etc/hosts"):
            if Path(path).exists():
                argv += ["--ro-bind", path, path]

    for root in _ro_bind_roots(interpreter):
        argv += ["--ro-bind", str(root), str(root)]

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

    for name, value in _sandbox_env(policy, interpreter).items():
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

    if policy.mode == "off":
        argv = [interpreter, str(script_path)]
        sandboxed = False
    else:
        if not bwrap_path():
            if policy.mode == "bwrap":
                return ExecutionResult(
                    stdout="",
                    stderr=(
                        "SANDBOX ERROR: physics.sandbox is 'bwrap' but bubblewrap "
                        "is not installed. Install bubblewrap, or set "
                        "physics.sandbox: off in config.yaml to run unconfined."
                    ),
                    returncode=-1,
                    timed_out=False,
                    interpreter=interpreter,
                )
            return ExecutionResult(
                stdout="",
                stderr=(
                    "SANDBOX ERROR: bubblewrap (bwrap) is not installed, so "
                    "computations cannot be confined. Install bubblewrap, or "
                    "set physics.sandbox: off in config.yaml to accept running "
                    "model-authored code unconfined."
                ),
                returncode=-1,
                timed_out=False,
                interpreter=interpreter,
            )
        argv = build_bwrap_command(policy, script_path, cwd_path, interpreter)
        sandboxed = True

    env = _sandbox_env(policy, interpreter) if not sandboxed else None
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd_path),
            env=env,
            start_new_session=True,
            preexec_fn=_rlimit_setter(policy),  # noqa: PLW1509
            check=False,
        )
        return ExecutionResult(
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            timed_out=False,
            sandboxed=sandboxed,
            interpreter=interpreter,
        )
    except subprocess.TimeoutExpired:
        return ExecutionResult(
            stdout="",
            stderr=f"TIMEOUT: Script exceeded {timeout}s limit.",
            returncode=-1,
            timed_out=True,
            sandboxed=sandboxed,
            interpreter=interpreter,
        )
    except Exception as e:
        return ExecutionResult(
            stdout="",
            stderr=f"EXECUTION ERROR: {e}",
            returncode=-1,
            timed_out=False,
            sandboxed=sandboxed,
            interpreter=interpreter,
        )
