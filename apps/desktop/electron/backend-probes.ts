/**
 * backend-probes.ts
 *
 * Cheap "does this candidate backend actually work" checks used by
 * resolveRencoBackend (main.ts). The resolver walks a ladder of
 * candidates -- bootstrap marker, `renco` on PATH, system Python with
 * renco_cli installed -- and historically returned the first candidate
 * whose binary existed on disk. That assumption breaks when a user has
 * a pre-installed Python 3.11-3.13 (so findSystemPython() returns a
 * path) but no renco_cli in its site-packages: the resolver hands back
 * a backend the spawn step can't actually run, and the user gets a
 * dead-on-arrival "ModuleNotFoundError: No module named 'renco_cli'"
 * instead of the first-launch installer.
 *
 * These probes give the resolver a way to verify a candidate before
 * trusting it. Failure (non-zero exit, exception, timeout) means "skip
 * this rung, try the next one"; success means "spawn this for real."
 * Falling off the bottom of the ladder lands on the bootstrap-needed
 * sentinel, which is exactly what we want when nothing pre-existing
 * actually works.
 *
 * Both probes are deliberately fast and forgiving:
 *   - default 15s timeout (5s was too short on cold Windows disks / AV;
 *     issue #61764 death-loop) with RENCO_PROBE_TIMEOUT_MS override
 *   - one automatic retry after a timeout before declaring the runtime dead
 *   - stdio ignored (we only care about exit code; stdout/stderr are
 *     not surfaced to the user, just to recentRencoLog for forensics
 *     via the caller's catch block if it chooses)
 *   - any throw -> false (never propagate -- resolver wants a boolean)
 *
 * Kept in a standalone ts module so it can be unit-tested with
 * `node --test` without dragging in the electron runtime (same pattern
 * as bootstrap-platform.ts and hardening.ts).
 */

import { execFileSync } from 'node:child_process'

/** Default probe budget. 5s false-negativeed healthy Windows cold starts (#61764). */
const DEFAULT_PROBE_TIMEOUT_MS = 15_000

/**
 * Resolve the backend probe timeout (ms).
 * Honours RENCO_PROBE_TIMEOUT_MS when it parses as a positive integer.
 */
function resolveProbeTimeoutMs(env: NodeJS.ProcessEnv = process.env): number {
  const raw = env.RENCO_PROBE_TIMEOUT_MS

  if (raw == null || raw === '') {
    return DEFAULT_PROBE_TIMEOUT_MS
  }

  const n = Number.parseInt(String(raw), 10)

  if (!Number.isFinite(n) || n <= 0) {
    return DEFAULT_PROBE_TIMEOUT_MS
  }

  // Clamp absurd values (ms) so a typo can't hang startup forever.
  return Math.min(n, 120_000)
}

const PROBE_TIMEOUT_MS = resolveProbeTimeoutMs()

function isTimeoutError(err: unknown): boolean {
  if (!err || typeof err !== 'object') {
    return false
  }

  const e = err as { code?: string; killed?: boolean; signal?: string }

  if (e.killed === true) {
    return true
  }

  if (e.code === 'ETIMEDOUT') {
    return true
  }

  // Node marks timed-out execFileSync with SIGTERM on some platforms.
  if (e.signal === 'SIGTERM') {
    return true
  }

  return false
}

/**
 * Run execFileSync; on timeout only, retry once before failing.
 * Non-timeout failures (ENOENT, non-zero exit) fail immediately.
 */
function execProbeSync(
  command: string,
  args: string[],
  options: {
    cwd?: string
    env?: NodeJS.ProcessEnv
    stdio: 'ignore'
    timeout: number
    shell?: boolean
    windowsHide?: boolean
  }
): void {
  try {
    execFileSync(command, args, options)
  } catch (err) {
    if (!isTimeoutError(err)) {
      throw err
    }

    // One cold-cache / AV miss should not force renco-setup --update (#61764).
    execFileSync(command, args, options)
  }
}

/**
 * Return the Python snippet used to verify Renco can import far enough to
 * launch the CLI. Kept exported for tests so dependency regressions are
 * caught without needing a real broken venv fixture.
 *
 * @returns {string}
 */
function rencoRuntimeImportProbe() {
  return 'import yaml; import dotenv; import renco_cli.config'
}

/**
 * Return true iff the Renco runtime import probe exits 0.
 *
 * Used to gate the "fallback to system Python with renco_cli installed"
 * rung of resolveRencoBackend. Without this, a system Python 3.11-3.13
 * registered in PEP 514 makes findSystemPython() succeed regardless of
 * whether renco_cli has actually been pip-installed into its
 * site-packages -- and the resolver returns a backend that immediately
 * dies on spawn.
 *
 * The probe intentionally imports renco_cli.config, not just the top-level
 * package: a broken/empty Windows launcher venv can still see the source tree
 * through PYTHONPATH but lack PyYAML, then die on the first real CLI import.
 *
 * @param {string} pythonPath - Absolute path to a python.exe / python.
 * @param {object} [opts.env] - Additional environment for the probe.
 * @returns {boolean}
 */
function canImportRencoCli(pythonPath: string, opts: { env?: Record<string, string> } = {}) {
  if (!pythonPath) {
    return false
  }

  try {
    execProbeSync(pythonPath, ['-c', rencoRuntimeImportProbe()], {
      env: { ...process.env, ...(opts.env || {}) },
      stdio: 'ignore',
      timeout: PROBE_TIMEOUT_MS,
      windowsHide: true
    })

    return true
  } catch {
    return false
  }
}

/**
 * Return true iff `<rencoCommand> --version` exits 0.
 *
 * Used to gate the "existing `renco` on PATH" rung. Without this, a
 * stale renco.cmd shim left behind by an uninstalled pip install (or
 * a half-built venv whose `renco` entry-point points at a deleted
 * Python) survives findOnPath() and gets selected as the backend.
 *
 * We intentionally avoid invoking the command with the dashboard args
 * here -- `--version` is the cheapest "is this binary alive" smoke
 * test that every renco_cli entry-point has supported since 0.1.
 *
 * @param {string} rencoCommand - Resolved absolute path to a renco
 *   executable (or an interpreter+script wrapper).
 * @param {boolean} [opts.shell] - Whether to run through a shell. For
 *   .cmd/.bat shims on Windows execFileSync needs shell:true to find
 *   the cmd interpreter; mirrors the same flag isCommandScript() drives
 *   in resolveRencoBackend.
 * @returns {boolean}
 */
/**
 * An explicit desktop backend command is a deployment contract, not a PATH
 * discovery candidate. In particular, the Nix desktop wrapper points this at
 * its immutable, matching Renco package; it must never fall through to the
 * mutable install-script bootstrap path if a best-effort probe is slow.
 */
function shouldTrustRencoOverride(rencoOverride?: string) {
  return typeof rencoOverride === 'string' && rencoOverride.trim().length > 0
}

function verifyRencoCli(rencoCommand: string, opts?: { shell?: boolean }) {
  if (!rencoCommand) {
    return false
  }

  try {
    execProbeSync(rencoCommand, ['--version'], {
      stdio: 'ignore',
      timeout: PROBE_TIMEOUT_MS,
      shell: Boolean(opts?.shell),
      windowsHide: true
    })

    return true
  } catch {
    return false
  }
}

export {
  canImportRencoCli,
  DEFAULT_PROBE_TIMEOUT_MS,
  execProbeSync,
  rencoRuntimeImportProbe,
  PROBE_TIMEOUT_MS,
  resolveProbeTimeoutMs,
  shouldTrustRencoOverride,
  verifyRencoCli
}
