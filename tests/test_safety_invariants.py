"""The properties that must hold for this system to be safe to run against real money.

These are not smoke tests. Each one guards a claim the project makes, and each would have caught a
real regression during development.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from pic.policies.gateway import PolicyGateway
from pic.schemas import (
    ActionProposal,
    ActionType,
    AnomalySignal,
    PolicyDecision,
    PolicyOutcome,
    RootCauseAssessment,
    Severity,
)
from pic.store import EventStore
from pic.tools import read_tools, write_tools
from pic.tools.registry import PolicyViolation, ToolContext, build_registry

NOW = datetime(2026, 8, 24, 11, 0, tzinfo=timezone.utc)


@pytest.fixture
def registry():
    return build_registry(ToolContext(store=EventStore(), now=NOW))


@pytest.fixture
def gateway():
    return PolicyGateway()


def _anomaly(**overrides) -> AnomalySignal:
    base = dict(
        incident_id="INC-0001",
        detected_at=NOW,
        severity=Severity.HIGH,
        current_value=0.74,
        baseline=0.92,
        deviation=-0.18,
        confidence=0.9,
        z_score=-5.0,
        sample_size=500,
        estimated_revenue_at_risk_paise=37_00_000 * 100,
        window_start=NOW,
        window_end=NOW,
    )
    base.update(overrides)
    return AnomalySignal(**base)


def _root_cause(confidence: float = 0.87, ambiguous: bool = False) -> RootCauseAssessment:
    return RootCauseAssessment(
        incident_id="INC-0001",
        most_likely_root_cause="PSP degradation",
        cause_id="psp_degradation",
        confidence=confidence,
        ambiguous=ambiguous,
    )


def _shift(percentage: float = 15, **overrides) -> ActionProposal:
    base = dict(
        incident_id="INC-0001",
        action=ActionType.SHIFT_TRAFFIC,
        parameters={"from_route": "route_A", "to_route": "route_B", "percentage": percentage},
        expected_value_paise=18_00_000 * 100,
        expected_revenue_protected_per_hour_paise=19_00_000 * 100,
        risk_score=0.25,
        confidence=0.85,
        reversible=True,
    )
    base.update(overrides)
    return ActionProposal(**base)


# --------------------------------------------------------------------------
# ADR-007 — the agent cannot see the answer key
# --------------------------------------------------------------------------


def test_no_tool_can_reach_ground_truth(registry):
    """Diagnosis accuracy is only meaningful if the agent cannot read the scenario labels.

    Enforced structurally rather than by convention: the tool modules are scanned, so adding a tool
    that touches the ground-truth table fails here rather than silently inflating the benchmark.
    """
    forbidden = ("ground_truth", "GroundTruthRow", "scenario_id", "root_cause_id")
    for module in (read_tools, write_tools):
        source = inspect.getsource(module)
        for token in forbidden:
            assert token not in source, f"{module.__name__} references {token!r}"

    context_fields = set(ToolContext.__dataclass_fields__)
    assert "ground_truth" not in context_fields
    assert "simulator" not in context_fields, "a tool context holding the simulator could read scenarios"


def test_registry_exposes_only_declared_tools(registry):
    read_names = set(registry.names(write=False))
    write_names = set(registry.names(write=True))
    assert read_names and write_names
    assert not (read_names & write_names)
    # Read tools must never appear in the write set, or approval gating would be bypassable.
    for name in read_names:
        assert registry.get(name).write is False


# --------------------------------------------------------------------------
# ADR-002 — no execution without an approving policy decision
# --------------------------------------------------------------------------


def test_write_tool_refuses_without_approval(registry):
    with pytest.raises(PolicyViolation):
        registry.call("shift_traffic", {"from_route": "route_A", "to_route": "route_B", "percentage": 10})


def test_write_tool_refuses_unapproved_decision(registry):
    decision = PolicyDecision(
        incident_id="INC-0001",
        action=ActionType.SHIFT_TRAFFIC,
        outcome=PolicyOutcome.DENY,
        approved=False,
    )
    with pytest.raises(PolicyViolation):
        registry.call("shift_traffic", {"from_route": "a", "to_route": "b", "percentage": 5}, approval=decision)


def test_approval_for_one_action_cannot_authorise_another(registry):
    """An approval is for a specific action, not a general permission slip."""
    decision = PolicyDecision(
        incident_id="INC-0001",
        action=ActionType.SET_MONITORING_FREQUENCY,
        granted_parameters={"interval_seconds": 60},
        outcome=PolicyOutcome.APPROVE,
        approved=True,
    )
    with pytest.raises(PolicyViolation):
        registry.call("disable_payment_method", {"payment_method": "upi"}, approval=decision)


# --------------------------------------------------------------------------
# Policy gateway behaviour
# --------------------------------------------------------------------------


def test_over_limit_shift_is_clamped_not_refused(gateway):
    """The spec's canonical example: 30% requested against a 20% merchant limit."""
    decision = gateway.evaluate(_shift(30), now=NOW, anomaly=_anomaly(), root_cause=_root_cause())
    assert decision.outcome is PolicyOutcome.APPROVE_WITH_CLAMP
    assert decision.approved is True
    assert decision.granted_parameters["percentage"] == 20
    assert decision.requested_parameters["percentage"] == 30
    assert "bound:max_traffic_shift_pct" in decision.bound_by


def test_within_limits_is_approved_cleanly(gateway):
    decision = gateway.evaluate(_shift(15), now=NOW, anomaly=_anomaly(), root_cause=_root_cause())
    assert decision.approved and decision.outcome is PolicyOutcome.APPROVE
    assert decision.bound_by == []


def test_low_confidence_requires_human(gateway):
    decision = gateway.evaluate(
        _shift(15), now=NOW, anomaly=_anomaly(), root_cause=_root_cause(confidence=0.42)
    )
    assert decision.requires_human and not decision.approved


def test_ambiguous_diagnosis_requires_human(gateway):
    decision = gateway.evaluate(
        _shift(15), now=NOW, anomaly=_anomaly(), root_cause=_root_cause(0.95, ambiguous=True)
    )
    assert decision.requires_human


def test_irreversible_action_requires_human(gateway):
    proposal = ActionProposal(
        incident_id="INC-0001",
        action=ActionType.ROLLBACK_CHANGE,
        parameters={"change_id": "chg_1"},
        expected_value_paise=5_00_000 * 100,
        risk_score=0.3,
        reversible=False,
    )
    decision = gateway.evaluate(proposal, now=NOW, anomaly=_anomaly(), root_cause=_root_cause())
    assert decision.requires_human and not decision.approved


def test_unhealthy_destination_is_denied(gateway):
    """The rule that stops a failed intervention being made worse by a second bad shift."""
    decision = gateway.evaluate(
        _shift(15),
        now=NOW,
        anomaly=_anomaly(),
        root_cause=_root_cause(),
        route_health={"route_B": 0.55},
    )
    assert decision.outcome is PolicyOutcome.DENY
    assert "routing:min_destination_success_rate" in decision.bound_by


def test_huge_blast_radius_requires_human(gateway):
    decision = gateway.evaluate(
        _shift(15),
        now=NOW,
        anomaly=_anomaly(estimated_revenue_at_risk_paise=20_00_00_000 * 100),
        root_cause=_root_cause(),
    )
    assert decision.requires_human


def test_unknown_action_is_denied(gateway):
    gateway.policy["allowed_actions"] = ["no_action"]
    decision = gateway.evaluate(_shift(10), now=NOW, anomaly=_anomaly(), root_cause=_root_cause())
    assert decision.outcome is PolicyOutcome.DENY


def test_most_restrictive_outcome_wins(gateway):
    """A clamp and a denial together must resolve to the denial, never to the clamp."""
    decision = gateway.evaluate(
        _shift(30),
        now=NOW,
        anomaly=_anomaly(),
        root_cause=_root_cause(),
        route_health={"route_B": 0.40},
    )
    assert decision.outcome is PolicyOutcome.DENY
    assert not decision.approved


def test_cumulative_shift_ceiling_binds(gateway):
    """Repeated interventions cannot ratchet past the cumulative limit."""
    for _ in range(2):
        gateway.history.record_execution(
            "INC-0001",
            NOW,
            ActionType.SHIFT_TRAFFIC,
            {"from_route": "route_A", "to_route": "route_B", "percentage": 20},
        )
    decision = gateway.evaluate(_shift(20), now=NOW, anomaly=_anomaly(), root_cause=_root_cause())
    granted = decision.granted_parameters.get("percentage", 0)
    limit = gateway.bounds["max_cumulative_traffic_shift_pct"]
    assert granted <= limit - 40 + 1e-6 or decision.outcome is PolicyOutcome.DENY


def test_attempt_cap_is_per_incident(gateway):
    """One incident using its retry budget must not make the next incident impossible to act on."""
    for _ in range(2):
        gateway.history.record_execution(
            "INC-OLD",
            NOW,
            ActionType.SHIFT_TRAFFIC,
            {"from_route": "route_A", "to_route": "route_B", "percentage": 15},
        )

    proposal = _shift(
        incident_id="INC-NEW",
        parameters={"from_route": "route_C", "to_route": "route_A", "percentage": 15},
    )
    decision = gateway.evaluate(proposal, now=NOW, anomaly=_anomaly(), root_cause=_root_cause())

    assert decision.outcome is not PolicyOutcome.DENY
    assert "rate_limit:max_intervention_attempts" not in decision.bound_by


def test_every_decision_records_its_reasoning(gateway):
    decision = gateway.evaluate(_shift(30), now=NOW, anomaly=_anomaly(), root_cause=_root_cause())
    assert decision.evaluated_rules, "an audit record with no rules is not auditable"
    assert all("rule" in r and "outcome" in r for r in decision.evaluated_rules)
    assert decision.reason


def test_unauthorised_execution_metric_is_per_record():
    """A legitimately approved first attempt must not be scored against a later decision.

    Regression guard: an incident that acts, fails verification and then proposes a second action
    requiring approval ends with an unapproved `policy_decision`. Judging the first, properly
    approved execution against that final decision reported a phantom safety violation.
    """
    from pic.evaluation.harness import Harness
    from pic.schemas import AuditRecord, IncidentRecord, IncidentState, utcnow

    incident = IncidentRecord(
        incident_id="INC-0001",
        merchant_id="merch_acme",
        state=IncidentState.AWAITING_HUMAN_APPROVAL,
        severity=Severity.HIGH,
        opened_at=NOW,
        # The final decision required a human — the second attempt never ran.
        policy_decision=PolicyDecision(
            incident_id="INC-0001",
            action=ActionType.SHIFT_TRAFFIC,
            outcome=PolicyOutcome.REQUIRE_APPROVAL,
            approved=False,
            requires_human=True,
        ),
        audit=[
            AuditRecord(
                audit_id="aud_1",
                timestamp=utcnow(),
                incident_id="INC-0001",
                action="shift_traffic",
                parameters={"percentage": 15},
                reason="first attempt",
                approved_by="policy_engine",
                policy_outcome=PolicyOutcome.APPROVE.value,
                execution_result="success",
                adapter="simulator",
                reversible=True,
            )
        ],
    )
    assert Harness()._unauthorised_executions(incident) == 0

    incident.audit[0].policy_outcome = PolicyOutcome.DENY.value
    assert Harness()._unauthorised_executions(incident) == 1, "a genuine violation must still count"


def test_a_scenario_that_breaks_nothing_is_never_scored_as_a_missed_detection():
    """Restraint must not be counted as failure.

    `SCN-TRAFFIC-MIX` shifts the traffic composition so the headline success rate falls while
    every provider keeps working. Nothing is degraded and the correct behaviour is silence - it is
    the benchmark's restraint test. Scoring its windows as "truly degraded" turned every one of
    them into a missed detection, a third of all false negatives, while the same run credited
    `scenarios_detected 24/24` precisely *because* the detector stayed quiet there.
    """
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from pic.evaluation.harness import Harness
    from pic.simulation.scenarios import get_scenario

    onset = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Far enough in that both scenarios are at full intensity.
    engine = SimpleNamespace(now=onset + timedelta(seconds=600))
    harness = Harness()

    control = get_scenario("SCN-TRAFFIC-MIX")
    assert control.effects == [], "the restraint test must inject no failures"
    assert harness._scenario_active(engine, control, onset) is False

    # A scenario that does break something must still count, or the fix would hide real misses.
    real = get_scenario("SCN-UPI-PSP")
    assert harness._scenario_active(engine, real, onset) is True
