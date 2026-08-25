"""Escalation Agent.

Knowing when *not* to act autonomously is a feature, not a fallback. This agent packages everything
a human needs to take over in one place — what was seen, what was concluded, what was proposed, why
the system stopped, and what it recommends — so the handover does not cost the operator ten minutes
of reconstruction during an outage.

Escalation is never silent: it always notifies, and always leaves a ticket.
"""

from __future__ import annotations

from ..schemas import (
    AgentResult,
    Escalation,
    IncidentState,
    PolicyDecision,
    PolicyOutcome,
    ActionType,
    Severity,
)
from .base import Agent, IncidentContext
from .impact import format_inr

# Reason codes, each mapping to a recommended human action.
REASONS = {
    "low_confidence": "Diagnosis confidence is below the threshold for autonomous action.",
    "ambiguous_diagnosis": "Competing explanations could not be separated by the evidence.",
    "policy_denied": "The proposed action was refused by merchant policy.",
    "policy_requires_approval": "Merchant policy requires human approval for this action.",
    "irreversible_action": "The only effective action cannot be automatically undone.",
    "intervention_failed": "The intervention did not improve payment success rate.",
    "intervention_regressed": "The intervention made payment success rate worse.",
    "rollback_failed": "An intervention could not be rolled back automatically.",
    "attempts_exhausted": "The maximum number of autonomous interventions has been reached.",
    "no_effective_action": "No available action addresses the diagnosed cause.",
    "agent_failure": "An agent failed unexpectedly and the workflow cannot continue safely.",
}

RECOMMENDATIONS = {
    "low_confidence": "Review the evidence and confirm or correct the diagnosis before acting.",
    "ambiguous_diagnosis": "Investigate the competing segments directly; more than one may be real.",
    "policy_denied": "Decide whether the policy limit should be relaxed for this incident.",
    "policy_requires_approval": "Approve or reject the proposed action in the dashboard.",
    "irreversible_action": "Approve the rollback, or coordinate a forward fix with the owning team.",
    "intervention_failed": "The fallback may be unhealthy too. Check provider status before retrying.",
    "intervention_regressed": "Configuration has been restored. Investigate before intervening again.",
    "rollback_failed": "Restore the previous routing configuration manually and verify immediately.",
    "attempts_exhausted": "Take manual control; automated mitigation has been exhausted.",
    "no_effective_action": "Contact the affected provider or issuer directly.",
    "agent_failure": "Check system logs; treat payment health as unmonitored until resolved.",
}


class EscalationAgent(Agent):
    name = "escalation"
    state = IncidentState.ESCALATED

    def run(self, ctx: IncidentContext) -> AgentResult:
        incident = ctx.incident
        reason_code = ctx.scratch.get("escalation_reason", "agent_failure")
        reason = REASONS.get(reason_code, reason_code)
        recommendation = RECOMMENDATIONS.get(reason_code, "Review the incident manually.")

        impact = incident.impact
        risk = impact.revenue_at_risk_per_hour_paise if impact else 0
        urgency = self._urgency(incident.severity, risk)

        context_pack = {
            "incident_id": incident.incident_id,
            "state": incident.state.value,
            "severity": incident.severity.value,
            "detected_at": incident.opened_at.isoformat(),
            "success_rate": incident.anomaly.current_value if incident.anomaly else None,
            "baseline": incident.anomaly.baseline if incident.anomaly else None,
            "revenue_at_risk_per_hour": format_inr(risk),
            "diagnosis": incident.root_cause.most_likely_root_cause if incident.root_cause else None,
            "diagnosis_confidence": incident.root_cause.confidence if incident.root_cause else None,
            "competing_hypotheses": [
                {"cause": h.cause, "probability": h.probability}
                for h in (incident.root_cause.hypotheses[:3] if incident.root_cause else [])
            ],
            "evidence": [f.statement for f in (incident.evidence.findings[:8] if incident.evidence else [])],
            "proposed_action": incident.proposal.action.value if incident.proposal else None,
            "proposed_parameters": incident.proposal.parameters if incident.proposal else None,
            "policy_outcome": incident.policy_decision.outcome.value if incident.policy_decision else None,
            "policy_reason": incident.policy_decision.reason if incident.policy_decision else None,
            "verification": incident.verification.explanation if incident.verification else None,
            "actions_taken": [a.action for a in incident.audit],
            "attempts": incident.attempts,
        }

        escalation = Escalation(
            incident_id=incident.incident_id,
            reason_code=reason_code,
            reason=reason,
            urgency=urgency,
            recommended_human_action=recommendation,
            context_pack=context_pack,
        )

        self._notify(ctx, escalation, risk)
        return AgentResult(
            ok=True,
            summary=f"escalated to human: {reason_code} ({urgency.value})",
            output=escalation,
        )

    def _urgency(self, severity: Severity, revenue_at_risk: int) -> Severity:
        if severity is Severity.CRITICAL or revenue_at_risk >= 10_00_000 * 100:
            return Severity.CRITICAL
        if severity is Severity.HIGH or revenue_at_risk >= 2_00_000 * 100:
            return Severity.HIGH
        return severity

    def _notify(self, ctx: IncidentContext, escalation: Escalation, risk: int) -> None:
        """Notify and ticket.

        Both are self-authorised here rather than routed through the gateway: they have no effect
        on payment behaviour, and making a human handover contingent on policy approval could leave
        an unresolved incident with nobody watching it.
        """
        body = (
            f"{escalation.reason}\n\n"
            f"Diagnosis: {escalation.context_pack.get('diagnosis') or 'undetermined'}\n"
            f"Revenue at risk: {format_inr(risk)}/hour\n"
            f"Recommended action: {escalation.recommended_human_action}"
        )
        for action, arguments in (
            (
                ActionType.NOTIFY_MERCHANT,
                {
                    "subject": f"[{escalation.urgency.value}] {ctx.incident.incident_id} needs human review",
                    "body": body,
                    "urgency": escalation.urgency.value.lower(),
                },
            ),
            (
                ActionType.CREATE_INCIDENT_TICKET,
                {
                    "title": f"{ctx.incident.incident_id}: {escalation.reason_code}",
                    "description": body,
                    "severity": escalation.urgency.value,
                },
            ),
        ):
            approval = PolicyDecision(
                incident_id=ctx.incident.incident_id,
                action=action,
                requested_parameters=arguments,
                granted_parameters=arguments,
                outcome=PolicyOutcome.APPROVE,
                approved=True,
                approved_by="policy_engine:escalation",
                reason="human handover has no effect on payment behaviour",
                decided_at=ctx.now,
            )
            ctx.registry.call(action.value, arguments, approval=approval)
