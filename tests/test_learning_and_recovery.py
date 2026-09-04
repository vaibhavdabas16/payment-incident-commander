"""The closed loop: memory, learning, revenue, prevention and the second recovery layer.

These tests are organised around the claims the product makes rather than around the modules that
implement them. The claim "the agent gets better because it remembers" is only worth making if
three things are true at once, and each has its own section below:

1. what it remembers is structured, scoped and retrievable (`Incident memory`);
2. remembering changes the next decision, in a bounded and visible way (`Learning`);
3. none of that can reach anything that keeps the system safe (`Safety under learning`).

The last section is the important one. A learning subsystem is the most natural place for a
guardrail to quietly stop applying, so the tests that matter most here are the ones asserting that
learning cannot execute, cannot authorise, cannot widen policy and cannot invent a past.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pic.agents.strategy import MAX_HISTORY_ADJUSTMENT, historical_support
from pic.engine import Engine, EngineConfig
from pic.memory.patterns import MIN_OCCURRENCES, find_patterns
from pic.memory.profile import build_profile
from pic.memory.store import IncidentMemory, signature_similarity
from pic.recovery.orders import HARD_DECLINES, RECOVERY_METHOD, find_recoverable_orders
from pic.revenue import compute_revenue_outcome
from pic.schemas import (
    ActionType,
    FailureSignature,
    IncidentOutcomeRecord,
    IncidentState,
    OrderRecoveryResult,
    PaymentEvent,
    Severity,
)

STEP_COST_S = 1.5


# --------------------------------------------------------------------------
# Fixtures and builders
# --------------------------------------------------------------------------


def _signature(**overrides) -> FailureSignature:
    base = dict(
        merchant_id="merch_acme",
        payment_method="upi",
        psp="psp_axis",
        route_id="route_A",
        dominant_error_code="PSP_UNAVAILABLE",
        root_cause_id="psp_degradation",
        severity=Severity.HIGH,
        degradation_magnitude=0.18,
        latency_shift_ms=1800.0,
        traffic_share=0.4,
        affected_segment_keys=["payment_method=upi&psp=psp_axis"],
        route_health={"route_A": 0.35, "route_B": 0.91},
    )
    base.update(overrides)
    return FailureSignature(**base)


def _record(
    incident_id: str,
    *,
    action: str = "shift_traffic",
    magnitude: float | None = 20.0,
    verification: str = "RECOVERED",
    rollback: bool = False,
    at_risk: int = 1_000_000,
    protected: int = 800_000,
    recovered: int = 0,
    minutes_ago: int = 30,
    signature: FailureSignature | None = None,
    merchant_id: str = "merch_acme",
) -> IncidentOutcomeRecord:
    signature = signature or _signature(merchant_id=merchant_id)
    lost = max(0, at_risk - protected - recovered)
    return IncidentOutcomeRecord(
        incident_id=incident_id,
        timestamp=datetime(2026, 8, 24, 20, 5, tzinfo=timezone.utc)
        - timedelta(minutes=minutes_ago),
        merchant_id=merchant_id,
        failure_signature=signature,
        selected_root_cause="Payment service provider degradation",
        selected_root_cause_id=signature.root_cause_id,
        root_cause_confidence=0.9,
        revenue_at_risk_paise=at_risk,
        selected_action=action,
        actual_action_executed=action,
        executed_parameters={
            "from_route": "route_A",
            "to_route": "route_B",
            "percentage": magnitude,
        },
        intervention_magnitude=magnitude,
        policy_result="APPROVE",
        verification_result=verification,
        revenue_protected_paise=protected,
        revenue_recovered_paise=recovered,
        revenue_lost_paise=lost,
        recovery_rate=round((protected + recovered) / at_risk, 4) if at_risk else 0.0,
        rollback_required=rollback,
        rollback_result="SUCCEEDED" if rollback else "NOT_REQUIRED",
        time_to_recovery_s=252.0,
        final_resolution=verification,
    )


@pytest.fixture
def memory() -> IncidentMemory:
    return IncidentMemory(persist=False)


def _engine(seed: int = 7, memory: IncidentMemory | None = None) -> Engine:
    engine = Engine(
        EngineConfig(
            seed=seed, reasoner="deterministic", step_cost_s=STEP_COST_S, memory=memory
        )
    )
    engine.warmup(45)
    return engine


def _run(scenario: str, seed: int = 7, memory: IncidentMemory | None = None):
    engine = _engine(seed, memory)
    engine.trigger(scenario)
    incident = engine.run_until_incident(max_ticks=40)
    assert incident is not None, f"{scenario} was never detected"
    for _ in range(3):
        if incident.state is not IncidentState.AWAITING_HUMAN_APPROVAL:
            break
        engine.supervisor.approve(incident, approver="test_operator")
    return engine, incident


# --------------------------------------------------------------------------
# Incident memory
# --------------------------------------------------------------------------


def test_a_closed_incident_is_stored_and_can_be_retrieved(memory):
    stored = memory.record_incident(_record("INC-0001"))
    assert len(memory) == 1
    assert memory.get("INC-0001") is stored
    assert memory.get("INC-9999") is None


def test_similarity_retrieval_ranks_the_same_kind_of_failure_first(memory):
    memory.record_incident(_record("INC-0001"))
    memory.record_incident(
        _record(
            "INC-0002",
            signature=_signature(
                psp="psp_yes",
                route_id="route_B",
                dominant_error_code="ISSUER_DECLINE",
                root_cause_id="issuer_degradation",
                payment_method="card",
            ),
        )
    )
    matches = memory.find_similar_incidents(_signature())
    assert [m.record.incident_id for m in matches] == ["INC-0001"], (
        "a card issuer decline is not evidence about a UPI provider outage"
    )
    assert matches[0].similarity > 0.9


def test_a_different_root_cause_is_not_retrieved_as_comparable(memory):
    """The dimension that most changes the right action is the one weighted highest."""
    memory.record_incident(_record("INC-0001"))
    other = _signature(root_cause_id="traffic_mix_shift", dominant_error_code="USER_DROPPED")
    assert signature_similarity(_signature(), other) < 0.55


def test_historical_action_success_rate_separates_magnitudes(memory):
    for i in range(3):
        memory.record_incident(_record(f"INC-000{i + 1}", magnitude=20.0, verification="RECOVERED"))
    for i in range(2):
        memory.record_incident(
            _record(f"INC-001{i}", magnitude=50.0, verification="FAILED", protected=0)
        )

    good = memory.get_success_rate_for_action("shift_traffic", signature=_signature(), magnitude=20)
    bad = memory.get_success_rate_for_action("shift_traffic", signature=_signature(), magnitude=50)
    assert (good.attempts, good.successes) == (3, 3)
    assert (bad.attempts, bad.successes) == (2, 0)
    assert good.success_rate > bad.success_rate


def test_an_action_never_tried_is_distinguishable_from_one_that_failed(memory):
    memory.record_incident(_record("INC-0001", verification="FAILED", protected=0))
    never = memory.get_success_rate_for_action("disable_payment_method", signature=_signature())
    tried = memory.get_success_rate_for_action("shift_traffic", signature=_signature())
    assert never.attempts == 0, "never tried must not look like tried and failed"
    assert tried.attempts == 1 and tried.successes == 0


def test_memory_is_isolated_by_merchant(memory):
    memory.record_incident(_record("INC-0001", merchant_id="merch_acme"))
    memory.record_incident(
        _record(
            "INC-0002",
            merchant_id="merch_other",
            signature=_signature(merchant_id="merch_other"),
        )
    )
    mine = memory.find_similar_incidents(_signature(merchant_id="merch_acme"))
    assert [m.record.incident_id for m in mine] == ["INC-0001"]

    stats = memory.get_success_rate_for_action(
        "shift_traffic", signature=_signature(merchant_id="merch_other")
    )
    assert stats.incident_ids == ["INC-0002"]

    outcomes = memory.get_historical_recovery_outcomes(merchant_id="merch_acme")
    assert outcomes["incidents"] == 1


def test_a_record_can_be_amended_but_never_conjured(memory):
    memory.record_incident(_record("INC-0001"))
    updated = memory.update_incident("INC-0001", final_resolution="ACKNOWLEDGED_BY_HUMAN")
    assert updated is not None and updated.final_resolution == "ACKNOWLEDGED_BY_HUMAN"
    assert len(memory) == 1
    assert memory.update_incident("INC-NOPE", final_resolution="x") is None
    assert len(memory) == 1, "updating an unknown incident must not create one"


def test_recording_the_same_incident_twice_replaces_rather_than_duplicates(memory):
    memory.record_incident(_record("INC-0001", verification="FAILED", protected=0))
    memory.record_incident(_record("INC-0001", verification="RECOVERED"))
    assert len(memory) == 1
    assert memory.get("INC-0001").verification_result == "RECOVERED"


# --------------------------------------------------------------------------
# Learning
# --------------------------------------------------------------------------


def test_a_successful_action_becomes_positive_historical_evidence(memory):
    for i in range(4):
        memory.record_incident(_record(f"INC-000{i}", verification="RECOVERED"))
    support = historical_support(memory, _signature(), ActionType.SHIFT_TRAFFIC, 20.0)
    assert support.matched_incidents == 4
    assert support.efficacy_adjustment > 0, "a run of successes must raise the prior"
    assert support.stats is not None and support.stats.successes == 4


def test_a_failed_action_becomes_negative_historical_evidence(memory):
    for i in range(4):
        memory.record_incident(
            _record(f"INC-000{i}", verification="FAILED", protected=0, rollback=True)
        )
    support = historical_support(memory, _signature(), ActionType.SHIFT_TRAFFIC, 20.0)
    assert support.matched_incidents == 4
    assert support.efficacy_adjustment < 0, "a run of failures must lower the prior"
    assert support.stats is not None and support.stats.rollbacks == 4


def test_a_partial_recovery_is_evidence_the_action_helps_not_that_it_failed(memory):
    for i in range(4):
        memory.record_incident(_record(f"INC-000{i}", verification="PARTIALLY_RECOVERED"))
    support = historical_support(memory, _signature(), ActionType.SHIFT_TRAFFIC, 20.0)
    stats = support.stats
    assert stats is not None
    assert stats.successes == 0, "a partial recovery is not reported as a success"
    assert stats.helped == 4, "but it is evidence the intervention works"
    assert support.efficacy_adjustment > 0


def test_history_can_never_move_the_prior_further_than_its_cap(memory):
    for i in range(60):
        memory.record_incident(
            _record(f"INC-{i:04d}", verification="FAILED", protected=0, rollback=True)
        )
    support = historical_support(memory, _signature(), ActionType.SHIFT_TRAFFIC, 20.0)
    assert abs(support.efficacy_adjustment) <= MAX_HISTORY_ADJUSTMENT + 1e-9


def test_a_single_past_incident_barely_moves_the_prior(memory):
    """Shrinkage, not replacement: one outcome is an anecdote, not a policy."""
    memory.record_incident(_record("INC-0001", verification="FAILED", protected=0))
    one = historical_support(memory, _signature(), ActionType.SHIFT_TRAFFIC, 20.0)
    for i in range(2, 12):
        memory.record_incident(_record(f"INC-{i:04d}", verification="FAILED", protected=0))
    many = historical_support(memory, _signature(), ActionType.SHIFT_TRAFFIC, 20.0)
    assert abs(one.efficacy_adjustment) < abs(many.efficacy_adjustment)


def test_a_future_incident_is_decided_against_recorded_history():
    """The end-to-end claim: the second incident can see what happened to the first."""
    memory = IncidentMemory(persist=False)
    _, first = _run("SCN-UPI-PSP", seed=7, memory=memory)
    assert first.learning is not None, "a closed incident must write what it learned"
    assert len(memory) == 1

    _, second = _run("SCN-UPI-PSP", seed=20260824, memory=memory)
    plan = second.recovery_plan
    assert plan is not None
    assert plan.memory_size == 1
    assert plan.similar_incidents, "the comparable incident must be retrieved"
    assert plan.similar_incidents[0]["incident_id"] == first.incident_id
    assert "comparable" in plan.historical_recommendation.lower()

    shifts = [s for s in plan.strategies if s.action is ActionType.SHIFT_TRAFFIC]
    assert shifts, "a PSP outage must still offer a reroute"
    assert any(
        s.historical_support and s.historical_support.matched_incidents > 0 for s in shifts
    ), "the option set must carry the historical evidence it was priced with"


def test_incident_ids_stay_unique_across_worlds_sharing_one_memory():
    """Otherwise every world's first incident overwrites the last and nothing accumulates."""
    memory = IncidentMemory(persist=False)
    _, first = _run("SCN-UPI-PSP", seed=7, memory=memory)
    _, second = _run("SCN-UPI-PSP", seed=20260824, memory=memory)
    assert first.incident_id != second.incident_id
    assert len(memory) == 2


def test_a_failed_intervention_survives_its_own_rollback_in_memory():
    """The rollback clears `action_result`; the record must still say what was tried."""
    _, incident = _run("SCN-UPI-PSP-BADFALLBACK")
    learning = incident.learning
    assert learning is not None
    assert learning.actual_action_executed == "shift_traffic", (
        "an intervention that had to be undone is the most valuable record there is"
    )
    assert learning.intervention_magnitude is not None
    assert learning.rollback_required is True
    assert learning.helped() is False
    assert learning.succeeded() is False


def test_the_merchant_profile_is_derived_from_policy_and_outcomes(memory):
    for i in range(4):
        memory.record_incident(_record(f"INC-000{i}", magnitude=20.0, verification="RECOVERED"))
    conservative = build_profile(
        "merch_acme",
        policy={
            "bounds": {"max_traffic_shift_pct": 10},
            "thresholds": {"max_autonomous_risk_score": 0.3},
            "allowed_actions": ["shift_traffic", "rollback_change"],
            "autonomous_actions": ["shift_traffic"],
        },
        records=memory.all(),
    )
    assert conservative.risk_tolerance == "conservative"
    assert conservative.approval_required_for == ["rollback_change"]
    assert conservative.preferred_shift_pct == 20.0
    assert conservative.preferred_destinations == ["route_B"]

    aggressive = build_profile(
        "merch_acme",
        policy={
            "bounds": {"max_traffic_shift_pct": 40},
            "thresholds": {"max_autonomous_risk_score": 0.8},
        },
        records=memory.all(),
    )
    assert aggressive.risk_tolerance == "aggressive"
    # Same incident, different merchant policy, different option set.
    assert max(aggressive.preferred_magnitudes(15.0)) > max(
        conservative.preferred_magnitudes(15.0)
    )


def test_a_merchant_with_no_history_gets_a_profile_from_policy_alone():
    profile = build_profile("merch_new", policy={"bounds": {"max_traffic_shift_pct": 20}})
    assert profile.incidents_seen == 0
    assert profile.preferred_shift_pct is None
    assert profile.notes, "the profile must say that it is running on policy alone"


# --------------------------------------------------------------------------
# Revenue
# --------------------------------------------------------------------------


def test_revenue_at_risk_protected_and_lost_always_add_up():
    _, incident = _run("SCN-UPI-PSP")
    revenue = incident.revenue
    assert revenue is not None and revenue.measurable
    assert (
        revenue.revenue_protected_paise
        + revenue.revenue_recovered_paise
        + revenue.revenue_lost_paise
        == revenue.revenue_at_risk_paise
    ), "a rupee is being double counted or dropped"


def test_recovery_rate_is_the_ratio_it_claims_to_be():
    _, incident = _run("SCN-UPI-PSP")
    revenue = incident.revenue
    assert revenue is not None
    expected = (
        revenue.revenue_protected_paise + revenue.revenue_recovered_paise
    ) / revenue.revenue_at_risk_paise
    assert revenue.recovery_rate == pytest.approx(expected, abs=1e-4)
    assert 0.0 <= revenue.recovery_rate <= 1.0


def test_every_money_figure_is_an_integer_number_of_paise():
    _, incident = _run("SCN-UPI-PSP")
    revenue = incident.revenue
    assert revenue is not None
    for field in (
        "revenue_at_risk_paise",
        "revenue_protected_paise",
        "revenue_recovered_paise",
        "revenue_lost_paise",
    ):
        assert isinstance(getattr(revenue, field), int), f"{field} must be integer paise"


def test_protected_revenue_cannot_exceed_what_was_at_risk():
    """An intervention cannot save more than the incident ever threatened."""
    _, incident = _run("SCN-UPI-PSP")
    revenue = incident.revenue
    assert revenue is not None
    assert revenue.revenue_protected_paise <= revenue.revenue_at_risk_paise
    assert (
        revenue.revenue_protected_paise + revenue.revenue_recovered_paise
        <= revenue.revenue_at_risk_paise
    )


def test_an_unpriced_incident_reports_no_revenue_rather_than_a_guess():
    engine = _engine()
    engine.trigger("SCN-UPI-PSP")
    incident = engine.run_until_incident(max_ticks=40)
    assert incident is not None
    stripped = incident.model_copy(update={"impact": None, "revenue": None})
    outcome = compute_revenue_outcome(stripped)
    assert outcome.measurable is False
    assert outcome.revenue_at_risk_paise == 0
    assert outcome.recovery_rate == 0.0
    assert outcome.calculation, "it must say why there is no figure"


def test_the_arithmetic_behind_every_figure_is_shown():
    _, incident = _run("SCN-UPI-PSP")
    assert incident.revenue is not None
    joined = " ".join(incident.revenue.calculation)
    for word in ("Exposure", "Protected", "Lost", "Recovery rate"):
        assert word in joined


def test_portfolio_recovery_rate_is_recomputed_not_averaged():
    """Averaging per-incident percentages would let a tiny incident cancel a large one."""
    from pic.revenue import aggregate
    from pic.schemas import RevenueOutcome

    outcomes = [
        RevenueOutcome(
            incident_id="A", revenue_at_risk_paise=100, revenue_protected_paise=100, recovery_rate=1.0
        ),
        RevenueOutcome(
            incident_id="B",
            revenue_at_risk_paise=900,
            revenue_protected_paise=0,
            revenue_lost_paise=900,
            recovery_rate=0.0,
        ),
    ]
    totals = aggregate(outcomes)
    assert totals["recovery_rate"] == pytest.approx(0.1)


# --------------------------------------------------------------------------
# Verification still governs what counts as recovered
# --------------------------------------------------------------------------


def test_the_control_group_is_never_touched_by_the_intervention():
    """Traffic left on the source route is the control, and it must stay there."""
    engine, incident = _run("SCN-UPI-PSP")
    executed = [
        a for a in incident.audit if a.action == "shift_traffic" and "rollback" not in a.approved_by
    ]
    assert executed
    for record in executed:
        weights = engine.simulator.control.snapshot()["route_weights"]["upi"]
        source = record.parameters["from_route"]
        assert weights[source] > 0, (
            "the source route must keep traffic, or there is no concurrent control to measure "
            "the intervention against"
        )
    verification = incident.verification
    assert verification is not None and verification.control_used
    assert verification.control_sample > 0


def test_a_failed_intervention_is_rolled_back_and_learned_from():
    engine, incident = _run("SCN-UPI-PSP-BADFALLBACK")
    assert incident.verification is not None
    assert incident.verification.status.value in ("FAILED", "REGRESSED")
    rollbacks = [a for a in incident.audit if "rollback" in a.approved_by]
    assert rollbacks and all(a.execution_result == "success" for a in rollbacks)
    assert incident.learning is not None and incident.learning.rollback_result == "SUCCEEDED"
    assert incident.revenue is not None
    assert incident.revenue.revenue_protected_paise == 0, (
        "an intervention that was reverted protected nothing"
    )


def test_a_partial_recovery_stays_partial_in_the_record(memory):
    record = _record("INC-0001", verification="PARTIALLY_RECOVERED")
    memory.record_incident(record)
    assert record.succeeded() is False
    assert record.helped() is True
    stats = memory.get_success_rate_for_action("shift_traffic", signature=_signature())
    assert stats.partials == 1 and stats.successes == 0


def test_an_inconclusive_verification_is_never_upgraded(memory):
    record = _record("INC-0001", verification="INCONCLUSIVE", protected=0)
    memory.record_incident(record)
    assert record.helped() is False and record.succeeded() is False
    stats = memory.get_success_rate_for_action("shift_traffic", signature=_signature())
    assert stats.helped == 0 and stats.failures == 1


def test_a_reverted_intervention_never_counts_as_having_helped(memory):
    memory.record_incident(_record("INC-0001", verification="RECOVERED", rollback=True))
    stats = memory.get_success_rate_for_action("shift_traffic", signature=_signature())
    assert stats.helped == 0, "if it had to be undone, it did not work, whatever it looked like"


# --------------------------------------------------------------------------
# Order recovery — the second layer
# --------------------------------------------------------------------------


def _payment(order: str, *, status: str, code: str | None, when: datetime, amount: int = 50_000):
    return PaymentEvent(
        payment_id=f"pay_{order}_{status}",
        order_id=order,
        timestamp=when,
        merchant_id="merch_acme",
        amount_paise=amount,
        payment_method="upi",
        gateway="gw_primary",
        psp="psp_axis",
        issuer="HDFC",
        geography="MH",
        device="mobile",
        os="android",
        app_version="8.4.1",
        status=status,
        latency_ms=900,
        route_id="route_A",
        error_code=code,
    )


def test_hard_declines_are_never_offered_for_recovery():
    from pic.store import EventStore

    store = EventStore()
    start = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    store.add(_payment("o1", status="failed", code="PSP_UNAVAILABLE", when=start))
    for i, code in enumerate(sorted(HARD_DECLINES)):
        store.add(_payment(f"hard{i}", status="failed", code=code, when=start))

    orders, summary = find_recoverable_orders(store, start, start + timedelta(minutes=5))
    assert [o.order_id for o in orders] == ["o1"]
    assert summary["hard_declines"] == len(HARD_DECLINES)


def test_an_order_the_customer_completed_is_not_counted_as_recovered():
    """Otherwise the system takes credit for revenue it never touched."""
    from pic.store import EventStore

    store = EventStore()
    start = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    store.add(_payment("o1", status="failed", code="GATEWAY_TIMEOUT", when=start))
    store.add(_payment("o1", status="success", code=None, when=start + timedelta(seconds=30)))
    store.add(_payment("o2", status="failed", code="GATEWAY_TIMEOUT", when=start))

    orders, summary = find_recoverable_orders(store, start, start + timedelta(minutes=5))
    assert [o.order_id for o in orders] == ["o2"]
    assert summary["already_completed_by_customer"] == 1


def test_one_order_that_failed_repeatedly_is_one_recoverable_order():
    from pic.store import EventStore

    store = EventStore()
    start = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    for i in range(3):
        store.add(
            _payment(
                "o1", status="failed", code="AUTH_TIMEOUT", when=start + timedelta(seconds=i * 10)
            )
        )
    orders, summary = find_recoverable_orders(store, start, start + timedelta(minutes=5))
    assert len(orders) == 1
    assert summary["failed_payments"] == 1


def test_every_recovery_method_is_one_the_failure_actually_implies():
    for code, method in RECOVERY_METHOD.items():
        assert method in ("retry", "alternate_route", "payment_link"), code
        assert code not in HARD_DECLINES


def test_the_recovery_campaign_is_clamped_to_the_methods_policy_allows():
    engine, incident = _run("SCN-UPI-PSP")
    recovery = incident.order_recovery
    assert recovery is not None
    if not recovery.executed:
        pytest.skip(f"policy declined the campaign in this run: {recovery.note}")
    allowed = set(engine.gateway.policy["recovery"]["autonomous_methods"])
    assert set(recovery.by_method) <= allowed, (
        "a method the merchant withheld must never be attempted autonomously"
    )
    assert recovery.recovered <= recovery.attempted <= recovery.recoverable_payments


def test_recovered_revenue_reaches_the_incident_ledger():
    _, incident = _run("SCN-UPI-PSP")
    recovery = incident.order_recovery
    revenue = incident.revenue
    assert recovery is not None and revenue is not None
    if recovery.recovered_value_paise:
        assert revenue.revenue_recovered_paise > 0
        assert revenue.revenue_recovered_paise <= recovery.recovered_value_paise


def test_order_recovery_is_skipped_when_the_fix_was_reverted():
    """Chasing customers back into a payment stack that is still broken is not a recovery."""
    _, incident = _run("SCN-UPI-PSP-BADFALLBACK")
    assert incident.order_recovery is None
    assert incident.revenue is not None
    assert incident.revenue.revenue_recovered_paise == 0


def test_the_recovery_campaign_has_a_working_inverse():
    control = _engine().simulator.control
    orders = [
        {
            "order_id": f"o{i}",
            "amount_paise": 1000,
            "recovery_probability": 1.0,
            "recovery_method": "retry",
        }
        for i in range(5)
    ]
    campaign = control.recover_payments("camp1", orders, ["retry"])
    assert campaign["recovered"] == 5
    undone = control.cancel_recovery("camp1")
    assert undone["cancelled"] is True
    # The inverse is honest about its limits rather than implying money came back.
    assert undone["already_recovered"] == 5
    assert "not reversed" in undone["note"]
    assert control.snapshot()["recovery_campaigns"]["camp1"]["cancelled"] is True


# --------------------------------------------------------------------------
# Prevention
# --------------------------------------------------------------------------


def test_a_pattern_needs_repetition_before_it_is_reported(memory):
    for i in range(MIN_OCCURRENCES - 1):
        memory.record_incident(_record(f"INC-000{i}"))
    assert find_patterns(memory.all()) == []
    memory.record_incident(_record("INC-0009"))
    assert len(find_patterns(memory.all())) == 1


def test_a_recommendation_states_its_conditions_and_its_evidence(memory):
    for i in range(4):
        memory.record_incident(_record(f"INC-000{i}", at_risk=1_000_000, protected=600_000))
    [recommendation] = find_patterns(memory.all())
    assert recommendation.occurrences == 4
    assert set(recommendation.incident_ids) == {"INC-0000", "INC-0001", "INC-0002", "INC-0003"}
    assert recommendation.conditions
    assert recommendation.evidence
    assert recommendation.historical_revenue_lost_paise == 4 * 400_000
    # A preemptive shift is smaller than the reactive one it is derived from.
    assert recommendation.proposed_action is ActionType.SHIFT_TRAFFIC
    assert recommendation.proposed_parameters["percentage"] < 20


def test_a_recommendation_always_requires_merchant_approval(memory):
    for i in range(4):
        memory.record_incident(_record(f"INC-000{i}"))
    for recommendation in find_patterns(memory.all()):
        assert recommendation.requires_merchant_approval is True
        assert recommendation.status == "PROPOSED"


def test_no_pattern_is_proposed_for_something_that_was_never_a_fault(memory):
    """A traffic-mix change is not broken, and a scheduled reroute against one would be harm."""
    for i in range(5):
        record = _record(f"INC-000{i}")
        memory.record_incident(record.model_copy(update={"false_positive": True}))
    assert find_patterns(memory.all()) == []


def test_no_pattern_is_proposed_for_a_fault_nothing_has_ever_fixed(memory):
    for i in range(5):
        memory.record_incident(
            _record(f"INC-000{i}", verification="FAILED", protected=0, rollback=True)
        )
    assert find_patterns(memory.all()) == [], (
        "a pattern the system has never fixed should be described to a human, not scheduled"
    )


def test_accepting_a_recommendation_records_it_and_changes_no_policy():
    memory = IncidentMemory(persist=False)
    engine = _engine(memory=memory)
    for i in range(4):
        memory.record_incident(_record(f"INC-000{i}"))
    engine.supervisor._refresh_prevention("merch_acme")
    assert engine.supervisor.prevention

    before = engine.gateway.policy["bounds"]["max_traffic_shift_pct"]
    before_allowed = list(engine.gateway.policy["allowed_actions"])
    recommendation = engine.supervisor.prevention[0]
    accepted = engine.supervisor.acknowledge_prevention(
        recommendation.recommendation_id, who="ops_lead", accept=True
    )

    assert accepted.status == "ACKNOWLEDGED" and accepted.acknowledged_by == "ops_lead"
    assert engine.gateway.policy["bounds"]["max_traffic_shift_pct"] == before
    assert engine.gateway.policy["allowed_actions"] == before_allowed
    assert accepted.requires_merchant_approval is True


def test_an_acknowledgement_survives_the_patterns_being_recomputed():
    memory = IncidentMemory(persist=False)
    engine = _engine(memory=memory)
    for i in range(4):
        memory.record_incident(_record(f"INC-000{i}"))
    engine.supervisor._refresh_prevention("merch_acme")
    rid = engine.supervisor.prevention[0].recommendation_id
    engine.supervisor.acknowledge_prevention(rid, who="ops_lead", accept=False)

    memory.record_incident(_record("INC-0009"))
    engine.supervisor._refresh_prevention("merch_acme")
    [again] = [p for p in engine.supervisor.prevention if p.recommendation_id == rid]
    assert again.status == "DISMISSED", "a dismissed recommendation must not silently reappear"


# --------------------------------------------------------------------------
# Safety under learning
# --------------------------------------------------------------------------


def test_learning_cannot_execute_anything_itself(memory):
    """Memory is a query surface. It holds no tool, no control plane and no approval."""
    for name in ("registry", "control", "gateway", "execute", "call"):
        assert not hasattr(memory, name), f"IncidentMemory must not expose {name!r}"


def test_no_amount_of_history_lets_an_action_skip_the_policy_gateway():
    """A hundred successes is still not authority."""
    memory = IncidentMemory(persist=False)
    for i in range(100):
        memory.record_incident(_record(f"INC-{i:04d}", verification="RECOVERED"))
    _, incident = _run("SCN-UPI-PSP", memory=memory)
    assert incident.policy_decision is not None, "the gateway must still have been consulted"
    if incident.action_result is not None and incident.action_result.executed:
        assert incident.policy_decision.approved
        assert incident.action_result.parameters == incident.policy_decision.granted_parameters


def test_history_cannot_widen_a_merchant_policy_limit():
    memory = IncidentMemory(persist=False)
    for i in range(50):
        memory.record_incident(
            _record(f"INC-{i:04d}", magnitude=80.0, verification="RECOVERED")
        )
    engine, incident = _run("SCN-UPI-PSP", memory=memory)
    limit = engine.gateway.policy["bounds"]["max_traffic_shift_pct"]
    for record in incident.audit:
        if record.action == "shift_traffic":
            assert record.parameters["percentage"] <= limit + 1e-9


def test_history_cannot_create_an_action_the_merchant_never_allowed():
    memory = IncidentMemory(persist=False)
    for i in range(20):
        memory.record_incident(
            _record(f"INC-{i:04d}", action="disable_payment_method", magnitude=None)
        )
    _, incident = _run("SCN-UPI-PSP", memory=memory)
    plan = incident.recovery_plan
    assert plan is not None
    allowed = {"shift_traffic", "create_incident_ticket", "set_monitoring_frequency", "no_action"}
    assert {s.action.value for s in plan.strategies} <= allowed, (
        "the option set comes from the diagnosis, not from what history happens to contain"
    )


def test_history_cannot_override_a_requirement_for_human_approval():
    memory = IncidentMemory(persist=False)
    for i in range(50):
        memory.record_incident(_record(f"INC-{i:04d}", verification="RECOVERED"))
    engine = _engine(memory=memory)
    from pic.schemas import ActionProposal

    proposal = ActionProposal(
        incident_id="INC-TEST",
        action=ActionType.DISABLE_PAYMENT_METHOD,
        parameters={"payment_method": "upi"},
        expected_value_paise=10_000_000,
        risk_score=0.1,
        reversible=True,
    )
    decision = engine.gateway.evaluate(proposal, now=engine.now)
    assert decision.requires_human is True
    assert decision.approved is False


def test_the_reasoner_is_never_given_the_chance_to_author_a_past(memory):
    """Historical claims are computed before the model is called, from stored records only."""
    memory.record_incident(_record("INC-0001", verification="RECOVERED"))
    _, incident = _run("SCN-UPI-PSP", memory=memory)
    plan = incident.recovery_plan
    assert plan is not None
    known = {r.incident_id for r in memory.all()}
    for match in plan.similar_incidents:
        assert match["incident_id"] in known, "a cited past incident must exist in the store"
    for strategy in plan.strategies:
        support = strategy.historical_support
        if support is None or support.stats is None:
            continue
        assert set(support.stats.incident_ids) <= known
        assert support.advisory is True


def test_the_historical_claim_is_backed_by_a_recorded_tool_call(memory):
    """Every fact an agent asserts must be traceable to a tool call, history included."""
    memory.record_incident(_record("INC-0001", verification="RECOVERED"))
    _, incident = _run("SCN-UPI-PSP", memory=memory)
    step = next(s for s in incident.steps if s.agent == "recovery_strategy")
    tools = {c.tool for c in step.tool_calls}
    assert "get_action_outcomes" in tools


def test_a_recovery_campaign_cannot_run_without_an_approving_decision():
    from pic.tools.registry import PolicyViolation

    engine = _engine()
    with pytest.raises(PolicyViolation):
        engine.registry.call("recover_failed_payments", {"orders": [], "methods": ["retry"]})


def test_an_approval_for_a_reroute_cannot_authorise_a_recovery_campaign():
    from pic.schemas import PolicyDecision, PolicyOutcome
    from pic.tools.registry import PolicyViolation

    engine = _engine()
    approval = PolicyDecision(
        incident_id="INC-TEST",
        action=ActionType.SHIFT_TRAFFIC,
        outcome=PolicyOutcome.APPROVE,
        approved=True,
    )
    with pytest.raises(PolicyViolation):
        engine.registry.call(
            "recover_failed_payments", {"orders": [], "methods": ["retry"]}, approval=approval
        )


def test_the_new_write_tools_are_gated_and_reversible():
    engine = _engine()
    for name in ("recover_failed_payments", "cancel_order_recovery"):
        spec = engine.registry.get(name)
        assert spec.write is True
    assert engine.registry.get("recover_failed_payments").inverse == "cancel_order_recovery"


def test_prevention_is_not_reachable_as_an_executable_action():
    """A recommendation is a document. It has no path into the pipeline."""
    memory = IncidentMemory(persist=False)
    engine = _engine(memory=memory)
    for i in range(4):
        memory.record_incident(_record(f"INC-000{i}"))
    engine.supervisor._refresh_prevention("merch_acme")
    recommendation = engine.supervisor.prevention[0]
    assert not hasattr(recommendation, "execute")
    # Nothing in the control plane moved.
    assert engine.simulator.control.snapshot()["route_weights"]["upi"]["route_A"] == pytest.approx(
        0.55
    )


def test_an_order_recovery_result_reports_zero_rather_than_estimating():
    empty = OrderRecoveryResult(incident_id="INC-TEST")
    assert empty.recovered == 0
    assert empty.recovered_value_paise == 0
    assert empty.executed is False
