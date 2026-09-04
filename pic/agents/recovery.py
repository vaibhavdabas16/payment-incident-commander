"""Order Recovery Agent — the second recovery layer.

Once the infrastructure fault is gone, the merchant is still short every payment that failed while
it was there. Those orders are real, they are in the event store, and some of them are still
completable. This agent goes after them.

It is deliberately a separate agent at a separate state rather than another branch inside the
Decision Agent, because it is answering a different question at a different time. Infrastructure
recovery is a bet placed on a diagnosis; order recovery is a mop-up performed on a known list.
Mixing them would mean pricing a mop-up as though it were an intervention and, worse, letting a
recovery campaign compete for the incident's intervention budget against the fix itself.

**Nothing about it is exempt from the safety architecture.** It proposes, the policy gateway
decides, and the tool registry refuses the write without an approving decision naming that exact
action. The merchant's policy says how many orders may be chased and by what means; a campaign
that overreaches is clamped rather than refused, and one that needs a method the merchant has
withheld waits for a person.
"""

from __future__ import annotations

from typing import Any

from ..recovery.orders import expected_value_paise
from ..schemas import (
    ActionProposal,
    ActionType,
    AgentResult,
    IncidentState,
    OrderRecoveryResult,
    PolicyOutcome,
    RecoverableOrder,
)
from ..tools.registry import PolicyViolation
from .action import write_audit
from .base import Agent, IncidentContext
from .impact import format_inr
from .strategy import RISK_PRIOR

# Widest window worth looking back over for recoverable orders. A payment that failed an hour ago
# is a support ticket, not a recovery.
MAX_LOOKBACK_MINUTES = 60
MIN_LOOKBACK_MINUTES = 5

# Campaigns below this expected value are not worth proposing: the policy gateway would refuse
# them on expected value anyway, and proposing them burns an intervention slot.
MIN_CAMPAIGN_VALUE_PAISE = 1_000

# Ceiling on how many orders are even looked at. The merchant's policy bound clamps the campaign
# below this; this exists only so a very long incident cannot build an unbounded proposal.
MAX_ORDERS = 5_000


class OrderRecoveryAgent(Agent):
    name = "order_recovery"
    state = IncidentState.RECOVERING_ORDERS

    def run(self, ctx: IncidentContext) -> AgentResult:
        incident = ctx.incident
        result = OrderRecoveryResult(incident_id=incident.incident_id)

        minutes = self._lookback_minutes(ctx)
        # A generous limit: the merchant's own `max_recovery_payments` bound is what should size
        # the campaign, not an arbitrary page size in the lookup tool.
        payload = ctx.call_tool("get_recoverable_orders", minutes=minutes, limit=MAX_ORDERS)
        if not payload:
            result.note = "Recoverable-order lookup failed; nothing was attempted."
            return AgentResult(ok=True, summary=result.note, output=result)

        result.failed_payments = int(payload.get("failed_payments", 0))
        result.recoverable_payments = int(payload.get("recoverable", 0))
        result.recoverable_value_paise = int(payload.get("recoverable_value_paise", 0))

        orders = [RecoverableOrder.model_validate(o) for o in payload.get("orders", [])]
        if not orders:
            result.note = (
                f"{result.failed_payments:,} payments failed during this incident and none of them "
                "is recoverable: they were hard declines, or the customer completed the order "
                "themselves."
            )
            return AgentResult(ok=True, summary="nothing recoverable", output=result)

        expected = expected_value_paise(orders)
        if expected < MIN_CAMPAIGN_VALUE_PAISE:
            result.note = (
                f"Expected recovery of {format_inr(expected)} is too small to be worth a campaign."
            )
            return AgentResult(ok=True, summary="not worth recovering", output=result)

        methods = sorted({o.recovery_method for o in orders})
        proposal = ActionProposal(
            incident_id=incident.incident_id,
            action=ActionType.RECOVER_FAILED_PAYMENTS,
            parameters={
                "orders": [o.model_dump(mode="json") for o in orders],
                "methods": methods,
            },
            rationale=(
                f"{len(orders):,} of {result.failed_payments:,} failed payments are still "
                f"completable, worth {format_inr(result.recoverable_value_paise)} at face value "
                f"and {format_inr(expected)} at their measured recovery probabilities."
            ),
            expected_revenue_protected_per_hour_paise=0,
            expected_value_paise=expected,
            risk_score=RISK_PRIOR[ActionType.RECOVER_FAILED_PAYMENTS],
            confidence=1.0,
            reversible=True,
            proposer="deterministic",
        )

        decision = ctx.gateway.evaluate(
            proposal,
            now=ctx.now,
            anomaly=incident.anomaly,
            root_cause=incident.root_cause,
        )
        ctx.publish(
            "order_recovery_policy",
            {
                "incident_id": incident.incident_id,
                "outcome": decision.outcome.value,
                "reason": decision.reason,
            },
        )

        if not decision.approved:
            result.note = (
                f"Recovery of {len(orders):,} orders was not authorised: {decision.reason}."
                + (
                    " A person can authorise it from the incident."
                    if decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
                    else ""
                )
            )
            return AgentResult(ok=True, summary="recovery not authorised", output=result)

        granted_orders = list(decision.granted_parameters.get("orders", []))
        granted_methods = list(decision.granted_parameters.get("methods", methods))
        try:
            payload, record = ctx.registry.call(
                ActionType.RECOVER_FAILED_PAYMENTS.value,
                {"orders": granted_orders, "methods": granted_methods},
                approval=decision,
            )
        except PolicyViolation as exc:
            return AgentResult(ok=False, summary=f"policy violation: {exc}", error=str(exc))

        if payload is None:
            error = record.error or "recovery tool returned no result"
            result.note = f"Recovery campaign failed: {error}"
            return AgentResult(ok=False, summary=result.note, output=result, error=error)

        detail: dict[str, Any] = payload.get("detail", {})
        result.campaign_id = str(detail.get("campaign_id", ""))
        result.attempted = int(detail.get("attempted", 0))
        result.recovered = int(detail.get("recovered", 0))
        result.recovered_value_paise = int(detail.get("recovered_value_paise", 0))
        result.by_method = dict(detail.get("by_method", {}))
        result.executed = True
        skipped = detail.get("skipped_by_method") or {}
        result.note = (
            f"{result.recovered:,} of {result.attempted:,} attempted orders completed, worth "
            f"{format_inr(result.recovered_value_paise)}."
        )
        if skipped:
            result.note += (
                " Not attempted, because merchant policy does not authorise the method: "
                + ", ".join(f"{n} by {m}" for m, n in sorted(skipped.items()))
                + "."
            )

        write_audit(
            ctx,
            action=ActionType.RECOVER_FAILED_PAYMENTS.value,
            # The order list is summarised rather than copied: an audit record holding twelve
            # thousand order ids is unreadable, and the campaign id is what actually identifies
            # what happened.
            parameters={
                "orders_count": len(granted_orders),
                "methods": granted_methods,
                "campaign_id": result.campaign_id,
            },
            granted_parameters={
                "orders_count": len(granted_orders),
                "methods": granted_methods,
            },
            reason=proposal.rationale,
            approved_by=decision.approved_by,
            policy_outcome=decision.outcome.value,
            execution_result="success",
            adapter=payload.get("adapter", "simulator"),
            reversible=True,
            inverse=payload.get("inverse_action"),
        )
        ctx.gateway.history.record_execution(
            incident.incident_id, ctx.now, ActionType.RECOVER_FAILED_PAYMENTS, {}
        )
        ctx.publish("order_recovery", result.model_dump(mode="json"))
        return AgentResult(
            ok=True,
            summary=(
                f"recovered {result.recovered:,}/{result.attempted:,} failed payments "
                f"({format_inr(result.recovered_value_paise)})"
            ),
            output=result,
        )

    def _lookback_minutes(self, ctx: IncidentContext) -> int:
        """How far back to look for failures: the incident's own life, bounded at both ends."""
        anomaly = ctx.incident.anomaly
        if anomaly is None:
            return MIN_LOOKBACK_MINUTES
        elapsed = (ctx.now - anomaly.window_start).total_seconds() / 60.0
        return int(max(MIN_LOOKBACK_MINUTES, min(MAX_LOOKBACK_MINUTES, round(elapsed) + 1)))
