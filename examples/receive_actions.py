"""Receive actions from a Payment Incident Commander deployment.

The other half of the integration: a webhook endpoint that verifies the signature and applies the
change to your payment stack. Run it, point `action_endpoint` at it, and you have the full loop.

    uvicorn examples.receive_actions:app --port 9000

Two things this file is trying to demonstrate, both of which matter more than the transport:

  * **Verify before you act.** A request that moves payment traffic must be proven to have come
    from your deployment. `verify_signature` is fifteen lines and there is no excuse for skipping it.
  * **Answer honestly.** Return 2xx only if you actually applied the change. Anything else is read
    as "not applied", and the agent hands the incident to a human instead of measuring a change
    that never happened. Lying with a 200 is the one thing that breaks the safety model.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI(title="Merchant action receiver")

SECRET = os.getenv("PIC_ACTION_SECRET", "whsec_change_me")
TOLERANCE_S = 300

# Your live routing table. In a real system this is a database row or a config service, not a dict.
ROUTE_WEIGHTS: dict[str, dict[str, float]] = {
    "upi": {"route_upi_primary": 0.8, "route_upi_backup": 0.2},
}


def verify_signature(header: str | None, body: bytes) -> bool:
    """Constant-time check of `X-PIC-Signature`, with the timestamp inside the signed string."""
    if not header:
        return False
    parts = dict(chunk.split("=", 1) for chunk in header.split(",") if "=" in chunk)
    raw_ts, provided = parts.get("t"), parts.get("v1")
    if not raw_ts or not provided:
        return False
    try:
        timestamp = int(raw_ts)
    except ValueError:
        return False
    if abs(int(time.time()) - timestamp) > TOLERANCE_S:
        return False  # too old to be anything but a replay
    expected = hmac.new(
        SECRET.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(provided, expected)


def apply_shift(params: dict[str, Any]) -> dict[str, Any]:
    """Move traffic between two routes. Replace with a call to your own control plane."""
    method = params.get("payment_method") or "upi"
    weights = ROUTE_WEIGHTS.setdefault(method, {})
    moving = min(weights.get(params["from_route"], 0.0), float(params["percentage"]) / 100.0)
    weights[params["from_route"]] = weights.get(params["from_route"], 0.0) - moving
    weights[params["to_route"]] = weights.get(params["to_route"], 0.0) + moving
    # Returning the resulting weights makes them the recorded truth, so a later rollback undoes
    # what really happened rather than what was requested.
    return {"weights_after": ROUTE_WEIGHTS, "effective_share_moved": round(moving, 4)}


@app.post("/pic/actions")
async def receive(request: Request, x_pic_signature: str | None = Header(default=None)) -> dict:
    body = await request.body()
    if not verify_signature(x_pic_signature, body):
        raise HTTPException(status_code=401, detail="bad signature")

    payload = await request.json()
    action, params = payload.get("action"), payload.get("parameters") or {}
    print(f"[action] {action} {params}", flush=True)

    try:
        if action == "shift_traffic":
            return apply_shift(params)
        if action in ("disable_method", "enable_method", "configure_retry"):
            # Apply it for real here. Raising instead of pretending is the correct failure.
            return {"applied": True}
        if action in ("rollback_config_change", "restore_config_change"):
            return {"applied": True, "change_id": params.get("change_id")}
    except Exception as exc:
        # A 5xx means "not applied", which is exactly what you want the agent to believe.
        raise HTTPException(status_code=500, detail=f"could not apply {action}: {exc}") from exc

    raise HTTPException(status_code=400, detail=f"unsupported action {action!r}")
