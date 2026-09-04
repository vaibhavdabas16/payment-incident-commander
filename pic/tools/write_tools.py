"""Write tools — the only code in the system with side effects.

Each mutates the payment control plane and returns both a result and an explicit `inverse_action`
describing exactly how to undo it. Rollback then replays a recorded inverse rather than trying to
reconstruct prior state from memory, which is what makes recovery from a failed intervention
reliable rather than best-effort.

The registry refuses to invoke any of these without an approved `PolicyDecision` (ADR-002), so the
approval check is structural rather than a convention these functions are trusted to follow.

**Adapter boundary.** These currently drive the local simulator's control plane. In a deployment
they would call Razorpay routing/config APIs; the signatures and the audit record are the contract,
and `adapter` records which backend actually executed. Nothing above this layer changes.
"""

from __future__ import annotations

from typing import Any

from .registry import ToolContext, ToolSpec

ADAPTER = "simulator"


def _require_control(ctx: ToolContext) -> Any:
    if ctx.control is None:
        raise RuntimeError("no control plane bound to this tool context")
    return ctx.control


def shift_traffic(
    ctx: ToolContext,
    from_route: str,
    to_route: str,
    percentage: float,
    payment_method: str | None = None,
) -> dict[str, Any]:
    """Move a share of traffic from one route to another."""
    control = _require_control(ctx)
    if from_route == to_route:
        raise ValueError("from_route and to_route must differ")
    if percentage <= 0:
        raise ValueError("percentage must be positive")
    detail = control.shift_traffic(from_route, to_route, percentage, payment_method)
    return {
        "adapter": ADAPTER,
        "detail": detail,
        "inverse_action": {
            "tool": "shift_traffic",
            "arguments": {
                "from_route": to_route,
                "to_route": from_route,
                # Undo exactly what moved, which may be less than requested if the source route
                # did not carry the full amount.
                "percentage": detail["effective_share_moved"] * 100,
                "payment_method": payment_method,
            },
        },
    }


def disable_payment_method(ctx: ToolContext, payment_method: str) -> dict[str, Any]:
    """Temporarily stop offering a payment method so customers fall back to a working one."""
    control = _require_control(ctx)
    detail = control.disable_method(payment_method)
    return {
        "adapter": ADAPTER,
        "detail": detail,
        "inverse_action": {
            "tool": "enable_payment_method",
            "arguments": {"payment_method": payment_method},
        },
    }


def enable_payment_method(ctx: ToolContext, payment_method: str) -> dict[str, Any]:
    control = _require_control(ctx)
    detail = control.enable_method(payment_method)
    return {
        "adapter": ADAPTER,
        "detail": detail,
        "inverse_action": {
            "tool": "disable_payment_method",
            "arguments": {"payment_method": payment_method},
        },
    }


def configure_retry(ctx: ToolContext, max_retries: int, enabled: bool = True) -> dict[str, Any]:
    """Change retry policy for failed payments."""
    control = _require_control(ctx)
    detail = control.configure_retry(max_retries, enabled)
    return {
        "adapter": ADAPTER,
        "detail": detail,
        "inverse_action": {
            "tool": "configure_retry",
            "arguments": {
                "max_retries": detail["before"]["max_retries"],
                "enabled": detail["before"]["retry_enabled"],
            },
        },
    }


def rollback_change(ctx: ToolContext, change_id: str) -> dict[str, Any]:
    """Revert a merchant configuration change identified during investigation.

    This used to record intent only. The simulator kept degrading traffic regardless, so the one
    action that genuinely addresses a config regression could never show a recovery: verification
    read no improvement and the agent dutifully reverted its own correct fix. Every failed
    rollback in the benchmark was one of these, which made `rollback_success_rate` a statement
    about the simulator rather than about the agent.

    The control plane now honours it - a degradation caused by a recorded change stops when that
    change is reverted - and the action carries a real inverse, so reverting it is reversible like
    any other write.
    """
    changes = {c.change_id: c for c in ctx.store.config_changes(
        ctx.now.replace(year=ctx.now.year - 1), ctx.now
    )}
    change = changes.get(change_id)
    if change is None:
        raise ValueError(f"unknown change_id {change_id!r}")
    if not change.reversible:
        raise ValueError(f"change {change_id!r} is not reversible")
    control = _require_control(ctx)
    applied = control.rollback_config_change(change_id)
    ctx.notifications.append(
        {
            "channel": "deploy",
            "subject": f"Rollback requested for {change.component}",
            "body": change.description,
            "at": ctx.now.isoformat(),
        }
    )
    return {
        "adapter": ADAPTER,
        "detail": {
            "change_id": change_id,
            "component": change.component,
            "rollback_requested": True,
            "already_rolled_back": applied["already_rolled_back"],
            "note": "Change reverted; traffic affected by it recovers from this point.",
        },
        "inverse_action": {
            "tool": "restore_change",
            "arguments": {"change_id": change_id},
        },
    }


def restore_change(ctx: ToolContext, change_id: str) -> dict[str, Any]:
    """Re-apply a configuration change, undoing a rollback.

    Exists so `rollback_change` is reversible on the same terms as every other write tool: if
    reverting the deploy does not help, the agent can put it back rather than leaving the merchant
    in a third state nobody chose.
    """
    control = _require_control(ctx)
    control.restore_config_change(change_id)
    return {
        "adapter": ADAPTER,
        "detail": {"change_id": change_id, "restored": True},
        "inverse_action": {
            "tool": "rollback_change",
            "arguments": {"change_id": change_id},
        },
    }


def recover_failed_payments(
    ctx: ToolContext,
    orders: list[dict[str, Any]],
    methods: list[str] | None = None,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    """Re-present payments that failed during the incident, and report what actually completed.

    The second recovery layer. Fixing the routing stops further losses; this is the only thing in
    the system that goes after the orders already lost.

    `orders` comes from `get_recoverable_orders`, which reads them out of the event store — this
    tool cannot invent an order, and an order the store does not contain cannot be recovered by
    it. `methods` is whatever merchant policy granted, so a method the merchant has not authorised
    is never attempted even if the campaign asks for it.
    """
    control = _require_control(ctx)
    if not orders:
        raise ValueError("no orders to recover")
    campaign = campaign_id or f"rec_{ctx.incident_id}_{len(ctx.notifications)}"
    detail = control.recover_payments(campaign, orders, methods)
    return {
        "adapter": ADAPTER,
        "detail": detail,
        # The inverse stops the campaign. It does not — and nothing could — reverse payments a
        # customer has already completed; `cancel_recovery` says so in its own result rather than
        # letting a rollback imply more than it did.
        "inverse_action": {
            "tool": "cancel_order_recovery",
            "arguments": {"campaign_id": campaign},
        },
    }


def cancel_order_recovery(ctx: ToolContext, campaign_id: str) -> dict[str, Any]:
    """Stop a recovery campaign. The inverse of `recover_failed_payments`."""
    control = _require_control(ctx)
    detail = control.cancel_recovery(campaign_id)
    return {
        "adapter": ADAPTER,
        "detail": detail,
        "inverse_action": None,
    }


def set_monitoring_frequency(ctx: ToolContext, interval_seconds: int) -> dict[str, Any]:
    """Raise or lower observation cadence. Always safe and always reversible."""
    control = _require_control(ctx)
    before = control.monitoring_interval_s
    control.monitoring_interval_s = interval_seconds
    return {
        "adapter": ADAPTER,
        "detail": {"before_seconds": before, "after_seconds": interval_seconds},
        "inverse_action": {
            "tool": "set_monitoring_frequency",
            "arguments": {"interval_seconds": before},
        },
    }


def notify_merchant(
    ctx: ToolContext, subject: str, body: str, urgency: str = "normal"
) -> dict[str, Any]:
    """Tell the merchant something they must act on themselves."""
    note = {
        "channel": "merchant",
        "subject": subject,
        "body": body,
        "urgency": urgency,
        "at": ctx.now.isoformat(),
        "incident_id": ctx.incident_id,
    }
    ctx.notifications.append(note)
    return {"adapter": ADAPTER, "detail": note, "inverse_action": None}


def create_incident_ticket(
    ctx: ToolContext, title: str, description: str, severity: str = "HIGH"
) -> dict[str, Any]:
    ticket = {
        "ticket_id": f"TCK-{len(ctx.tickets) + 1:04d}",
        "incident_id": ctx.incident_id,
        "title": title,
        "description": description,
        "severity": severity,
        "created_at": ctx.now.isoformat(),
    }
    ctx.tickets.append(ticket)
    return {"adapter": ADAPTER, "detail": ticket, "inverse_action": None}


SPECS = [
    ToolSpec(
        name="shift_traffic",
        description=(
            "Move a percentage of payment traffic from one route to another. Reversible. "
            "Only helps when an alternative route is healthy."
        ),
        parameters={
            "from_route": {"type": "string", "required": True},
            "to_route": {"type": "string", "required": True},
            "percentage": {
                "type": "number",
                "required": True,
                "description": "Percentage points of total traffic to move.",
            },
            "payment_method": {
                "type": "string",
                "description": "Restrict the shift to one payment method.",
            },
        },
        func=shift_traffic,
        write=True,
        inverse="shift_traffic",
    ),
    ToolSpec(
        name="disable_payment_method",
        description=(
            "Stop offering a payment method so customers fall back to a working one. Reversible, "
            "but visibly changes checkout, so it is a heavier intervention than rerouting."
        ),
        parameters={"payment_method": {"type": "string", "required": True}},
        func=disable_payment_method,
        write=True,
        inverse="enable_payment_method",
    ),
    ToolSpec(
        name="enable_payment_method",
        description="Re-enable a previously disabled payment method.",
        parameters={"payment_method": {"type": "string", "required": True}},
        func=enable_payment_method,
        write=True,
        inverse="disable_payment_method",
    ),
    ToolSpec(
        name="configure_retry",
        description=(
            "Change the retry policy for failed payments. Useful for transient timeout errors, "
            "useless for hard declines, and it increases load on an already degraded provider."
        ),
        parameters={
            "max_retries": {"type": "integer", "required": True},
            "enabled": {"type": "boolean"},
        },
        func=configure_retry,
        write=True,
        inverse="configure_retry",
    ),
    ToolSpec(
        name="rollback_change",
        description=(
            "Request rollback of a merchant configuration change. The correct action when a "
            "config change immediately precedes the degradation."
        ),
        parameters={"change_id": {"type": "string", "required": True}},
        func=rollback_change,
        write=True,
        inverse="restore_change",
    ),
    ToolSpec(
        name="restore_change",
        description=(
            "Re-apply a configuration change that was rolled back. The inverse of "
            "rollback_change, used when the rollback did not help."
        ),
        parameters={"change_id": {"type": "string", "required": True}},
        func=restore_change,
        write=True,
        inverse="rollback_change",
    ),
    ToolSpec(
        name="set_monitoring_frequency",
        description="Change how often the system evaluates payment health. Always safe.",
        parameters={"interval_seconds": {"type": "integer", "required": True}},
        func=set_monitoring_frequency,
        write=True,
        inverse="set_monitoring_frequency",
    ),
    ToolSpec(
        name="recover_failed_payments",
        description=(
            "Re-present payments that failed during the incident, by retry, alternate route or "
            "payment link. Only attempts orders the event store actually contains and only by "
            "methods merchant policy has granted."
        ),
        parameters={
            "orders": {
                "type": "array",
                "required": True,
                "description": "Recoverable orders from get_recoverable_orders.",
            },
            "methods": {
                "type": "array",
                "description": "Recovery methods permitted by merchant policy.",
            },
            "campaign_id": {"type": "string"},
        },
        func=recover_failed_payments,
        write=True,
        inverse="cancel_order_recovery",
    ),
    ToolSpec(
        name="cancel_order_recovery",
        description=(
            "Stop a recovery campaign. Halts further attempts; cannot reverse payments customers "
            "have already completed."
        ),
        parameters={"campaign_id": {"type": "string", "required": True}},
        func=cancel_order_recovery,
        write=True,
        inverse=None,
    ),
    ToolSpec(
        name="notify_merchant",
        description=(
            "Alert the merchant about something only they can fix, such as an issuer-side outage."
        ),
        parameters={
            "subject": {"type": "string", "required": True},
            "body": {"type": "string", "required": True},
            "urgency": {"type": "string"},
        },
        func=notify_merchant,
        write=True,
        inverse=None,
    ),
    ToolSpec(
        name="create_incident_ticket",
        description="Open a tracked ticket for human follow-up.",
        parameters={
            "title": {"type": "string", "required": True},
            "description": {"type": "string", "required": True},
            "severity": {"type": "string"},
        },
        func=create_incident_ticket,
        write=True,
        inverse=None,
    ),
]
