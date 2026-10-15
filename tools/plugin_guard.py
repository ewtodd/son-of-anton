#!/usr/bin/env python3
"""
Plugin Guard — Security scanner for externally-installed plugins.

Inspired by Claude Cowork's plugin security scanning (announced
2026-08-06: third-party plugins are automatically checked for malicious
content when someone uploads or edits them, returning pass / warn / fail).
Regex-based static analysis over a plugin tree, before it is installed.

Plugins run Python in-process with the agent, and they are *expected* to
read their own API keys from environment variables (the documented
``requires_env`` pattern), call provider HTTP APIs with those keys, and
spawn subprocesses. A naive pattern scan would flag every legitimate
provider plugin. So this scanner:

- Runs the full pattern set on documentation/config files
  (README, after-install.md, plugin.toml, ...), where prompt-injection
  and social-engineering content lives.
- Exempts the "reads own env secret" / "HTTP call with key" pattern
  family on *code* files, while keeping genuinely malicious signals:
  foreign credential-store access (~/.ssh, ~/.aws, ~/.son-of-anton/.env),
  reverse shells, destructive commands, persistence mechanisms,
  obfuscated execution, and known exfiltration services.
- Applies plugin-sized structural limits and skips VCS/venv noise.

Verdict → install policy (Cowork's pass/warn/fail, adapted):

- ``safe``      → install normally.
- ``caution``   → warn; requires explicit confirmation (interactive
                  prompt, ``--force``, or a caller-supplied decision
                  callback).
- ``dangerous`` → blocked. ``--force`` does NOT override.

Usage:
    from tools.plugin_guard import scan_plugin, should_allow_plugin_install

    result = scan_plugin(Path("/tmp/clone/my-plugin"), source="owner/repo")
    allowed, reason = should_allow_plugin_install(result)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

PLUGIN_SCANNER_VERSION = "plugin-guard-v1"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    pattern_id: str
    severity: str       # "critical" | "high" | "medium" | "low"
    category: str       # "exfiltration" | "injection" | "destructive" | "persistence" | "network" | "obfuscation"
    file: str
    line: int
    match: str
    description: str


@dataclass
class ScanResult:
    name: str
    source: str
    trust_level: str    # always "community" for an externally installed plugin
    verdict: str        # "safe" | "caution" | "dangerous"
    findings: List[Finding] = field(default_factory=list)
    scanned_at: str = ""
    summary: str = ""
    scan_provenance: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Threat patterns — (regex, pattern_id, severity, category, description)
# ---------------------------------------------------------------------------

THREAT_PATTERNS = [
    # ── Exfiltration: shell commands leaking secrets ──
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)',
     "env_exfil_curl", "critical", "exfiltration",
     "curl command interpolating secret environment variable"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)',
     "env_exfil_wget", "critical", "exfiltration",
     "wget command interpolating secret environment variable"),
    (r'fetch\s*\([^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|API)',
     "env_exfil_fetch", "critical", "exfiltration",
     "fetch() call interpolating secret environment variable"),
    (r'httpx?\.(get|post|put|patch)\s*\([^\n]*(KEY|TOKEN|SECRET|PASSWORD)',
     "env_exfil_httpx", "critical", "exfiltration",
     "HTTP library call with secret variable"),
    (r'requests\.(get|post|put|patch)\s*\([^\n]*(KEY|TOKEN|SECRET|PASSWORD)',
     "env_exfil_requests", "critical", "exfiltration",
     "requests library call with secret variable"),

    # ── Exfiltration: reading credential stores ──
    (r'base64[^\n]*env',
     "encoded_exfil", "high", "exfiltration",
     "base64 encoding combined with environment access"),
    (r'\$HOME/\.ssh|\~/\.ssh',
     "ssh_dir_access", "high", "exfiltration",
     "references user SSH directory"),
    (r'\$HOME/\.aws|\~/\.aws',
     "aws_dir_access", "high", "exfiltration",
     "references user AWS credentials directory"),
    (r'\$HOME/\.gnupg|\~/\.gnupg',
     "gpg_dir_access", "high", "exfiltration",
     "references user GPG keyring"),
    (r'\$HOME/\.kube|\~/\.kube',
     "kube_dir_access", "high", "exfiltration",
     "references Kubernetes config directory"),
    (r'\$HOME/\.docker|\~/\.docker',
     "docker_dir_access", "high", "exfiltration",
     "references Docker config (may contain registry creds)"),
    (r'\$HOME/\.son-of-anton/\.env|\~/\.son-of-anton/\.env',
     "son_of_anton_env_access", "critical", "exfiltration",
     "directly references Son of Anton secrets file"),
    # Match `cat <secrets-file>` (reading credentials) but NOT `cat > <file>`
    # or `cat >> <file>`, which are output redirections that WRITE a file
    # (e.g. a setup doc telling the user to write their own keys into their
    # own local `.env` via a heredoc). Writing your own config in is the
    # opposite of exfiltrating secrets out.
    (r'cat\s+(?!>)[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)',
     "read_secrets_file", "critical", "exfiltration",
     "reads known secrets file"),

    # ── Exfiltration: programmatic env access ──
    (r'printenv|env\s*\|',
     "dump_all_env", "high", "exfiltration",
     "dumps all environment variables"),
    # `os.environ` bare access (dict dump / iteration) is suspicious, but the
    # common `os.environ.get("SOME_CONFIG")` form is just a config read and is
    # the OPPOSITE of exfiltration (it reads a local var, sends nothing). The
    # lookahead exempts `os.environ.get("<name>")` only when <name> is NOT a
    # secret-shaped identifier — `os.environ.get("OPENAI_API_KEY")` still trips
    # via the dedicated secret pattern just below.
    (r'os\.environ\b(?!\s*\.get\s*\(\s*["\'](?![^"\']*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)))',
     "python_os_environ", "high", "exfiltration",
     "accesses os.environ (potential env dump)"),
    (r'os\.environ\s*\.get\s*\(\s*["\'][^"\']*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)',
     "python_environ_get_secret", "critical", "exfiltration",
     "reads secret via os.environ.get()"),
    (r'os\.getenv\s*\(\s*[^\)]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)',
     "python_getenv_secret", "critical", "exfiltration",
     "reads secret via os.getenv()"),
    (r'process\.env\[',
     "node_process_env", "high", "exfiltration",
     "accesses process.env (Node.js environment)"),
    (r'ENV\[.*(?:KEY|TOKEN|SECRET|PASSWORD)',
     "ruby_env_secret", "critical", "exfiltration",
     "reads secret via Ruby ENV[]"),

    # ── Exfiltration: DNS and staging ──
    (r'\b(dig|nslookup|host)\s+[^\n]*\$',
     "dns_exfil", "critical", "exfiltration",
     "DNS lookup with variable interpolation (possible DNS exfiltration)"),
    (r'>\s*/tmp/[^\s]*\s*&&\s*(curl|wget|nc|python)',
     "tmp_staging", "critical", "exfiltration",
     "writes to /tmp then exfiltrates"),

    # ── Exfiltration: markdown/link based ──
    (r'!\[.*\]\(https?://[^\)]*\$\{?',
     "md_image_exfil", "high", "exfiltration",
     "markdown image URL with variable interpolation (image-based exfil)"),
    (r'\[.*\]\(https?://[^\)]*\$\{?',
     "md_link_exfil", "high", "exfiltration",
     "markdown link with variable interpolation"),

    # ── Prompt injection ──
    (r'ignore\s+(?:\w+\s+)*(previous|all|above|prior)\s+instructions',
     "prompt_injection_ignore", "critical", "injection",
     "prompt injection: ignore previous instructions"),
    (r'you\s+are\s+(?:\w+\s+)*now\s+',
     "role_hijack", "high", "injection",
     "attempts to override the agent's role"),
    (r'do\s+not\s+(?:\w+\s+)*tell\s+(?:\w+\s+)*the\s+user',
     "deception_hide", "critical", "injection",
     "instructs agent to hide information from user"),
    (r'system\s+(?:\w+\s+)*prompt\s+(?:\w+\s+)*override',
     "sys_prompt_override", "critical", "injection",
     "attempts to override the system prompt"),
    (r'pretend\s+(?:\w+\s+)*(you\s+are|to\s+be)\s+',
     "role_pretend", "high", "injection",
     "attempts to make the agent assume a different identity"),
    (r'disregard\s+(?:\w+\s+)*(your|all|any)\s+(?:\w+\s+)*(instructions|rules|guidelines)',
     "disregard_rules", "critical", "injection",
     "instructs agent to disregard its rules"),
    (r'output\s+(?:\w+\s+)*(system|initial)\s+prompt',
     "leak_system_prompt", "high", "injection",
     "attempts to extract the system prompt"),
    (r'(when|if)\s+no\s*one\s+is\s+(watching|looking)',
     "conditional_deception", "high", "injection",
     "conditional instruction to behave differently when unobserved"),
    (r'act\s+as\s+(if|though)\s+(?:\w+\s+)*you\s+(?:\w+\s+)*(have\s+no|don\'t\s+have)\s+(?:\w+\s+)*(restrictions|limits|rules)',
     "bypass_restrictions", "critical", "injection",
     "instructs agent to act without restrictions"),
    (r'translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)',
     "translate_execute", "critical", "injection",
     "translate-then-execute evasion technique"),
    (r'<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->',
     "html_comment_injection", "high", "injection",
     "hidden instructions in HTML comments"),
    (r'<\s*div\s+style\s*=\s*["\'][\s\S]*?display\s*:\s*none',
     "hidden_div", "high", "injection",
     "hidden HTML div (invisible instructions)"),

    # ── Destructive operations ──
    (r'rm\s+-rf\s+/',
     "destructive_root_rm", "critical", "destructive",
     "recursive delete from root"),
    (r'rm\s+(-[^\s]*)?r.*\$HOME|\brmdir\s+.*\$HOME',
     "destructive_home_rm", "critical", "destructive",
     "recursive delete targeting home directory"),
    (r'chmod\s+777',
     "insecure_perms", "medium", "destructive",
     "sets world-writable permissions"),
    (r'>\s*/etc/',
     "system_overwrite", "critical", "destructive",
     "overwrites system configuration file"),
    (r'\bmkfs\b',
     "format_filesystem", "critical", "destructive",
     "formats a filesystem"),
    (r'\bdd\s+.*if=.*of=/dev/',
     "disk_overwrite", "critical", "destructive",
     "raw disk write operation"),
    (r'shutil\.rmtree\s*\(\s*[\"\'/]',
     "python_rmtree", "high", "destructive",
     "Python rmtree on absolute or root-relative path"),
    (r'truncate\s+-s\s*0\s+/',
     "truncate_system", "critical", "destructive",
     "truncates system file to zero bytes"),

    # ── Persistence ──
    (r'\bcrontab\b',
     "persistence_cron", "medium", "persistence",
     "modifies cron jobs"),
    (r'\.(bashrc|zshrc|profile|bash_profile|bash_login|zprofile|zlogin)\b',
     "shell_rc_mod", "medium", "persistence",
     "references shell startup file"),
    (r'authorized_keys',
     "ssh_backdoor", "critical", "persistence",
     "modifies SSH authorized keys"),
    (r'ssh-keygen',
     "ssh_keygen", "medium", "persistence",
     "generates SSH keys"),
    (r'systemd.*\.service|systemctl\s+(enable|start)',
     "systemd_service", "medium", "persistence",
     "references or enables systemd service"),
    (r'/etc/init\.d/',
     "init_script", "medium", "persistence",
     "references init.d startup script"),
    (r'launchctl\s+load|LaunchAgents|LaunchDaemons',
     "macos_launchd", "medium", "persistence",
     "macOS launch agent/daemon persistence"),
    (r'/etc/sudoers|visudo',
     "sudoers_mod", "critical", "persistence",
     "modifies sudoers (privilege escalation)"),
    (r'git\s+config\s+--global\s+',
     "git_config_global", "medium", "persistence",
     "modifies global git configuration"),

    # ── Network: reverse shells and tunnels ──
    (r'\bnc\s+-[lp]|ncat\s+-[lp]|\bsocat\b',
     "reverse_shell", "critical", "network",
     "potential reverse shell listener"),
    (r'\bngrok\b|\blocaltunnel\b|\bserveo\b|\bcloudflared\b',
     "tunnel_service", "high", "network",
     "uses tunneling service for external access"),
    (r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{2,5}',
     "hardcoded_ip_port", "medium", "network",
     "hardcoded IP address with port"),
    (r'0\.0\.0\.0:\d+|INADDR_ANY',
     "bind_all_interfaces", "high", "network",
     "binds to all network interfaces"),
    (r'/bin/(ba)?sh\s+-i\s+.*>/dev/tcp/',
     "bash_reverse_shell", "critical", "network",
     "bash interactive reverse shell via /dev/tcp"),
    (r'python[23]?\s+-c\s+["\']import\s+socket',
     "python_socket_oneliner", "critical", "network",
     "Python one-liner socket connection (likely reverse shell)"),
    (r'socket\.connect\s*\(\s*\(',
     "python_socket_connect", "high", "network",
     "Python socket connect to arbitrary host"),
    (r'webhook\.site|requestbin\.com|pipedream\.net|hookbin\.com',
     "exfil_service", "high", "network",
     "references known data exfiltration/webhook testing service"),
    (r'pastebin\.com|hastebin\.com|ghostbin\.',
     "paste_service", "medium", "network",
     "references paste service (possible data staging)"),

    # ── Obfuscation: encoding and eval ──
    (r'base64\s+(-d|--decode)\s*\|',
     "base64_decode_pipe", "high", "obfuscation",
     "base64 decodes and pipes to execution"),
    (r'\\x[0-9a-fA-F]{2}.*\\x[0-9a-fA-F]{2}.*\\x[0-9a-fA-F]{2}',
     "hex_encoded_string", "medium", "obfuscation",
     "hex-encoded string (possible obfuscation)"),
    (r'\beval\s*\(\s*["\']',
     "eval_string", "high", "obfuscation",
     "eval() with string argument"),
    (r'\bexec\s*\(\s*["\']',
     "exec_string", "high", "obfuscation",
     "exec() with string argument"),
    (r'echo\s+[^\n]*\|\s*(bash|sh|python|perl|ruby|node)',
     "echo_pipe_exec", "critical", "obfuscation",
     "echo piped to interpreter for execution"),
    (r'compile\s*\(\s*[^\)]+,\s*["\'].*["\']\s*,\s*["\']exec["\']\s*\)',
     "python_compile_exec", "high", "obfuscation",
     "Python compile() with exec mode"),
    (r'getattr\s*\(\s*__builtins__',
     "python_getattr_builtins", "high", "obfuscation",
     "dynamic access to Python builtins (evasion technique)"),
    (r'__import__\s*\(\s*["\']os["\']\s*\)',
     "python_import_os", "high", "obfuscation",
     "dynamic import of os module"),
    (r'codecs\.decode\s*\(\s*["\']',
     "python_codecs_decode", "medium", "obfuscation",
     "codecs.decode (possible ROT13 or encoding obfuscation)"),
    (r'String\.fromCharCode|charCodeAt',
     "js_char_code", "medium", "obfuscation",
     "JavaScript character code construction (possible obfuscation)"),
    (r'atob\s*\(|btoa\s*\(',
     "js_base64", "medium", "obfuscation",
     "JavaScript base64 encode/decode"),
    (r'\[::-1\]',
     "string_reversal", "low", "obfuscation",
     "string reversal (possible obfuscated payload)"),
    (r'chr\s*\(\s*\d+\s*\)\s*\+\s*chr\s*\(\s*\d+',
     "chr_building", "high", "obfuscation",
     "building string from chr() calls (obfuscation)"),
    (r'\\u[0-9a-fA-F]{4}.*\\u[0-9a-fA-F]{4}.*\\u[0-9a-fA-F]{4}',
     "unicode_escape_chain", "medium", "obfuscation",
     "chain of unicode escapes (possible obfuscation)"),

    # ── Process execution in scripts ──
    (r'subprocess\.(run|call|Popen|check_output)\s*\(',
     "python_subprocess", "medium", "execution",
     "Python subprocess execution"),
    (r'os\.system\s*\(',
     "python_os_system", "high", "execution",
     "os.system() — unguarded shell execution"),
    (r'os\.popen\s*\(',
     "python_os_popen", "high", "execution",
     "os.popen() — shell pipe execution"),
    (r'child_process\.(exec|spawn|fork)\s*\(',
     "node_child_process", "high", "execution",
     "Node.js child_process execution"),
    (r'Runtime\.getRuntime\(\)\.exec\(',
     "java_runtime_exec", "high", "execution",
     "Java Runtime.exec() — shell execution"),
    (r'`[^`]*\$\([^)]+\)[^`]*`',
     "backtick_subshell", "medium", "execution",
     "backtick string with command substitution"),

    # ── Path traversal ──
    (r'\.\./\.\./\.\.',
     "path_traversal_deep", "high", "traversal",
     "deep relative path traversal (3+ levels up)"),
    (r'\.\./\.\.',
     "path_traversal", "medium", "traversal",
     "relative path traversal (2+ levels up)"),
    (r'/etc/passwd|/etc/shadow',
     "system_passwd_access", "critical", "traversal",
     "references system password files"),
    (r'/proc/self|/proc/\d+/',
     "proc_access", "high", "traversal",
     "references /proc filesystem (process introspection)"),
    (r'/dev/shm/',
     "dev_shm", "medium", "traversal",
     "references shared memory (common staging area)"),

    # ── Crypto mining ──
    (r'xmrig|stratum\+tcp|monero|coinhive|cryptonight',
     "crypto_mining", "critical", "mining",
     "cryptocurrency mining reference"),
    (r'hashrate|nonce.*difficulty',
     "mining_indicators", "medium", "mining",
     "possible cryptocurrency mining indicators"),

    # ── Supply chain: curl/wget pipe to shell ──
    (r'curl\s+[^\n]*\|\s*(ba)?sh',
     "curl_pipe_shell", "critical", "supply_chain",
     "curl piped to shell (download-and-execute)"),
    (r'wget\s+[^\n]*-O\s*-\s*\|\s*(ba)?sh',
     "wget_pipe_shell", "critical", "supply_chain",
     "wget piped to shell (download-and-execute)"),
    (r'curl\s+[^\n]*\|\s*python',
     "curl_pipe_python", "critical", "supply_chain",
     "curl piped to Python interpreter"),

    # ── Supply chain: unpinned/deferred dependencies ──
    (r'#\s*///\s*script.*dependencies',
     "pep723_inline_deps", "medium", "supply_chain",
     "PEP 723 inline script metadata with dependencies (verify pinning)"),
    (r'pip\s+install\s+(?!-r\s)(?!.*==)',
     "unpinned_pip_install", "medium", "supply_chain",
     "pip install without version pinning"),
    (r'npm\s+install\s+(?!.*@\d)',
     "unpinned_npm_install", "medium", "supply_chain",
     "npm install without version pinning"),
    (r'uv\s+run\s+',
     "uv_run", "medium", "supply_chain",
     "uv run (may auto-install unpinned dependencies)"),

    # ── Supply chain: remote resource fetching ──
    (r'(curl|wget|httpx?\.get|requests\.get|fetch)\s*[\(]?\s*["\']https?://',
     "remote_fetch", "medium", "supply_chain",
     "fetches remote resource at runtime"),
    (r'git\s+clone\s+',
     "git_clone", "medium", "supply_chain",
     "clones a git repository at runtime"),
    (r'docker\s+pull\s+',
     "docker_pull", "medium", "supply_chain",
     "pulls a Docker image at runtime"),

    # ── Privilege escalation ──
    (r'\bsudo\b',
     "sudo_usage", "high", "privilege_escalation",
     "uses sudo (privilege escalation)"),
    (r'setuid|setgid|cap_setuid',
     "setuid_setgid", "critical", "privilege_escalation",
     "setuid/setgid (privilege escalation mechanism)"),
    (r'NOPASSWD',
     "nopasswd_sudo", "critical", "privilege_escalation",
     "NOPASSWD sudoers entry (passwordless privilege escalation)"),
    (r'chmod\s+[u+]?s',
     "suid_bit", "critical", "privilege_escalation",
     "sets SUID/SGID bit on a file"),

    # ── Agent config persistence ──
    (r'AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules',
     "agent_config_mod", "critical", "persistence",
     "references agent config files (could persist malicious instructions across sessions)"),
    (r'\.son-of-anton/config\.yaml|\.son-of-anton/SOUL\.md',
     "son_of_anton_config_mod", "critical", "persistence",
     "references Son of Anton configuration files directly"),
    (r'\.claude/settings|\.codex/config',
     "other_agent_config", "high", "persistence",
     "references other agent configuration files"),

    # ── Hardcoded secrets (credentials embedded in the scanned content) ──
    (r'(?:api[_-]?key|token|secret|password)\s*[=:]\s*["\'][A-Za-z0-9+/=_-]{20,}',
     "hardcoded_secret", "critical", "credential_exposure",
     "possible hardcoded API key, token, or secret"),
    (r'-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----',
     "embedded_private_key", "critical", "credential_exposure",
     "embedded private key"),
    (r'ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{80,}',
     "github_token_leaked", "critical", "credential_exposure",
     "GitHub personal access token in scanned content"),
    (r'sk-[A-Za-z0-9]{20,}',
     "openai_key_leaked", "critical", "credential_exposure",
     "possible OpenAI API key in scanned content"),
    (r'sk-ant-[A-Za-z0-9_-]{90,}',
     "anthropic_key_leaked", "critical", "credential_exposure",
     "possible Anthropic API key in scanned content"),
    (r'AKIA[0-9A-Z]{16}',
     "aws_access_key_leaked", "critical", "credential_exposure",
     "AWS access key ID in scanned content"),
    (r'glpat-[A-Za-z0-9_\-]{20,}',
     "gitlab_token_leaked", "critical", "credential_exposure",
     "GitLab personal access token in scanned content"),

    # ── Additional prompt injection: jailbreak patterns ──
    (r'\bDAN\s+mode\b|Do\s+Anything\s+Now',
     "jailbreak_dan", "critical", "injection",
     "DAN (Do Anything Now) jailbreak attempt"),
    (r'\bdeveloper\s+mode\b.*\benabled?\b',
     "jailbreak_dev_mode", "critical", "injection",
     "developer mode jailbreak attempt"),
    (r'hypothetical\s+scenario.*(?:ignore|bypass|override)',
     "hypothetical_bypass", "high", "injection",
     "hypothetical scenario used to bypass restrictions"),
    (r'for\s+educational\s+purposes?\s+only',
     "educational_pretext", "medium", "injection",
     "educational pretext often used to justify harmful content"),
    (r'(respond|answer|reply)\s+without\s+(?:\w+\s+)*(restrictions|limitations|filters|safety)',
     "remove_filters", "critical", "injection",
     "instructs agent to respond without safety filters"),
    (r'you\s+have\s+been\s+(?:\w+\s+)*(updated|upgraded|patched)\s+to',
     "fake_update", "high", "injection",
     "fake update/patch announcement (social engineering)"),
    (r'new\s+(?:\w+\s+)*policy|updated\s+(?:\w+\s+)*guidelines|revised\s+(?:\w+\s+)*instructions',
     "fake_policy", "medium", "injection",
     "claims new policy/guidelines (may be social engineering)"),

    # ── Context window exfiltration ──
    (r'(include|output|print|send|share)\s+(?:\w+\s+)*(conversation|chat\s+history|previous\s+messages|context)',
     "context_exfil", "high", "exfiltration",
     "instructs agent to output/share conversation history"),
    (r'(send|post|upload|transmit)\s+.*\s+(to|at)\s+https?://',
     "send_to_url", "high", "exfiltration",
     "instructs agent to send data to a URL"),
]

_COMPILED_THREAT_PATTERNS = [
    (re.compile(pattern, re.IGNORECASE), pid, severity, category, description)
    for pattern, pid, severity, category, description in THREAT_PATTERNS
]

# File extensions to scan (text files only — skip binary)
SCANNABLE_EXTENSIONS = {
    '.md', '.txt', '.py', '.sh', '.bash', '.js', '.ts', '.rb',
    '.yaml', '.yml', '.json', '.toml', '.cfg', '.ini', '.conf',
    '.html', '.css', '.xml', '.tex', '.r', '.jl', '.pl', '.php',
}

# Known binary extensions that cannot be scanned
SUSPICIOUS_BINARY_EXTENSIONS = {
    '.exe', '.dll', '.so', '.dylib', '.bin', '.dat', '.com',
    '.msi', '.dmg', '.app', '.deb', '.rpm',
}

# Zero-width and invisible unicode characters used for injection
INVISIBLE_CHARS = {
    '\u200b',  # zero-width space
    '\u200c',  # zero-width non-joiner
    '\u200d',  # zero-width joiner
    '\u2060',  # word joiner
    '\u2062',  # invisible times
    '\u2063',  # invisible separator
    '\u2064',  # invisible plus
    '\ufeff',  # zero-width no-break space (BOM)
    '\u202a',  # left-to-right embedding
    '\u202b',  # right-to-left embedding
    '\u202c',  # pop directional formatting
    '\u202d',  # left-to-right override
    '\u202e',  # right-to-left override
    '\u2066',  # left-to-right isolate
    '\u2067',  # right-to-left isolate
    '\u2068',  # first strong isolate
    '\u2069',  # pop directional isolate
}


# ---------------------------------------------------------------------------
# Scanning primitives
# ---------------------------------------------------------------------------

def scan_file(file_path: Path, rel_path: str = "") -> List[Finding]:
    """
    Scan a single file for threat patterns and invisible unicode characters.

    Args:
        file_path: Absolute path to the file
        rel_path: Relative path for display (defaults to file_path.name)

    Returns:
        List of findings (deduplicated per pattern per line)
    """
    if not rel_path:
        rel_path = file_path.name

    if file_path.suffix.lower() not in SCANNABLE_EXTENSIONS:
        return []

    try:
        content = file_path.read_text(encoding='utf-8')
    except (UnicodeDecodeError, OSError):
        return []

    findings = []
    lines = content.split('\n')
    seen = set()  # (pattern_id, line_number) for deduplication

    # Regex pattern matching
    for pattern, pid, severity, category, description in _COMPILED_THREAT_PATTERNS:
        for i, line in enumerate(lines, start=1):
            if (pid, i) in seen:
                continue
            if pattern.search(line):
                seen.add((pid, i))
                matched_text = line.strip()
                if len(matched_text) > 120:
                    matched_text = matched_text[:117] + "..."
                findings.append(Finding(
                    pattern_id=pid,
                    severity=severity,
                    category=category,
                    file=rel_path,
                    line=i,
                    match=matched_text,
                    description=description,
                ))

    # Invisible unicode character detection
    for i, line in enumerate(lines, start=1):
        for char in INVISIBLE_CHARS:
            if char in line:
                char_name = _unicode_char_name(char)
                findings.append(Finding(
                    pattern_id="invisible_unicode",
                    severity="high",
                    category="injection",
                    file=rel_path,
                    line=i,
                    match=f"U+{ord(char):04X} ({char_name})",
                    description=f"invisible unicode character {char_name} (possible text hiding/injection)",
                ))
                break  # one finding per line for invisible chars

    return findings


def _unicode_char_name(char: str) -> str:
    """Get a readable name for an invisible unicode character."""
    names = {
        '\u200b': "zero-width space",
        '\u200c': "zero-width non-joiner",
        '\u200d': "zero-width joiner",
        '\u2060': "word joiner",
        '\u2062': "invisible times",
        '\u2063': "invisible separator",
        '\u2064': "invisible plus",
        '\ufeff': "BOM/zero-width no-break space",
        '\u202a': "LTR embedding",
        '\u202b': "RTL embedding",
        '\u202c': "pop directional",
        '\u202d': "LTR override",
        '\u202e': "RTL override",
        '\u2066': "LTR isolate",
        '\u2067': "RTL isolate",
        '\u2068': "first strong isolate",
        '\u2069': "pop directional isolate",
    }
    return names.get(char, f"U+{ord(char):04X}")


def _determine_verdict(findings: List[Finding]) -> str:
    """Determine the overall verdict from a list of findings."""
    if not findings:
        return "safe"

    has_critical = any(f.severity == "critical" for f in findings)
    has_high = any(f.severity == "high" for f in findings)

    if has_critical:
        return "dangerous"
    if has_high:
        return "caution"
    # medium/low findings alone are informational, not blocking
    return "safe"



# Directories that are never scanned (VCS internals, caches, vendored envs).
EXCLUDED_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
}

# Code file extensions where "reads an env secret" / "HTTP call with a key
# variable" is the NORMAL, documented plugin pattern (provider plugins read
# their own API keys via requires_env and call their backend with them).
CODE_FILE_EXTENSIONS = {
    ".py", ".js", ".ts", ".sh", ".bash", ".rb", ".pl", ".php",
}

# Pattern ids from THREAT_PATTERNS that are exempt on code
# files. Each of these describes behavior every legitimate provider plugin
# exhibits. They still apply in full to documentation and config files,
# where such content is a strong injection/social-engineering signal.
CODE_EXEMPT_PATTERN_IDS = {
    "python_environ_get_secret",
    "python_getenv_secret",
    "python_os_environ",
    "node_process_env",
    "ruby_env_secret",
    "env_exfil_httpx",
    "env_exfil_requests",
    "env_exfil_fetch",
    "env_exfil_curl",
    "env_exfil_wget",
    # Agent-facing instruction patterns are meaningless inside code
    # (docstrings/comments about prompts trip them constantly).
    "context_exfil",
    "send_to_url",
    "fake_policy",
    # Plugins legitimately write their own settings into config.toml during
    # post_setup, and encode credentials (e.g. HTTP Basic auth) with base64.
    "agent_config_mod",
    "encoded_exfil",
}

# Findings whose severity is remapped for plugins. Plugin repos occasionally
# vendor a compiled artifact legitimately, so a binary is a warn-tier signal
# instead of an instant block.
#
# ``son_of_anton_env_access`` (a reference to ``~/.son-of-anton/.env``) is the DOCUMENTED
# way plugins tell users where to put their API keys — nearly every legit
# plugin README mentions it. A mere reference is informational for plugins;
# actually READING the file still trips ``read_secrets_file`` (critical).
# ``curl | sh`` install instructions are common in plugin READMEs; keep them
# at warn tier (caution) rather than an unoverridable block.
SEVERITY_REMAP = {
    "binary_file": "high",
    "son_of_anton_env_access": "medium",
    "curl_pipe_shell": "high",
}

# Structural limits — plugins are real codebases.
MAX_PLUGIN_FILE_COUNT = 400
MAX_PLUGIN_TOTAL_SIZE_KB = 10 * 1024   # 10MB of scannable tree
MAX_PLUGIN_SINGLE_FILE_KB = 1024       # 1MB single file


def _is_excluded(rel_parts: Tuple[str, ...]) -> bool:
    return any(part in EXCLUDED_DIRS for part in rel_parts)


def _filter_findings(findings: List[Finding], rel_path: str) -> List[Finding]:
    """Apply plugin-specific exemptions and severity remaps to raw findings."""
    ext = Path(rel_path).suffix.lower()
    is_code = ext in CODE_FILE_EXTENSIONS
    out: List[Finding] = []
    for f in findings:
        if is_code and f.pattern_id in CODE_EXEMPT_PATTERN_IDS:
            continue
        remapped = SEVERITY_REMAP.get(f.pattern_id)
        if remapped:
            f.severity = remapped
        out.append(f)
    return out


def _check_plugin_structure(plugin_dir: Path) -> List[Finding]:
    """Structural checks sized for plugin repositories."""
    findings: List[Finding] = []
    file_count = 0
    total_size = 0

    for f in plugin_dir.rglob("*"):
        try:
            rel_parts = f.relative_to(plugin_dir).parts
        except ValueError:
            continue
        if _is_excluded(rel_parts):
            continue
        rel = "/".join(rel_parts)

        if f.is_symlink():
            file_count += 1
            try:
                resolved = f.resolve()
                if not resolved.is_relative_to(plugin_dir.resolve()):
                    findings.append(Finding(
                        pattern_id="symlink_escape",
                        severity="critical",
                        category="traversal",
                        file=rel,
                        line=0,
                        match=f"symlink -> {resolved}",
                        description="symlink points outside the plugin directory",
                    ))
            except OSError:
                findings.append(Finding(
                    pattern_id="broken_symlink",
                    severity="medium",
                    category="traversal",
                    file=rel,
                    line=0,
                    match="broken symlink",
                    description="broken or circular symlink",
                ))
            continue

        if not f.is_file():
            continue
        file_count += 1

        try:
            size = f.stat().st_size
        except OSError:
            continue
        total_size += size

        if size > MAX_PLUGIN_SINGLE_FILE_KB * 1024:
            findings.append(Finding(
                pattern_id="oversized_file",
                severity="medium",
                category="structural",
                file=rel,
                line=0,
                match=f"{size // 1024}KB",
                description=(
                    f"file is {size // 1024}KB "
                    f"(limit: {MAX_PLUGIN_SINGLE_FILE_KB}KB)"
                ),
            ))

        ext = f.suffix.lower()
        if ext in SUSPICIOUS_BINARY_EXTENSIONS:
            findings.append(Finding(
                pattern_id="binary_file",
                severity=SEVERITY_REMAP.get("binary_file", "high"),
                category="structural",
                file=rel,
                line=0,
                match=f"binary: {ext}",
                description=(
                    f"binary/executable file ({ext}) bundled in plugin "
                    f"(cannot be scanned)"
                ),
            ))

    if file_count > MAX_PLUGIN_FILE_COUNT:
        findings.append(Finding(
            pattern_id="too_many_files",
            severity="medium",
            category="structural",
            file="(directory)",
            line=0,
            match=f"{file_count} files",
            description=(
                f"plugin has {file_count} files "
                f"(limit: {MAX_PLUGIN_FILE_COUNT})"
            ),
        ))
    if total_size > MAX_PLUGIN_TOTAL_SIZE_KB * 1024:
        findings.append(Finding(
            pattern_id="oversized_bundle",
            severity="medium",
            category="structural",
            file="(directory)",
            line=0,
            match=f"{total_size // 1024}KB",
            description=(
                f"plugin is {total_size // 1024}KB total "
                f"(limit: {MAX_PLUGIN_TOTAL_SIZE_KB}KB)"
            ),
        ))

    return findings


def scan_plugin(plugin_dir: Path, source: str = "") -> ScanResult:
    """Scan a plugin directory for security threats.

    Args:
        plugin_dir: Path to the plugin directory (typically the temp clone,
            before it is moved into ``~/.son-of-anton/plugins/``).
        source: Identifier for display (git URL or owner/repo shorthand).

    Returns:
        ScanResult with verdict ``safe`` | ``caution`` | ``dangerous``.
        Every externally installed plugin is ``community`` trust.
    """
    all_findings: List[Finding] = []

    if plugin_dir.is_dir():
        all_findings.extend(_check_plugin_structure(plugin_dir))
        for f in sorted(plugin_dir.rglob("*")):
            if not f.is_file() or f.is_symlink():
                continue
            try:
                rel_parts = f.relative_to(plugin_dir).parts
            except ValueError:
                continue
            if _is_excluded(rel_parts):
                continue
            rel = "/".join(rel_parts)
            raw = scan_file(f, rel_path=rel)
            all_findings.extend(_filter_findings(raw, rel))

    verdict = _determine_verdict(all_findings)
    result = ScanResult(
        name=plugin_dir.name,
        source=source or plugin_dir.name,
        trust_level="community",
        verdict=verdict,
        findings=all_findings,
        scanned_at=datetime.now(timezone.utc).isoformat(),
    )
    if all_findings:
        categories = {f.category for f in all_findings}
        result.summary = (
            f"{plugin_dir.name}: {verdict} — {len(all_findings)} finding(s) "
            f"in {', '.join(sorted(categories))}"
        )
    else:
        result.summary = f"{plugin_dir.name}: clean scan, no threats detected"
    result.scan_provenance = {
        "scanner_version": PLUGIN_SCANNER_VERSION,
        "verdict": verdict,
        "source": result.source,
    }
    return result


def should_allow_plugin_install(
    result: ScanResult,
    force: bool = False,
) -> Tuple[Optional[bool], str]:
    """Map a plugin scan verdict to an install decision.

    Returns ``(allowed, reason)``:
      - ``(True, ...)``  install proceeds.
      - ``(None, ...)``  needs explicit confirmation (caution verdict).
      - ``(False, ...)`` blocked; ``force`` never overrides ``dangerous``.
    """
    if result.verdict == "safe":
        return True, "Allowed (clean scan)"
    if result.verdict == "caution":
        if force:
            return True, (
                f"Force-installed despite caution verdict "
                f"({len(result.findings)} findings)"
            )
        return None, (
            f"Requires confirmation (caution verdict, "
            f"{len(result.findings)} findings)"
        )
    return False, (
        f"Blocked (dangerous verdict, {len(result.findings)} findings). "
        f"--force does not override a dangerous verdict."
    )


def format_scan_report(result: ScanResult) -> str:
    """Format a scan result as a compact multi-line report for CLI or chat."""
    lines = [
        f"Scan: {result.name} ({result.source}/{result.trust_level})  "
        f"Verdict: {result.verdict.upper()}"
    ]

    if result.findings:
        # Critical first, then high, medium, low.
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        for f in sorted(result.findings, key=lambda f: severity_order.get(f.severity, 4)):
            sev = f.severity.upper().ljust(8)
            cat = f.category.ljust(14)
            loc = f"{f.file}:{f.line}".ljust(30)
            lines.append(f"  {sev} {cat} {loc} \"{f.match[:60]}\"")
        lines.append("")

    allowed, reason = should_allow_plugin_install(result)
    if allowed is True:
        status = "ALLOWED"
    elif allowed is None:
        status = "NEEDS CONFIRMATION"
    else:
        status = "BLOCKED"
    lines.append(f"Decision: {status} — {reason}")
    return "\n".join(lines)


__all__ = [
    "scan_plugin",
    "should_allow_plugin_install",
    "format_scan_report",
    "PLUGIN_SCANNER_VERSION",
]
