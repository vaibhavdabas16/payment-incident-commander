"""Escalation Agent.

Knowing when *not* to act autonomously is a feature, not a fallback. This agent packages everything
a human needs to take over in one place — what was seen, what was concluded, what was proposed, why
the system stopped, and what it recommends — so the handover does not cost the operator ten minutes
of reconstruction during an outage.

Escalation is never silent: it always notifies, and always leaves a ticket.
"""

from __future__ import annotations

from typing import Any

from ..schemas import (
    AgentResult,
    Escalation,
    IncidentState,
    NextStep,
    NON_REMEDIAL_ACTIONS,
    PolicyDecision,
    PolicyOutcome,
    ActionType,
    Severity,
    VerificationStatus,
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


def _pct(value: float | None) -> str:
    return "unknown" if value is None else f"{value * 100:.1f}%"


def _readable(action: ActionType | None) -> str:
    return action.value.replace("_", " ") if action is not None else "no action"


def _failed_step(incident: Any) -> Any:
    """The step that broke, so a failure can name itself instead of saying 'an agent'."""
    return next((s for s in reversed(incident.steps or []) if not s.ok), None)


def _because(incident: Any, reason_code: str) -> str:
    """State this incident's case for stopping, in facts the operator can act on.

    Reads only what the pipeline already recorded. Where a fact is missing the sentence gets
    shorter, never invented - a handover is exactly the moment a fabricated detail does damage.
    """
    rc = incident.root_cause
    ver = incident.verification
    pol = incident.policy_decision
    prop = incident.proposal
    diagnosis = rc.most_likely_root_cause if rc else None
    acted = _readable(prop.action if prop else None)

    if reason_code == "policy_denied" and pol is not None:
        return (
            f"The agent proposed {acted} and merchant policy refused it: {pol.reason}. "
            f"Nothing was executed."
        )

    if reason_code == "policy_requires_approval" and pol is not None:
        return (
            f"{acted.capitalize()} is ready to run, but {pol.reason}. "
            f"It executes only if you approve it."
        )

    if reason_code == "intervention_regressed" and ver is not None:
        contrast = (
            f"treated traffic fell to {_pct(ver.treated_success_rate)} while the control group "
            f"held at {_pct(ver.control_success_rate)}"
            if ver.control_used
            else f"success rate fell from {_pct(ver.before_success_rate)} to "
            f"{_pct(ver.after_success_rate)}"
        )
        return (
            f"{acted.capitalize()} made things worse: {contrast}. The change has been reverted, "
            f"so payments are back on the configuration you started with."
        )

    if reason_code in {"intervention_failed", "attempts_exhausted"} and ver is not None:
        if ver.status is VerificationStatus.INCONCLUSIVE:
            return (
                f"{acted.capitalize()} ran, but the result cannot be called: "
                f"{ver.treated_sample or ver.after_sample} treated payments against "
                f"{ver.control_sample or ver.before_sample} control (p={ver.p_value:.2f}). "
                f"Deciding needs more traffic than the incident has produced."
            )
        measured = (
            f"{_pct(ver.treated_success_rate)} on treated traffic against "
            f"{_pct(ver.control_success_rate)} on a control group left alone"
            if ver.control_used
            else f"{_pct(ver.after_success_rate)} after, against "
            f"{_pct(ver.before_success_rate)} before"
        )
        tail = (
            f"After {incident.attempts} attempts the agent stopped rather than keep changing a "
            f"live payment system."
            if reason_code == "attempts_exhausted"
            else "The change has been reverted."
        )
        return (
            f"{acted.capitalize()} did not help: {measured} (p={ver.p_value:.2f}). {tail} The "
            f"diagnosis may be wrong, or the fault may sit outside what routing can reach."
        )

    if reason_code == "intervention_failed":
        step = _failed_step(incident)
        detail = (step.error or step.summary) if step else "the tool call did not complete"
        return f"{acted.capitalize()} was authorised but could not be executed: {detail}."

    if reason_code == "rollback_failed":
        return (
            f"{acted.capitalize()} was applied and then could not be undone automatically. "
            f"Payment routing is still in the changed state, which is why this is urgent: the "
            f"system is not in a configuration anyone chose."
        )

    if reason_code == "no_effective_action":
        if prop is not None and incident.action_result is not None:
            return (
                f"The most useful thing available was to {acted}, which does not change how "
                f"payments route. {diagnosis or 'The fault'} needs a fix this system cannot apply."
            )
        if rc is None:
            step = _failed_step(incident)
            if step is not None:
                return (
                    f"The {step.agent.replace('_', ' ')} agent did not produce a diagnosis: "
                    f"{step.error or step.summary or 'no detail recorded'}. Acting without one "
                    f"would mean guessing which segment to change."
                )
            return (
                "No hypothesis scored above zero against the evidence gathered, so acting would "
                "mean guessing which of several segments to change."
            )
        return (
            f"The diagnosis is {diagnosis} at {_pct(rc.confidence)} confidence, but none of the "
            f"available tools change it - the failure is not somewhere traffic can be moved "
            f"away from."
        )

    if reason_code == "agent_failure":
        step = _failed_step(incident)
        if step is not None:
            return (
                f"The {step.agent.replace('_', ' ')} agent failed: "
                f"{step.error or step.summary or 'no detail recorded'}. Every stage after it reads "
                f"its output, so the workflow stopped rather than act on a partial picture."
            )
        return "An agent failed before producing output, so the workflow stopped without acting."

    if reason_code == "ambiguous_diagnosis" and rc is not None and len(rc.hypotheses) >= 2:
        first, second = rc.hypotheses[0], rc.hypotheses[1]
        return (
            f"Two explanations survived the evidence - {first.cause} at "
            f"{_pct(first.probability)} and {second.cause} at {_pct(second.probability)}. "
            f"Fixing one would not fix the other, and both may be real."
        )

    if reason_code == "low_confidence" and rc is not None:
        return (
            f"The best explanation is {diagnosis}, but only at {_pct(rc.confidence)} confidence - "
            f"below the bar for changing a live payment system without a human."
        )

    if reason_code == "irreversible_action" and prop is not None:
        return (
            f"The only action that would help is {acted}, and it cannot be undone automatically. "
            f"An action the agent cannot reverse is an action a human authorises."
        )

    return ""


# Policy rules are identified as `family:name`. These families say an action is unsafe or
# forbidden rather than that the agent is unsure about it, so no human authority makes them
# overridable: the destination is unhealthy or unapproved (routing), the merchant does not permit
# the action at all (capability), or the system has already tried and must stand down (rate_limit).
#
# Everything else - confidence, expected value, risk appetite, "this needs a person" - is a
# statement of uncertainty, and supplying that judgement is exactly what a human is for.
NON_OVERRIDABLE_RULE_FAMILIES = {"routing", "capability", "rate_limit"}


def _rule_family(rule: str) -> str:
    return rule.split(":", 1)[0]


def is_overridable(bound_by: list[str]) -> bool:
    """Whether a human may proceed past the rules that bound this decision."""
    return not any(_rule_family(rule) in NON_OVERRIDABLE_RULE_FAMILIES for rule in bound_by)


def _refused_proposal(incident: Any) -> bool:
    """Whether policy *refused* a concrete action, as opposed to holding it for a human.

    Only a refusal is overridable. A hold is the gateway doing its job, and the answer to it is
    approve or reject.
    """
    decision = incident.policy_decision
    if incident.proposal is None or decision is None or decision.approved:
        return False
    if decision.requires_human:
        return False
    return is_overridable(decision.bound_by)


def _next_steps(incident: Any, reason_code: str) -> list[NextStep]:
    """The moves available on this incident, derived from what actually happened to it."""
    steps: list[NextStep] = []
    proposal = incident.proposal
    decision = incident.policy_decision

    if decision is not None and decision.requires_human and proposal is not None:
        # The gateway is waiting on a person, which is the designed path rather than a problem to
        # be worked around. Both answers are offered, because "no" is a decision too.
        steps.append(
            NextStep(
                action="approve",
                label=f"Approve {_readable(proposal.action)}",
                detail=f"Policy asked for a person: {decision.reason}.",
                consequence=(
                    "Runs it, then measures the result against a control group and reverts it if "
                    "the numbers do not improve."
                ),
                destructive=True,
            )
        )
        steps.append(
            NextStep(
                action="reject",
                label="Reject it",
                detail="Nothing runs. The incident stays on the record as declined by a human.",
                consequence="Payments are left exactly as they are.",
            )
        )

    if _refused_proposal(incident):
        steps.append(
            NextStep(
                action="override",
                label=f"Run {_readable(proposal.action)} anyway",
                detail=(
                    f"Policy stopped this: {decision.reason}. Overriding records the decision "
                    f"against your name."
                ),
                consequence=(
                    "It still measures the result against a control group afterwards, and still "
                    "reverts itself if the numbers do not improve."
                ),
                destructive=True,
            )
        )

    for alternative in (proposal.alternatives_considered if proposal else [])[:6]:
        name = str(alternative.get("action") or "").strip()
        if not name or name == (proposal.action.value if proposal else None):
            continue
        # Filing a ticket or watching more closely is not an alternative remedy, and neither is
        # doing nothing. An option that cannot move the success rate is not worth a button.
        try:
            if ActionType(name) in NON_REMEDIAL_ACTIONS:
                continue
        except ValueError:
            continue
        if int(alternative.get("expected_value_paise") or 0) <= 0:
            continue
        steps.append(
            NextStep(
                action=f"run_alternative:{name}",
                label=f"Try {name.replace('_', ' ')} instead",
                detail=(
                    f"Costed at {format_inr(int(alternative.get('expected_value_paise') or 0))} "
                    f"expected value and not chosen."
                ),
                consequence="Goes through the policy gateway like any other action.",
                destructive=True,
            )
        )

    if reason_code in {
        "intervention_failed",
        "intervention_regressed",
        "attempts_exhausted",
        "agent_failure",
        "no_effective_action",
    }:
        steps.append(
            NextStep(
                action="retry",
                label="Look again now",
                detail=(
                    "Re-runs detection and diagnosis against the traffic since this incident "
                    "opened. Worth doing when the picture has changed - a fault that was "
                    "ambiguous ten minutes ago is often obvious now."
                ),
                consequence="Read-only until it reaches a decision, which the gateway judges afresh.",
            )
        )

    if reason_code == "rollback_failed":
        steps.append(
            NextStep(
                action="retry_rollback",
                label="Try the revert again",
                detail=(
                    "Payment routing is still in the changed state. This re-sends the recorded "
                    "inverse of what was applied."
                ),
                consequence="If it fails again, the configuration must be restored by hand.",
                destructive=True,
            )
        )

    steps.append(
        NextStep(
            action="acknowledge",
            label="I have this",
            detail=(
                "Records that a person took it, with a note, and takes it off the board. Use it "
                "when the fix belongs somewhere else - a provider, a deploy, another team."
            ),
            consequence="Changes nothing about payments. The incident stays in the audit trail.",
        )
    )
    return steps


class EscalationAgent(Agent):
    name = "escalation"
    state = IncidentState.ESCALATED

    def run(self, ctx: IncidentContext) -> AgentResult:
        incident = ctx.incident
        reason_code = ctx.scratch.get("escalation_reason", "agent_failure")
        reason = REASONS.get(reason_code, reason_code)
        because = _because(incident, reason_code)
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
            because=because,
            urgency=urgency,
            recommended_human_action=recommendation,
            next_steps=_next_steps(incident, reason_code),
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
            f"{escalation.because or escalation.reason}\n\n"
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
