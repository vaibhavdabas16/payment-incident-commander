"""Turning a finished incident into the structured record the system remembers.

One function, and it is deliberately dull: every field is copied from a typed agent output or
computed from one. Nothing here calls a model, and nothing here reads prose. That is the whole
guarantee behind "the agent cannot invent a historical incident" — the only writer of memory is
this function, and it can only write what the incident actually recorded.

It lives apart from `store.py` so the store stays a query surface with no knowledge of the
incident lifecycle, and apart from the supervisor so the supervisor does not grow a second job.
"""

from __future__ import annotations

from typing import Any

from ..revenue import compute_revenue_outcome
from ..schemas import (
    ActionResult,
    IncidentOutcomeRecord,
    IncidentRecord,
    IncidentState,
    RevenueOutcome,
)
from .store import build_signature

# Outcomes that mean a person had to be involved, whether by approving, overriding, acknowledging
# or being handed the incident outright.
_HUMAN_OUTCOMES = {
    "AWAITING_APPROVAL",
    "REJECTED_BY_HUMAN",
    "ACKNOWLEDGED_BY_HUMAN",
    "ESCALATED",
    "HANDED_TO_HUMAN",
}


def build_outcome_record(
    incident: IncidentRecord,
    *,
    revenue: RevenueOutcome | None = None,
    route_health: dict[str, float] | None = None,
    executed: ActionResult | None = None,
) -> IncidentOutcomeRecord:
    """Reduce a completed incident to the record that will inform the next one.

    `executed` exists because a reverted intervention clears `incident.action_result` - correctly,
    so the reverted shift stops counting against the cumulative-shift ceiling. Learning needs the
    opposite: an intervention that had to be undone is the most valuable record the system can
    hold, and taking the field at face value here would erase every failure from memory while
    keeping every success. The supervisor keeps what was executed and passes it in.
    """
    revenue = revenue or incident.revenue or compute_revenue_outcome(incident)
    signature = build_signature(incident, route_health=route_health)

    anomaly = incident.anomaly
    evidence = incident.evidence
    root_cause = incident.root_cause
    proposal = incident.proposal
    decision = incident.policy_decision
    executed = executed or incident.action_result
    verification = incident.verification

    detected_metrics: dict[str, Any] = {}
    if anomaly:
        detected_metrics = {
            "success_rate": anomaly.current_value,
            "baseline_success_rate": anomaly.baseline,
            "deviation": anomaly.deviation,
            "z_score": anomaly.z_score,
            "confidence": anomaly.confidence,
            "sample_size": anomaly.sample_size,
            "severity": anomaly.severity.value,
            "detection_method": list(anomaly.detection_method),
            "estimated_revenue_at_risk_per_hour_paise": anomaly.estimated_revenue_at_risk_paise,
        }
    if evidence:
        detected_metrics["dominant_error_code"] = evidence.dominant_error_code
        detected_metrics["dominant_error_share"] = evidence.dominant_error_share
        detected_metrics["latency_shift_ms"] = evidence.latency_shift_ms

    candidates: list[dict[str, Any]] = []
    if incident.recovery_plan is not None:
        candidates = [
            {
                "strategy_id": s.strategy_id,
                "action": s.action.value,
                "target": s.target,
                "magnitude": s.magnitude,
                "expected_value_paise": s.expected_value_paise,
                "expected_revenue_protected_per_hour_paise": (
                    s.expected_revenue_protected_per_hour_paise
                ),
                "risk_score": s.risk_score,
                "p_success": s.p_success,
                "historical_attempts": (
                    s.historical_support.matched_incidents if s.historical_support else 0
                ),
            }
            for s in incident.recovery_plan.strategies
        ]
    elif proposal is not None:
        candidates = list(proposal.alternatives_considered)

    control: dict[str, Any] = {}
    treatment: dict[str, Any] = {}
    if verification is not None and verification.control_used:
        control = {
            "success_rate": verification.control_success_rate,
            "sample": verification.control_sample,
            "route": (executed.parameters.get("from_route") if executed else None),
        }
        treatment = {
            "success_rate": verification.treated_success_rate,
            "sample": verification.treated_sample,
            "route": (executed.parameters.get("to_route") if executed else None),
            "difference": (
                round(verification.treated_success_rate - verification.control_success_rate, 4)
                if verification.treated_success_rate is not None
                and verification.control_success_rate is not None
                else None
            ),
            "p_value": verification.p_value,
        }
    elif verification is not None:
        # No control group existed for this action. Say so, rather than presenting a before/after
        # pair as though it were an experiment.
        treatment = {
            "before_success_rate": verification.before_success_rate,
            "after_success_rate": verification.after_success_rate,
            "sample": verification.after_sample,
            "p_value": verification.p_value,
            "control_available": False,
        }

    rollbacks = [r for r in incident.audit if str(r.approved_by).startswith("policy_engine:rollback")]
    rollback_required = bool(rollbacks)
    if rollback_required:
        rollback_result = (
            "SUCCEEDED" if all(r.execution_result == "success" for r in rollbacks) else "FAILED"
        )
    else:
        rollback_result = "NOT_REQUIRED"

    magnitude = None
    if executed is not None and "percentage" in executed.parameters:
        try:
            magnitude = float(executed.parameters["percentage"])
        except (TypeError, ValueError):
            magnitude = None

    time_to_recovery = None
    if incident.closed_at is not None:
        time_to_recovery = (incident.closed_at - incident.opened_at).total_seconds()

    human_required = (
        (incident.outcome in _HUMAN_OUTCOMES)
        or incident.escalation is not None
        or incident.state is IncidentState.AWAITING_HUMAN_APPROVAL
        or (decision is not None and decision.requires_human)
        or (decision is not None and not decision.approved_by.startswith("policy_engine"))
    )

    false_positive = (
        root_cause is not None and root_cause.cause_id == "traffic_mix_shift"
    ) or incident.outcome == "FALSE_POSITIVE"

    return IncidentOutcomeRecord(
        incident_id=incident.incident_id,
        timestamp=incident.closed_at or incident.opened_at,
        merchant_id=incident.merchant_id,
        failure_signature=signature,
        affected_segments=[
            dict(s.segment.dimensions) for s in (anomaly.affected_segments[:5] if anomaly else [])
        ],
        detected_metrics=detected_metrics,
        root_cause_hypotheses=[
            {
                "cause_id": h.cause_id,
                "cause": h.cause,
                "probability": h.probability,
                "memory_adjustment": h.memory_adjustment,
            }
            for h in (root_cause.hypotheses[:5] if root_cause else [])
        ],
        selected_root_cause=root_cause.most_likely_root_cause if root_cause else "",
        selected_root_cause_id=root_cause.cause_id if root_cause else "",
        root_cause_confidence=root_cause.confidence if root_cause else 0.0,
        revenue_at_risk_paise=revenue.revenue_at_risk_paise,
        revenue_at_risk_per_hour_paise=revenue.revenue_at_risk_per_hour_paise,
        candidate_actions=candidates,
        selected_action=proposal.action.value if proposal else "none",
        selected_parameters=dict(proposal.parameters) if proposal else {},
        actual_action_executed=(
            executed.action.value if executed is not None and executed.executed else "none"
        ),
        executed_parameters=dict(executed.parameters) if executed else {},
        intervention_magnitude=magnitude,
        policy_result=decision.outcome.value if decision else "",
        policy_bound_by=list(decision.bound_by) if decision else [],
        control_group_metrics=control,
        treatment_group_metrics=treatment,
        verification_result=verification.status.value if verification else "NOT_VERIFIED",
        verification_significant=bool(verification and verification.statistically_significant),
        revenue_protected_paise=revenue.revenue_protected_paise,
        revenue_recovered_paise=revenue.revenue_recovered_paise,
        revenue_lost_paise=revenue.revenue_lost_paise,
        recovery_rate=revenue.recovery_rate,
        order_recovery=incident.order_recovery,
        rollback_required=rollback_required,
        rollback_result=rollback_result,
        time_to_recovery_s=time_to_recovery,
        time_to_mitigate_s=incident.time_to_mitigate_s,
        human_intervention_required=bool(human_required),
        final_resolution=incident.outcome or (incident.state.value if incident.state else "UNKNOWN"),
        false_positive=false_positive,
    )
