"""The explainable decision trace.

One flat, ordered list of stages from the first observation to what the system learned, each
carrying the structured evidence that produced it. It exists so a person can click through an
incident and check every claim against the thing that made it, rather than reading a summary and
deciding whether to believe it.

The rule this file follows: **a stage may only state what the incident actually recorded.** Where
a stage did not happen, it says so and says why; it never fills the gap with a plausible sentence.
That is why every `facts` entry is a value copied from a typed agent output and every `evidence`
entry is an id, a tool name or a recorded computation.

Nothing is computed here. If a number is not already on the incident, it does not appear.
"""

from __future__ import annotations

from typing import Any

from ..schemas import IncidentRecord


def build_trace(incident: IncidentRecord) -> dict[str, Any]:
    """OBSERVATION → EVIDENCE → HYPOTHESIS → HISTORY → STRATEGIES → DECISION → POLICY →
    ACTION → CONTROL → RESULT → REVENUE → RECOVERY → LEARNING → PREVENTION."""
    stages: list[dict[str, Any]] = []

    def stage(
        key: str,
        title: str,
        *,
        reached: bool,
        headline: str,
        facts: list[tuple] | None = None,
        evidence: list[str] | None = None,
        detail: Any = None,
        tone: str = "neutral",
    ) -> None:
        """Record one stage. A fact is `(label, value)` or `(label, value, unit)`.

        The unit travels with the value rather than being inferred from the label downstream: a
        renderer guessing that "protected" means rupees and "protects per hour" does not is a
        renderer that will eventually print paise as a count.
        """
        stages.append(
            {
                "key": key,
                "title": title,
                "reached": reached,
                "headline": headline,
                "facts": [
                    {"label": f[0], "value": f[1], "unit": (f[2] if len(f) > 2 else None)}
                    for f in (facts or [])
                ],
                "evidence": evidence or [],
                "detail": detail,
                "tone": tone,
            }
        )

    a = incident.anomaly
    stage(
        "observation",
        "Observation",
        reached=a is not None,
        headline=(
            f"Payment success rate {a.current_value:.1%} against a baseline of {a.baseline:.1%}."
            if a
            else "No detection signal recorded."
        ),
        facts=[
            ("deviation", round(a.deviation, 4), "ratio"),
            ("z score", a.z_score),
            ("detection confidence", a.confidence),
            ("payments in window", a.sample_size),
            ("severity", a.severity.value),
        ]
        if a
        else [],
        evidence=list(a.detection_method) if a else [],
        tone="alert" if a else "neutral",
    )

    e = incident.evidence
    stage(
        "evidence",
        "Evidence",
        reached=e is not None,
        headline=(
            f"{len(e.findings)} findings from {len(e.tools_used)} read-only tools; failures "
            f"dominated by {e.dominant_error_code or 'no single code'} "
            f"({e.dominant_error_share:.0%})."
            if e
            else "Investigation did not complete."
        ),
        facts=[
            ("dominant error", e.dominant_error_code),
            ("share of failures", round(e.dominant_error_share, 4)),
            ("p95 latency shift", f"{e.latency_shift_ms:.0f}ms"),
            ("config changes found", len(e.recent_config_changes)),
            ("tools degraded", e.degraded),
        ]
        if e
        else [],
        evidence=[f.finding_id for f in e.findings] if e else [],
        detail=[
            {
                "id": f.finding_id,
                "tool": f.source_tool,
                "dimension": f.dimension,
                "statement": f.statement,
                "strength": f.strength,
            }
            for f in (e.findings if e else [])
        ],
    )

    rc = incident.root_cause
    stage(
        "hypothesis",
        "Hypothesis",
        reached=rc is not None,
        headline=(
            f"{rc.most_likely_root_cause} at {rc.confidence:.0%} confidence"
            + (", flagged ambiguous." if rc.ambiguous else ".")
            if rc
            else "No cause was established."
        ),
        facts=[("reasoner", rc.reasoner), ("ambiguous", rc.ambiguous)] if rc else [],
        evidence=(rc.supporting_evidence if rc else []),
        detail=[
            {
                "cause": h.cause,
                "cause_id": h.cause_id,
                "probability": h.probability,
                "deterministic_score": h.deterministic_score,
                # How far memory moved this hypothesis, shown alongside the score it moved, so
                # the contribution of history is visible rather than baked in.
                "memory_adjustment": h.memory_adjustment,
                "supporting_evidence": h.supporting_evidence,
                "contradicting_evidence": h.contradicting_evidence,
            }
            for h in (rc.hypotheses if rc else [])
        ],
    )

    plan = incident.recovery_plan
    stage(
        "history",
        "Historical evidence",
        reached=plan is not None,
        headline=(
            plan.historical_recommendation
            if plan
            else "The recovery-planning stage was not reached."
        ),
        facts=[
            ("incidents in memory", plan.memory_size),
            ("comparable incidents", len(plan.similar_incidents)),
            ("failure signature", plan.signature.label()),
            ("merchant risk tolerance", plan.profile_applied.get("risk_tolerance")),
            # Read off the options that exist, not off what the merchant profile asked for. The
            # two differ whenever the verifiable-shift ceiling binds, and a label saying "priced"
            # must describe what was priced.
            (
                "magnitudes priced",
                sorted({s.magnitude for s in plan.strategies if s.magnitude is not None}),
            ),
        ]
        if plan
        else [],
        evidence=[m["incident_id"] for m in plan.similar_incidents] if plan else [],
        detail=plan.similar_incidents if plan else [],
    )

    stage(
        "strategies",
        "Candidate strategies",
        reached=bool(plan and plan.strategies),
        headline=(
            f"{len(plan.strategies)} options priced, best first."
            if plan and plan.strategies
            else "No option set was produced."
        ),
        detail=[
            {
                "strategy_id": st.strategy_id,
                "action": st.action.value,
                "target": st.target,
                "magnitude": st.magnitude,
                "expected_revenue_protected_per_hour_paise": (
                    st.expected_revenue_protected_per_hour_paise
                ),
                "expected_value_paise": st.expected_value_paise,
                "confidence": st.confidence,
                "risk": st.risk_score,
                "p_success": st.p_success,
                "reasoning": st.reasoning,
                "supporting_findings": st.supporting_findings,
                "historical_support": (
                    st.historical_support.model_dump(mode="json")
                    if st.historical_support
                    else None
                ),
            }
            for st in sorted(
                (plan.strategies if plan else []),
                key=lambda x: x.expected_value_paise,
                reverse=True,
            )
        ],
    )

    p = incident.proposal
    stage(
        "decision",
        "Decision",
        reached=p is not None,
        headline=(
            f"{p.action.value.replace('_', ' ')} — {p.rationale}"
            if p
            else "No action was proposed."
        ),
        facts=[
            ("parameters", p.parameters),
            ("expected value", p.expected_value_paise, "paise"),
            ("protects per hour", p.expected_revenue_protected_per_hour_paise, "paise"),
            ("risk score", p.risk_score),
            ("reversible", p.reversible),
            ("proposed by", p.proposer),
        ]
        if p
        else [],
        detail=(p.alternatives_considered if p else []),
    )

    pd = incident.policy_decision
    stage(
        "policy",
        "Policy check",
        reached=pd is not None,
        headline=(pd.reason if pd else "The policy gateway was not reached."),
        facts=[
            ("outcome", pd.outcome.value),
            ("approved", pd.approved),
            ("needs a human", pd.requires_human),
            ("approved by", pd.approved_by),
            ("requested", pd.requested_parameters),
            ("granted", pd.granted_parameters),
        ]
        if pd
        else [],
        evidence=(pd.bound_by if pd else []),
        detail=(pd.evaluated_rules if pd else []),
        tone="warn" if pd and not pd.approved else "ok" if pd else "neutral",
    )

    ar = incident.action_result
    stage(
        "action",
        "Action",
        reached=ar is not None,
        headline=(
            f"{ar.action.value.replace('_', ' ')} executed via {ar.adapter}."
            if ar and ar.success
            else f"Execution failed: {ar.error}"
            if ar
            else "Nothing was executed."
        ),
        facts=[
            ("parameters", ar.parameters),
            ("reversible", ar.inverse_action is not None),
            ("inverse", ar.inverse_action),
        ]
        if ar
        else [],
        tone="ok" if ar and ar.success else "warn" if ar else "neutral",
    )

    v = incident.verification
    stage(
        "control",
        "Control group",
        reached=bool(v and v.control_used),
        headline=(
            f"Treated traffic {v.treated_success_rate:.1%} (n={v.treated_sample}) against control "
            f"{v.control_success_rate:.1%} (n={v.control_sample}), p={v.p_value}."
            if v and v.control_used
            else "No control group existed for this action, so the comparison is before/after."
            if v
            else "Verification was not reached."
        ),
        facts=[
            ("difference", round((v.treated_success_rate or 0) - (v.control_success_rate or 0), 4)),
            ("statistically significant", v.statistically_significant),
        ]
        if v and v.control_used
        else ([("before", v.before_success_rate), ("after", v.after_success_rate)] if v else []),
    )

    stage(
        "result",
        "Result",
        reached=v is not None,
        headline=(v.explanation if v else "The intervention was never measured."),
        facts=[
            ("status", v.status.value),
            ("gap to baseline closed", v.recovery_ratio, "ratio"),
            ("caused harm", v.caused_harm),
            ("rollback recommended", v.rollback_recommended),
            ("side effects", v.side_effects),
        ]
        if v
        else [],
        tone=(
            "ok"
            if v and v.status.value in ("RECOVERED", "PARTIALLY_RECOVERED")
            else "warn"
            if v
            else "neutral"
        ),
    )

    rev = incident.revenue
    stage(
        "revenue",
        "Revenue outcome",
        reached=rev is not None and rev.measurable,
        headline=(
            f"{rev.recovery_rate:.0%} of the revenue at risk was protected or recovered."
            if rev and rev.measurable
            else "This incident was never priced, so there is no revenue figure."
        ),
        facts=[
            ("at risk", rev.revenue_at_risk_paise, "paise"),
            ("protected", rev.revenue_protected_paise, "paise"),
            ("recovered", rev.revenue_recovered_paise, "paise"),
            ("lost", rev.revenue_lost_paise, "paise"),
            ("recovery rate", rev.recovery_rate, "ratio"),
        ]
        if rev
        else [],
        # The arithmetic, line by line. A revenue number an operator cannot check is a number
        # they will not act on.
        detail=(rev.calculation if rev else []),
        tone="ok" if rev and rev.recovery_rate >= 0.5 else "neutral",
    )

    orec = incident.order_recovery
    stage(
        "order_recovery",
        "Failed-payment recovery",
        reached=orec is not None,
        headline=(orec.note if orec else "The order-recovery stage was not reached."),
        facts=[
            ("payments that failed", orec.failed_payments),
            ("still recoverable", orec.recoverable_payments),
            ("attempted", orec.attempted),
            ("recovered", orec.recovered),
            ("value recovered", orec.recovered_value_paise, "paise"),
            ("by method", orec.by_method),
        ]
        if orec
        else [],
        tone="ok" if orec and orec.recovered else "neutral",
    )

    learning = incident.learning
    stage(
        "learning",
        "Learning",
        reached=learning is not None,
        headline=(
            f"Recorded: {learning.actual_action_executed.replace('_', ' ')}"
            + (
                f" at {learning.intervention_magnitude:g}%"
                if learning.intervention_magnitude
                else ""
            )
            + f" → {learning.verification_result}. Future incidents matching "
            f"'{learning.failure_signature.label()}' will weigh this."
            if learning
            else "Nothing was written to memory."
        ),
        facts=[
            ("signature", learning.failure_signature.label()),
            ("succeeded", learning.succeeded()),
            ("helped", learning.helped()),
            ("rollback", learning.rollback_result),
            ("human involved", learning.human_intervention_required),
            ("final resolution", learning.final_resolution),
        ]
        if learning
        else [],
        detail=(learning.model_dump(mode="json") if learning else None),
    )

    return {
        "incident_id": incident.incident_id,
        "state": incident.state.value,
        "outcome": incident.outcome,
        "title": incident.title,
        "stages": stages,
    }
