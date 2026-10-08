"""Gateway-side relay authentication primitives. EXPERIMENTAL.

The connector⇄gateway channel is authenticated because a gateway may be
customer-managed and internet-exposed (see the connector repo
``docs/connector-gateway-auth-design.md``). This module is the **gateway half**
of two HMAC schemes whose wire bytes must match the connector's TypeScript
exactly:

1. **WS upgrade auth** (gateway → connector): the gateway presents
   ``Authorization: Bearer <token>`` on the ``/relay`` WebSocket upgrade, where
   ``token = make_upgrade_token(gateway_id, secret)``. Mirrors the connector's
   ``relayAuthToken.ts`` ``makeToken`` (``src/core/relayAuthToken.ts``):
   ``base64url(f"{payload}:{exp}:{sig}")`` with
   ``sig = HMAC_SHA256(f"{payload}:{exp}", secret).hexdigest()`` and
   ``payload == gateway_id``.

2. **Inbound delivery signature** (connector → gateway): the connector signs
   each inbound POST with the per-tenant *delivery key*, carried as
   ``x-relay-timestamp`` + ``x-relay-signature`` headers; the gateway verifies
   before accepting the event. Mirrors the connector's ``deliverySigning.ts``:
   ``sig = HMAC_SHA256(f"{ts}.{body_json}", key).hexdigest()`` over the EXACT
   request body bytes, with a replay-window skew check.

Both schemes use a **multi-secret verify list** (primary first, then a secondary
during a rotation window), exactly like ``api/src/handlers/stats_oauth.ts`` — so
a secret rotation doesn't invalidate outstanding tokens.

EXPERIMENTAL: may change without a deprecation cycle until ≥2 Class-1 platforms
validate the relay contract.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

# Header names the connector uses for inbound delivery signatures
# (connector ``src/core/deliverySigning.ts`` — DELIVERY_TS_HEADER / SIG_HEADER).

# Default replay window for an inbound delivery signature (connector default).
# Default TTL for an upgrade token (connector ``makeUpgradeToken`` default).
_DEFAULT_UPGRADE_TTL_SECONDS = 300


def _hmac_hex(payload: str, secret: str) -> str:
    """HMAC-SHA256 hex digest of ``payload`` under ``secret`` (UTF-8)."""
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def sign(payload: str, secret: str) -> str:
    """HMAC-SHA256 hex digest — the connector's ``sign`` (relayAuthToken.ts)."""
    return _hmac_hex(payload, secret)


def make_token(payload: str, secret: str, ttl_seconds: int = 0) -> str:
    """Build a signed, optionally-expiring token — the connector's ``makeToken``.

    ``base64url(f"{payload}:{exp}:{sig}")`` where ``exp`` is a unix-seconds
    expiry (0 = never) and ``sig = HMAC_SHA256(f"{payload}:{exp}", secret)``.
    base64url is unpadded to match Node's ``Buffer.toString("base64url")``.
    """
    exp = int(time.time()) + ttl_seconds if ttl_seconds > 0 else 0
    signed = f"{payload}:{exp}"
    sig = _hmac_hex(signed, secret)
    raw = f"{signed}:{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_upgrade_token(
    gateway_id: str, secret: str, ttl_seconds: int = _DEFAULT_UPGRADE_TTL_SECONDS
) -> str:
    """The WS-upgrade bearer token a gateway sends: ``payload = gateway_id``.

    The connector peeks ``gateway_id`` (the payload head) to index its secret
    verify list, then verifies the signature against that gateway's stored
    secret(s). Mirrors the connector's ``makeUpgradeToken``.
    """
    return make_token(gateway_id, secret, ttl_seconds)


