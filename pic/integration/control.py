"""The control plane for a real deployment: actions leave as signed webhooks.

`write_tools.py` calls `ctx.control.shift_traffic(...)` and does not care what is on the other end.
In the demo that is the simulator's own routing table. Here it is the merchant's payment stack,
reached over HTTP, and the method surface is identical so nothing above this file changes.

Two properties matter more than the transport:

  * **An action is applied only if the merchant says so.** A non-2xx response, a timeout or an
    unreachable endpoint raises, which the Action Agent records as a failed intervention and the
    supervisor escalates. It never assumes success — the whole verification story rests on the
    system knowing what it actually did.
  * **State mirrors what the merchant confirmed, not what was requested.** `snapshot()` and the
    inverse actions are built from the mirror, so a rollback undoes what really happened. If the
    merchant returns its own resulting configuration, that is what gets recorded.

The mirror is deliberately thin. This process is not the source of truth for a merchant's routing
table and should never behave as though it is; it remembers only what it changed, so it can undo it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .signing import sign

DEFAULT_TIMEOUT_S = 10.0


class ActionRejected(RuntimeError):
    """The merchant's endpoint refused or failed to apply an action."""


@dataclass
class WebhookControlPlane:
    """Dispatches control-plane actions to a merchant endpoint over signed HTTP."""

    endpoint: str
    secret: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    # Injectable so tests and dry runs do not need a network. Takes (url, headers, body) and
    # returns (status_code, parsed_json_or_None).
    transport: Callable[[str, dict[str, str], bytes], tuple[int, Any]] | None = None

    route_weights: dict[str, dict[str, float]] = field(default_factory=dict)
    disabled_methods: set[str] = field(default_factory=set)
    max_retries: int = 1
    retry_enabled: bool = True
    monitoring_interval_s: int = 120
    rolled_back_changes: set[str] = field(default_factory=set)
    recovery_campaigns: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Every dispatch, for the audit trail and for support conversations that begin "your system
    # says it moved our traffic at 03:14".
    dispatched: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------- transport

    def _post(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"action": action, "parameters": payload}, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            "X-PIC-Signature": sign(self.secret, body),
            "X-PIC-Action": action,
        }
        try:
            if self.transport is not None:
                status, parsed = self.transport(self.endpoint, headers, body)
            else:
                response = httpx.post(
                    self.endpoint, content=body, headers=headers, timeout=self.timeout_s
                )
                status = response.status_code
                try:
                    parsed = response.json()
                except ValueError:
                    parsed = None
        except httpx.HTTPError as exc:
            # Unreachable is not "applied". Raising here is what makes the agent escalate instead
            # of going on to verify a change that never happened.
            raise ActionRejected(f"{action} could not be delivered: {exc}") from exc

        self.dispatched.append({"action": action, "parameters": payload, "status": status})
        if not 200 <= status < 300:
            detail = parsed if isinstance(parsed, (str, dict)) else None
            raise ActionRejected(f"{action} refused by merchant endpoint (HTTP {status}): {detail}")
        return parsed if isinstance(parsed, dict) else {}

    # ---------------------------------------------------------------- writes

    def weights_for(self, method: str) -> dict[str, float]:
        return self.route_weights.get(method, {})

    def shift_traffic(
        self, from_route: str, to_route: str, percentage: float, payment_method: str | None = None
    ) -> dict:
        confirmed = self._post(
            "shift_traffic",
            {
                "from_route": from_route,
                "to_route": to_route,
                "percentage": percentage,
                "payment_method": payment_method,
            },
        )
        before = {m: dict(w) for m, w in self.route_weights.items()}
        after = confirmed.get("weights_after")
        if isinstance(after, dict):
            # The merchant is the source of truth for what its routing table now looks like.
            self.route_weights = {m: dict(w) for m, w in after.items()}
        return {
            "from_route": from_route,
            "to_route": to_route,
            "percentage": percentage,
            "payment_method": payment_method,
            "weights_before": before,
            "weights_after": {m: dict(w) for m, w in self.route_weights.items()},
            "effective_share_moved": confirmed.get("effective_share_moved", percentage / 100.0),
            "confirmed_by_merchant": True,
        }

    def disable_method(self, method: str) -> dict:
        self._post("disable_method", {"payment_method": method})
        self.disabled_methods.add(method)
        return {"payment_method": method, "disabled": True}

    def enable_method(self, method: str) -> dict:
        self._post("enable_method", {"payment_method": method})
        self.disabled_methods.discard(method)
        return {"payment_method": method, "disabled": False}

    def configure_retry(self, max_retries: int, enabled: bool = True) -> dict:
        before = {"max_retries": self.max_retries, "retry_enabled": self.retry_enabled}
        self._post("configure_retry", {"max_retries": max_retries, "enabled": enabled})
        self.max_retries = max_retries
        self.retry_enabled = enabled
        return {"before": before, "after": {"max_retries": max_retries, "retry_enabled": enabled}}

    def rollback_config_change(self, change_id: str) -> dict:
        already = change_id in self.rolled_back_changes
        self._post("rollback_config_change", {"change_id": change_id})
        self.rolled_back_changes.add(change_id)
        return {"change_id": change_id, "already_rolled_back": already}

    def restore_config_change(self, change_id: str) -> dict:
        self._post("restore_config_change", {"change_id": change_id})
        self.rolled_back_changes.discard(change_id)
        return {"change_id": change_id, "restored": True}

    def recover_payments(
        self, campaign_id: str, orders: list[dict], methods: list[str] | None = None
    ) -> dict:
        """Ask the merchant to re-present failed payments, and record what they report back.

        Same rule as every other write here: what the merchant confirms is what gets recorded. An
        endpoint that does not answer with counts is treated as having recovered nothing, because
        the alternative is reporting revenue on the strength of a 200.
        """
        confirmed = self._post(
            "recover_payments",
            {"campaign_id": campaign_id, "orders": orders, "methods": methods},
        )
        self.recovery_campaigns[campaign_id] = confirmed
        return {
            "campaign_id": campaign_id,
            "attempted": int(confirmed.get("attempted", 0)),
            "recovered": int(confirmed.get("recovered", 0)),
            "attempted_value_paise": int(confirmed.get("attempted_value_paise", 0)),
            "recovered_value_paise": int(confirmed.get("recovered_value_paise", 0)),
            "by_method": confirmed.get("by_method", {}) or {},
            "skipped_by_method": confirmed.get("skipped_by_method", {}) or {},
            "cancelled": False,
            "confirmed_by_merchant": True,
        }

    def cancel_recovery(self, campaign_id: str) -> dict:
        confirmed = self._post("cancel_recovery", {"campaign_id": campaign_id})
        campaign = self.recovery_campaigns.get(campaign_id, {})
        return {
            "campaign_id": campaign_id,
            "cancelled": True,
            "known": bool(campaign),
            "already_recovered": int(confirmed.get("already_recovered", campaign.get("recovered", 0))),
            "note": (
                "Further attempts stopped. Payments already completed by customers are not "
                "reversed by a rollback."
            ),
        }

    def snapshot(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "rolled_back_changes": sorted(self.rolled_back_changes),
            "route_weights": {m: dict(w) for m, w in self.route_weights.items()},
            "disabled_methods": sorted(self.disabled_methods),
            "max_retries": self.max_retries,
            "retry_enabled": self.retry_enabled,
            "monitoring_interval_s": self.monitoring_interval_s,
            "actions_dispatched": len(self.dispatched),
        }


@dataclass
class ReadOnlyControlPlane:
    """Refuses every write, with an explanation.

    The default for a live tenant that has not configured an action endpoint. Detection,
    investigation, impact and diagnosis all work — which is most of the value and all of the risk
    profile of a read-only integration — and any attempt to act fails closed rather than silently
    doing nothing, so the incident is handed to a human instead of appearing to have been fixed.
    """

    reason: str = (
        "no action endpoint is configured for this merchant, so nothing can be changed "
        "automatically"
    )
    route_weights: dict[str, dict[str, float]] = field(default_factory=dict)
    disabled_methods: set[str] = field(default_factory=set)
    max_retries: int = 1
    retry_enabled: bool = True
    monitoring_interval_s: int = 120
    rolled_back_changes: set[str] = field(default_factory=set)
    recovery_campaigns: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _refuse(self, *_args: Any, **_kwargs: Any) -> dict:
        raise ActionRejected(self.reason)

    weights_for = staticmethod(lambda method: {})
    shift_traffic = _refuse
    disable_method = _refuse
    enable_method = _refuse
    configure_retry = _refuse
    rollback_config_change = _refuse
    restore_config_change = _refuse
    recover_payments = _refuse
    cancel_recovery = _refuse

    def snapshot(self) -> dict:
        return {"mode": "read_only", "reason": self.reason}
