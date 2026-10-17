"""URL safety checks — blocks requests to private/internal network addresses.

Prevents SSRF (Server-Side Request Forgery) where a malicious prompt
could trick the agent into fetching internal resources like cloud
metadata endpoints (169.254.169.254), localhost services, or private
network hosts.

Limitations:
  - DNS rebinding (TOCTOU): an attacker-controlled DNS server with TTL=0
    can return a public IP for the check, then a private IP for the actual
    connection. Direct httpx request paths should use
    ``create_ssrf_safe_async_client()`` so the same policy is applied
    immediately before TCP connect and the client connects to the validated
    IP while preserving Host/SNI semantics.
  - Redirect-based bypass is mitigated by httpx event hooks that re-validate
    each redirect target in vision_tools and the gateway platform adapter.
"""

import ipaddress
import logging
import os
import socket
import asyncio
import re
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlparse, urlsplit, urlunsplit

logger = logging.getLogger(__name__)


# ── Proxy detection ──────────────────────────────────────────
# Proxy environment variables that indicate the runtime should
# delegate DNS to a proxy rather than attempting direct resolution.
_PROXY_ENV_VARS = (
    "HTTPS_PROXY", "https_proxy",
    "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy",
)


def _proxy_is_configured() -> bool:
    """Return True when at least one HTTP proxy env var is set."""
    return any(os.environ.get(v) for v in _PROXY_ENV_VARS)


def normalize_url_for_request(url: str) -> str:
    """Return an ASCII-safe HTTP URL for Son of Anton-owned URL tools.

    Browsers and HTTP clients expect URIs, but users and models often provide
    IRIs such as ``https://wttr.in/Köln``.  Preserve URL syntax and existing
    percent escapes while encoding non-ASCII host/path/query/fragment text.
    This is intentionally for URL tool inputs only; arbitrary shell commands
    must not be rewritten.
    """
    if not isinstance(url, str):
        return url

    raw = url.strip()
    if not raw:
        return raw

    # Models sometimes emit otherwise valid URLs with whitespace between the
    # scheme separator and authority (``https:// docs.example``). That position
    # is never meaningful in HTTP(S) URLs, and repairing it before parsing keeps
    # web tools from failing on a formatting artifact while leaving path/query
    # whitespace to the normal percent-encoding path below.
    raw = re.sub(r"^([A-Za-z][A-Za-z0-9+.-]*://)\s+", r"\1", raw)

    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw

    if parsed.scheme.lower() not in {"http", "https"}:
        return raw

    netloc = parsed.netloc
    hostname = parsed.hostname
    if hostname:
        try:
            ascii_host = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            ascii_host = hostname
        if ascii_host != hostname:
            netloc = netloc.replace(hostname, ascii_host, 1)

    path = quote(parsed.path, safe="/%:@!$&'()*+,;=")
    query = quote(parsed.query, safe="/%:@!$&'()*+,;=?")
    fragment = quote(parsed.fragment, safe="/%:@!$&'()*+,;=?")

    return urlunsplit((parsed.scheme, netloc, path, query, fragment))


# Query parameter names that are unambiguously credential-bearing. Kept
# deliberately narrow: bare English words that double as normal page facets
# (``code`` on promo/challenge pages, ``key``/``auth``/``session``/``sig`` as
# search or routing params) are intentionally EXCLUDED to avoid blocking
# ordinary browsing. Prefix-based token redaction (``is_safe_url``) still
# catches recognizable vendor key shapes; this set is the belt-and-suspenders
# for opaque secrets that carry an explicit credential-named parameter.
_SENSITIVE_QUERY_PARAM_NAMES = frozenset({
    "access_token",
    "api_key",
    "apikey",
    "auth_token",
    "authorization",
    "awsaccesskeyid",
    "client_secret",
    "credential",
    "credentials",
    "jwt",
    "password",
    "passwd",
    "secret",
    "session_id",
    "signature",
    "token",
    "x_amz_security_token",
    "x_amz_signature",
    "x-amz-security-token",
    "x-amz-signature",
})


def sensitive_query_param_name(url: str) -> Optional[str]:
    """Return the first sensitive query parameter name in ``url``, if any.

    Used before handing URLs to third-party fetch/browser backends. Prefix-based
    token redaction catches known credential shapes; this catches opaque magic
    links, OAuth codes, signed URL signatures, and custom ``?token=...`` values
    that do not have a recognizable vendor prefix.
    """
    if not isinstance(url, str) or "?" not in url:
        return None
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.query:
        return None
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if value and unquote(key).lower() in _SENSITIVE_QUERY_PARAM_NAMES:
            return key
    return None


# Cloud metadata hostnames an attacker could use to steal instance
# credentials; always blocked.
_BLOCKED_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
})

# Cloud metadata / credential endpoints — the #1 SSRF target — and the
# link-local range where they all live.
#
# IPv4-mapped IPv6 variants are included because DNS resolvers may
# return ``::ffff:x.x.x.x`` for IPv4-only hosts, and Python's
# ipaddress module treats these as distinct from the plain IPv4
# address (they won't match ``ip in frozenset`` or ``ip in network``).
_ALWAYS_BLOCKED_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure/DO/Oracle metadata
    ipaddress.ip_address("169.254.170.2"),     # AWS ECS task metadata (task IAM creds)
    ipaddress.ip_address("169.254.169.253"),   # Azure IMDS wire server
    ipaddress.ip_address("fd00:ec2::254"),     # AWS metadata (IPv6)
    ipaddress.ip_address("100.100.100.200"),   # Alibaba Cloud metadata
    # IPv4-mapped IPv6 variants — same endpoints reachable via ::ffff:x.x.x.x
    ipaddress.ip_address("::ffff:169.254.169.254"),
    ipaddress.ip_address("::ffff:169.254.170.2"),
    ipaddress.ip_address("::ffff:169.254.169.253"),
    ipaddress.ip_address("::ffff:100.100.100.200"),
})
_ALWAYS_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),    # Entire link-local range (no legit agent target)
    ipaddress.ip_network("::ffff:169.254.0.0/112"), # IPv4-mapped link-local range
)

_MAX_SSRF_CONNECT_IPS = 8

# 100.64.0.0/10 (CGNAT / Shared Address Space, RFC 6598) is NOT covered by
# ipaddress.is_private — it returns False for both is_private and is_global.
# Must be blocked explicitly. Used by carrier-grade NAT, Tailscale/WireGuard
# VPNs, and some cloud internal networks.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if the IP should be blocked for SSRF protection."""
    # IPv4-mapped IPv6 addresses (``::ffff:x.x.x.x``) should be checked
    # by their embedded IPv4 address, not as IPv6
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        embedded_ip = ip.ipv4_mapped
        return (embedded_ip.is_private or embedded_ip.is_loopback or
                embedded_ip.is_link_local or embedded_ip.is_reserved or
                embedded_ip.is_multicast or embedded_ip.is_unspecified or
                embedded_ip in _CGNAT_NETWORK)

    # Standard IPv4/IPv6 address checking
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return True
    # CGNAT range not covered by is_private
    if ip in _CGNAT_NETWORK:
        return True
    return False


def is_safe_url(url: str) -> bool:
    """Return True if the URL target is not a private/internal address.

    Resolves the hostname to an IP and checks against private ranges.
    Fails closed: DNS errors and unexpected exceptions block the request.
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        scheme = (parsed.scheme or "").strip().lower()
        if scheme not in {"http", "https"}:
            logger.warning("Blocked request — unsupported URL scheme: %s", scheme or "<empty>")
            return False
        if not hostname:
            return False

        if hostname in _BLOCKED_HOSTNAMES:
            logger.warning("Blocked request to internal hostname: %s", hostname)
            return False

        # Try to resolve and check IP
        try:
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            # DNS resolution failed.  In sandbox / proxy environments
            # the host may block direct DNS — only HTTP(S) through the proxy is
            # permitted.  When a proxy is configured, delegate DNS to
            # the proxy rather than blocking the request outright.
            # The hostname was already checked against
            # _BLOCKED_HOSTNAMES above so metadata endpoints remain
            # blocked regardless.  Literal IPs never qualify — they
            # need no DNS, so a getaddrinfo failure on one is not a
            # proxy-environment symptom; keep them on the fail-closed
            # path (and the blocked-IP floor) instead of delegating.
            _is_literal_ip = True
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                _is_literal_ip = False
            if not _is_literal_ip and _proxy_is_configured():
                logger.debug(
                    "DNS resolution failed for %s — proxy configured, "
                    "allowing through for proxy-side resolution",
                    hostname,
                )
                return True
            logger.warning("Blocked request — DNS resolution failed for: %s", hostname)
            return False

        for family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            if '%' in ip_str:
                ip_str = ip_str.split('%')[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                # Still unparseable after scope ID strip — fail closed
                logger.warning("Blocked request — unparseable IP address %r for hostname %s", sockaddr[0], hostname)
                return False

            if ip in _ALWAYS_BLOCKED_IPS or any(ip in net for net in _ALWAYS_BLOCKED_NETWORKS):
                logger.warning(
                    "Blocked request to cloud metadata address: %s -> %s",
                    hostname, ip_str,
                )
                return False

            if _is_blocked_ip(ip):
                logger.warning(
                    "Blocked request to private/internal address: %s -> %s",
                    hostname, ip_str,
                )
                return False

        return True

    except Exception as exc:
        # Fail closed on unexpected errors — don't let parsing edge cases
        # become SSRF bypass vectors
        logger.warning("Blocked request — URL safety check error for %s: %s", url, exc)
        return False


async def async_is_safe_url(url: str) -> bool:
    """Same rules as :func:`is_safe_url`, but run the DNS work off the event loop.

    ``socket.getaddrinfo`` can block; call this from async code paths (gateway,
    ``web_extract_tool``, vision download hooks) instead of ``is_safe_url``.
    """
    return await asyncio.to_thread(is_safe_url, url)


class SSRFConnectionBlocked(ValueError):
    """Raised when connect-time DNS resolution violates the URL safety policy."""


def _resolved_http_connect_ips(host: str, port: int) -> list[str]:
    """Resolve and validate *host* for one HTTP connect attempt.

    Unlike :func:`is_safe_url`, this is called from the HTTP transport at the
    time the TCP socket is about to be opened.  It returns concrete IP strings
    that the transport can dial directly, closing the DNS-rebinding gap between
    pre-flight validation and connection setup for direct httpx clients.
    """
    hostname = (host or "").strip().lower().rstrip(".")
    if not hostname:
        raise SSRFConnectionBlocked("Blocked request with empty hostname")

    if hostname in _BLOCKED_HOSTNAMES:
        raise SSRFConnectionBlocked(f"Blocked request to internal hostname: {hostname}")

    try:
        addr_info = socket.getaddrinfo(
            hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise SSRFConnectionBlocked(
            f"Blocked request - DNS resolution failed for: {hostname}"
        ) from exc

    safe_ips: list[str] = []
    seen: set[str] = set()
    for _family, _, _, _, sockaddr in addr_info:
        ip_str = sockaddr[0]
        if "%" in ip_str:
            ip_str = ip_str.split("%")[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            raise SSRFConnectionBlocked(
                f"Blocked request - unparseable IP address {sockaddr[0]!r} for hostname {hostname}"
            ) from exc

        if ip in _ALWAYS_BLOCKED_IPS or any(ip in net for net in _ALWAYS_BLOCKED_NETWORKS):
            raise SSRFConnectionBlocked(
                f"Blocked request to cloud metadata address during connect: {hostname} -> {ip_str}"
            )

        if _is_blocked_ip(ip):
            raise SSRFConnectionBlocked(
                f"Blocked request to private/internal address during connect: {hostname} -> {ip_str}"
            )

        if ip_str not in seen and len(safe_ips) < _MAX_SSRF_CONNECT_IPS:
            safe_ips.append(ip_str)
            seen.add(ip_str)

    if not safe_ips:
        raise SSRFConnectionBlocked(f"Blocked request - DNS returned no results for: {hostname}")
    return safe_ips


class _SSRFGuardedAsyncNetworkBackend:
    def __init__(self):
        from httpcore._backends.auto import AutoBackend

        self._backend = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        import httpcore

        ips = await asyncio.to_thread(_resolved_http_connect_ips, host, port)

        last_exc: Exception | None = None
        for ip in ips:
            try:
                return await self._backend.connect_tcp(
                    ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        raise SSRFConnectionBlocked(f"Blocked request - DNS returned no usable IPs for: {host}")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> Any:
        raise SSRFConnectionBlocked("Blocked Unix socket connection in SSRF-safe transport")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def create_ssrf_safe_async_client(**kwargs: Any) -> Any:
    """Create an ``httpx.AsyncClient`` with connect-time SSRF validation.

    Direct HTTP(S) connections are resolved, validated, and dialed by IP at
    TCP-connect time while the original request hostname is preserved for Host,
    SNI, and certificate verification.  If httpx routes through a proxy, final
    target resolution is delegated to that configured proxy; treat the proxy as
    a trusted egress boundary.
    """
    import httpx

    client = httpx.AsyncClient(**kwargs)
    pool = getattr(client.__dict__.get("_transport"), "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise SSRFConnectionBlocked("Unsupported async httpx transport cannot be made SSRF-safe")
    pool._network_backend = _SSRFGuardedAsyncNetworkBackend()
    return client


def redirect_target_from_response(response: Any) -> Optional[str]:
    """Return the redirect target visible from inside an httpx response hook.

    In ``httpx.AsyncClient`` response event hooks, ``response.next_request`` is
    frequently ``None`` even for a genuine redirect (it is populated later by
    the redirect-following machinery). Relying on ``next_request`` alone means
    an SSRF redirect guard silently never fires: a public URL that 302s to
    ``http://169.254.169.254/`` gets followed anyway. The ``Location`` header,
    however, is already present on the response, so resolve the target from it
    first (handling relative Locations via ``urljoin``) and only fall back to
    ``next_request`` when no ``Location`` header is set.
    """
    if not getattr(response, "is_redirect", False):
        return None

    headers = getattr(response, "headers", {}) or {}
    location = headers.get("location")
    if location:
        return urljoin(str(getattr(response, "url", "")), str(location))

    next_request = getattr(response, "next_request", None)
    if next_request:
        return str(next_request.url)

    return None
