"""End-to-end incident lifecycles.

These run the real supervisor, real agents, real tools and the real policy gateway against the
simulator. They are the tests that would catch a regression in how the system behaves as a whole,
rather than in any one component.
"""

from __future__ import annotations

import pytest

from pic.engine import Engine, EngineConfig
from pic.schemas import ActionType, IncidentState, VerificationStatus

pytestmark = pytest.mark.slow


# A fixed simulated cost per agent step. Live, a step charges the wall time it really took, so a
# slower machine advances the simulated clock further and borderline assertions flip for reasons
# that have nothing to do with the code under test - which is exactly what happened when agent
# timeouts started running each step on a worker thread.
STEP_COST_S = 1.5


def _engine(seed: int = 7) -> Engine:
    engine = Engine(
        EngineConfig(seed=seed, reasoner="deterministic", step_cost_s=STEP_COST_S)
    )
    engine.warmup(45)
    return engine


def _run(scenario: str, seed: int = 7, approve: bool = True):
    engine = _engine(seed)
    engine.trigger(scenario)
    incident = engine.run_until_incident(max_ticks=40)
    assert incident is not None, f"{scenario} was never detected"
    if approve and incident.state is IncidentState.AWAITING_HUMAN_APPROVAL:
        engine.supervisor.approve(incident, approver="test_operator")
    return engine, incident


# --------------------------------------------------------------------------
# The autonomous happy path
# --------------------------------------------------------------------------


def test_psp_degradation_is_diagnosed_acted_on_and_verified():
    engine, incident = _run("SCN-UPI-PSP")

    assert incident.anomaly is not None
    assert incident.evidence is not None and incident.evidence.findings
    assert incident.impact is not None and incident.impact.calculation
    assert incident.root_cause is not None
    assert incident.root_cause.cause_id == "psp_degradation"

    assert incident.proposal is not None
    assert incident.proposal.action is ActionType.SHIFT_TRAFFIC
    assert incident.policy_decision is not None and incident.policy_decision.approved
    assert incident.action_result is not None and incident.action_result.success

    verification = incident.verification
    assert verification is not None
    assert verification.status in (
        VerificationStatus.RECOVERED,
        VerificationStatus.PARTIALLY_RECOVERED,
    )
    # The effect must be established against the concurrent control, not before/after.
    assert verification.control_used is True
    assert verification.treated_success_rate > verification.control_success_rate
    assert verification.p_value < 0.05

    assert incident.audit, "an executed action must leave an audit record"
    assert all(record.approved_by for record in incident.audit)


def test_impact_numbers_are_derived_and_never_bare():
    _, incident = _run("SCN-UPI-PSP")
    impact = incident.impact
    assert impact.revenue_at_risk_per_hour_paise > 0
    # Every figure must carry its arithmetic, or an operator cannot check it.
    assert len(impact.calculation) >= 4
    assert any("shortfall" in line for line in impact.calculation)
    assert impact.assumptions


def test_cited_evidence_always_exists(monkeypatch):
    """The 'never invent evidence' claim, checked rather than asserted."""
    _, incident = _run("SCN-UPI-PSP")
    valid = incident.evidence.finding_ids()
    for hypothesis in incident.root_cause.hypotheses:
        assert set(hypothesis.supporting_evidence).issubset(valid)


def test_revenue_protected_never_exceeds_revenue_at_risk():
    """An intervention cannot protect more than the incident ever threatened."""
    _, incident = _run("SCN-UPI-PSP")
    if incident.verification and incident.impact:
        assert (
            incident.verification.estimated_revenue_protected_per_hour_paise
            <= incident.impact.revenue_at_risk_per_hour_paise
        )


# --------------------------------------------------------------------------
# The failure path — the one that matters
# --------------------------------------------------------------------------


def test_failed_intervention_is_detected_reverted_and_escalated():
    """A reroute cannot fix a network-wide fault. The agent must notice and undo its own change."""
    engine, incident = _run("SCN-UPI-PSP-BADFALLBACK")

    assert incident.action_result is not None or incident.attempts >= 1
    verification = incident.verification
    assert verification is not None
    assert verification.status in (VerificationStatus.FAILED, VerificationStatus.REGRESSED)
    assert verification.control_used, "the failure must be established against the control"

    # The routing configuration must be back where it started.
    weights = engine.simulator.control.snapshot()["route_weights"]["upi"]
    assert abs(weights["route_A"] - 0.55) < 1e-6, weights
    assert abs(weights["route_B"] - 0.25) < 1e-6, weights

    rollbacks = [r for r in incident.audit if "rollback" in r.approved_by]
    assert rollbacks, "a failed intervention must be reverted"
    assert all(r.execution_result == "success" for r in rollbacks)

    assert incident.escalation is not None, "a failure the agent cannot fix must reach a human"


def test_a_human_is_told_when_the_agent_stops():
    engine, incident = _run("SCN-UPI-PSP-BADFALLBACK")
    assert engine.notifications(), "escalation must notify"
    assert engine.tickets(), "escalation must leave a ticket"


# --------------------------------------------------------------------------
# Knowing when not to act
# --------------------------------------------------------------------------


def test_traffic_mix_change_produces_no_intervention():
    """Nothing is broken, so nothing should be changed about payment routing."""
    engine = _engine()
    engine.trigger("SCN-TRAFFIC-MIX")
    incident = engine.run_until_incident(max_ticks=40)

    before = Engine(EngineConfig(seed=7)).simulator.control.snapshot()["route_weights"]
    after = engine.simulator.control.snapshot()["route_weights"]
    assert after == before, "healthy traffic must not be rerouted"

    if incident is not None:
        assert incident.proposal is None or incident.proposal.action in (
            ActionType.NO_ACTION,
            ActionType.CREATE_INCIDENT_TICKET,
            ActionType.SET_MONITORING_FREQUENCY,
            ActionType.NOTIFY_MERCHANT,
        )


def test_issuer_outage_is_not_answered_with_rerouting():
    """No routing change can fix a bank declining its own cards; the agent must not pretend."""
    _, incident = _run("SCN-ISSUER", approve=False)
    assert incident.root_cause is not None
    if incident.proposal is not None:
        assert incident.proposal.action is not ActionType.SHIFT_TRAFFIC


# --------------------------------------------------------------------------
# Supervisor invariants
# --------------------------------------------------------------------------


def test_execution_never_happens_without_approval():
    for scenario in ("SCN-UPI-PSP", "SCN-ANDROID", "SCN-GW-LATENCY"):
        _, incident = _run(scenario, approve=False)
        if incident.action_result is not None and incident.action_result.executed:
            assert incident.policy_decision is not None
            assert incident.policy_decision.approved, scenario


def test_repeat_detections_are_correlated_into_one_incident():
    """One fault must not become an incident storm."""
    engine = _engine()
    engine.trigger("SCN-UPI-PSP")
    for _ in range(30):
        engine.advance(30)
        incident = engine.supervisor.observe()
        if incident is not None:
            engine.supervisor.run_incident(incident)

    assert len(engine.incidents()) <= 2, [i.incident_id for i in engine.incidents()]
    assert engine.incidents()[0].correlated_detections > 3


def test_every_step_is_recorded_with_its_tool_calls():
    _, incident = _run("SCN-UPI-PSP")
    assert len(incident.steps) >= 6
    investigation = next(s for s in incident.steps if s.agent == "investigation")
    assert investigation.tool_calls, "investigation must show which tools produced its evidence"
    assert all(call.latency_ms >= 0 for call in investigation.tool_calls)
    assert all(step.summary for step in incident.steps)


def test_agent_failure_degrades_to_escalation_not_a_crash(monkeypatch):
    """A crashed agent must hand over to a human, never abort the incident silently."""
    from pic.agents.investigation import InvestigationAgent

    def boom(self, ctx):
        raise RuntimeError("simulated tool outage")

    monkeypatch.setattr(InvestigationAgent, "run", boom)

    engine = _engine()
    engine.trigger("SCN-UPI-PSP")
    incident = engine.run_until_incident(max_ticks=40)

    assert incident is not None
    assert incident.escalation is not None
    assert incident.state in (IncidentState.CLOSED, IncidentState.LEARNING)
    failed = [s for s in incident.steps if not s.ok]
    assert failed and "simulated tool outage" in (failed[0].error or "")


def test_a_fault_nested_inside_the_primary_segment_is_not_filed_as_its_echo():
    """A genuine issuer outage must survive the independence test that its own method fails it.

    Echoes are separated from real second faults by removing the primary segment's traffic and
    re-measuring. That question is unanswerable when the candidate lives *inside* the primary:
    `issuer=HDFC` issues cards, so excluding `payment_method=card` leaves it holding only its
    healthy non-card volume. The forward test then reads "HDFC recovers once cards are gone" and
    files the cause as an echo of the very method it is degrading - after which the issuer
    hypothesis scores zero and the agent confidently blames the aggregate instead.

    The seed is chosen for its shape, not its answer: the primary segment has to be the bare
    method rather than the `card & HDFC` slice, because that is what makes the exclusion destroy
    the evidence. Where the detector already picks the compound slice the reversed test is never
    reached and the case proves nothing.
    """
    engine, incident = _run("SCN-ISSUER", seed=13)
    bundle = incident.evidence
    assert bundle is not None
    assert bundle.primary_segment == {"payment_method": "card"}, (
        "seed no longer reproduces the shape this regression guards"
    )

    issuer = next(f for f in bundle.findings if f.dimension == "issuer")
    assert issuer.metrics["independent"] is True, "the real cause was discarded as an echo"
    # The reversed test is what saved it: excluding the issuer, the method recovers.
    assert issuer.metrics.get("primary_residual_deviation") is not None, (
        "the reversed independence test never ran, so this passed for the wrong reason"
    )
    assert issuer.metrics["primary_residual_deviation"] > -0.08

    assert incident.root_cause is not None
    assert incident.root_cause.cause_id == "issuer_degradation"


def test_a_method_wide_failure_is_not_blamed_on_its_largest_provider():
    """The biggest payment method cannot look concentrated, even when it is entirely at fault.

    SCN-UPI-PSP-BADFALLBACK degrades every UPI PSP at once - an upstream rails problem, not a
    provider problem. Concentration is failure share over traffic share, so the largest method on
    the platform tops out near 1.3x even when it is wholly responsible. The whole-method hypothesis
    required 1.4x and therefore scored exactly 0.0 on the one scenario built to test it, leaving a
    single PSP to win on error-code evidence alone with no concentration evidence behind it - and
    the resulting fix is a reroute that cannot possibly help, because every route is degraded.

    The bar now matches the `strong_dimensions` test in the same module, which is set low for
    precisely this reason.
    """
    engine, incident = _run("SCN-UPI-PSP-BADFALLBACK", seed=7)
    assert incident.root_cause is not None
    assert incident.root_cause.cause_id == "payment_method_degradation"

    # It works because every provider inside the method is judged an echo of the method itself:
    # excluding UPI leaves each PSP healthy, so no single provider is the better explanation.
    psp_findings = [f for f in incident.evidence.findings if f.dimension == "psp"]
    assert psp_findings, "expected PSP evidence to exist and be considered"
    assert all(f.metrics.get("independent") is False for f in psp_findings)
