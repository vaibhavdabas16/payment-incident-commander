"""HMAC signing for the outbound action webhook.

The webhook tells a merchant's payment stack to move real traffic, so the receiving end has to be
able to prove the request came from here and is not a replay. Same construction as most payment
webhook schemes, for the boring reason that it is the one integrators already have code for:

    X-PIC-Signature: t=<unix seconds>,v1=<hex hmac-sha256 of "<t>.<body>">

The timestamp is inside the signed string, not merely alongside it, so an attacker cannot take a
valid old request and re-stamp it. Verification is constant-time, and rejects anything outside a
tolerance window; five minutes is enough for clock drift and not enough to be useful to a replayer.
"""

from __future__ import annotations

import hashlib
import hmac
import time

DEFAULT_TOLERANCE_S = 300
SCHEME = "v1"


def _digest(secret: str, timestamp: int, body: bytes) -> str:
    payload = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    """Build the `X-PIC-Signature` header value for a request body."""
    ts = int(time.time()) if timestamp is None else timestamp
    return f"t={ts},{SCHEME}={_digest(secret, ts, body)}"


def verify(
    secret: str, header: str, body: bytes, tolerance_s: int = DEFAULT_TOLERANCE_S, now: int | None = None
) -> bool:
    """Check a signature header against a body. False for anything malformed, stale or wrong."""
    if not header or not secret:
        return False

    parts: dict[str, str] = {}
    for chunk in header.split(","):
        key, _, value = chunk.strip().partition("=")
        if key and value:
            parts[key.strip()] = value.strip()

    raw_ts, provided = parts.get("t"), parts.get(SCHEME)
    if not raw_ts or not provided:
        return False

    try:
        timestamp = int(raw_ts)
    except ValueError:
        return False

    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance_s:
        return False

    return hmac.compare_digest(provided, _digest(secret, timestamp, body))
