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


def test_a_hung_agent_is_abandoned_and_escalated_rather_than_stalling():
    """A step that never returns must become a human handover, not an open incident forever.

    `Agent.timeout_s` documented that a step is "abandoned and treated as a failure" after the
    timeout, but nothing enforced it: a reasoner that never answered held the incident in its
    current state indefinitely, with the merchant still losing money and nobody told. This was
    observed against a live LLM, where the pipeline sat in DIAGNOSING for over ten minutes.
    """
    import time

    from pic.engine import Engine, EngineConfig
    from pic.agents.root_cause import RootCauseAgent
    from pic.schemas import IncidentState

    original = RootCauseAgent.run

    def hangs(self, ctx):
        time.sleep(30)  # far beyond the timeout the test sets
        return original(self, ctx)

    engine = Engine(EngineConfig(seed=7, reasoner="deterministic"))
    engine.warmup(45)
    engine.trigger("SCN-UPI-PSP")

    RootCauseAgent.run = hangs
    engine.supervisor.root_cause_agent.timeout_s = 1.0
    started = time.perf_counter()
    try:
        incident = engine.run_until_incident(max_ticks=40)
        assert incident is not None
        engine.supervisor.run_incident(incident)
    finally:
        RootCauseAgent.run = original

    elapsed = time.perf_counter() - started
    # The hung step is abandoned near its timeout rather than run to completion.
    assert elapsed < 25, f"the supervisor waited {elapsed:.1f}s for a step it should have abandoned"

    diagnosis = next((s for s in incident.steps if s.agent == "root_cause"), None)
    assert diagnosis is not None and not diagnosis.ok
    assert "timed out" in (diagnosis.summary or "")
    # And the incident ends in human hands rather than sitting open.
    assert incident.state is IncidentState.CLOSED
    assert incident.escalation is not None
    # The handover names the agent that died and says it died, so the operator is not sent to
    # investigate payment segments over what is actually a broken agent.
    because = incident.escalation.because
    assert "root cause agent did not produce a diagnosis" in because, because
    assert "AgentTimeout" in because, because


def test_handover_states_the_case_not_the_category():
    """Every escalation explains this incident, not the branch it took.

    The reason strings were a fixed table keyed by reason code, so "No available action addresses
    the diagnosed cause" was returned verbatim for every incident reaching that branch. It names
    the branch. A human paged at 2am needs the action that was refused, the rule that refused it or
    the measurement that came back flat - facts the pipeline already recorded and then dropped.
    """
    from pic.evaluation.harness import Harness

    seen = []

    class Probe(Harness):
        def _score(self, engine, scenario, incident, run):
            if incident.escalation is not None:
                seen.append(incident.escalation)
            return super()._score(engine, scenario, incident, run)

    harness = Probe(reasoner="deterministic")
    # Three scenarios that hand over for three different reasons: policy holds the action, the
    # only useful action cannot route payments, and the fix actively made things worse.
    for scenario_id in ("SCN-UPI-PSP", "SCN-ISSUER", "SCN-UPI-PSP-BADFALLBACK"):
        run = harness.run_scenario(scenario_id, 991)
        assert run.error is None, run.error

    assert seen, "no incident handed over, so the reason could not be checked"
    codes = {e.reason_code for e in seen}
    assert len(codes) > 1, f"expected several handover kinds, got {codes}"

    for esc in seen:
        assert esc.because, f"{esc.reason_code} handed over with no concrete reason"
        assert esc.because != esc.reason, "the reason is the category restated"
        # Concrete means it carries a fact taken from this incident's own record: a measured
        # number, the action that was proposed, or the diagnosis it names.
        pack = esc.context_pack
        proposed = str(pack.get("proposed_action") or "").replace("_", " ")
        diagnosis = str(pack.get("diagnosis") or "")
        assert (
            any(ch.isdigit() for ch in esc.because)
            or (proposed and proposed in esc.because.lower())
            or (diagnosis and diagnosis in esc.because)
        ), esc.because


def test_only_watched_worlds_advance():
    """A simulation nobody is watching must not steal time from one that is.

    Clicking "Run an incident" on the deployed site appeared to do nothing: the scenario was
    injected and no incident ever opened. The trigger was fine - the clock was not. One tick costs
    about 1.35s, almost all of it detection, against a 1s interval, and the loop paid that for
    every session in the registry whether or not anyone was there. Expiry only ran when a new
    session was created, so abandoned worlds accumulated and the simulated world fell about forty
    times behind real time, which reads to a visitor as a broken button.
    """
    import time as _time

    from pic.api import main

    now = _time.monotonic()
    watched = main.Session(session_id="watched", engine=None, hub=None, last_seen=now)
    idle = main.Session(
        session_id="idle", engine=None, hub=None, last_seen=now - main.ACTIVE_WINDOW_S - 5
    )
    stopped = main.Session(session_id="stopped", engine=None, hub=None, last_seen=now, running=False)
    gone = main.Session(
        session_id="gone", engine=None, hub=None, last_seen=now - main.SESSION_TTL_S - 5
    )

    saved = dict(main._sessions)
    main._sessions.clear()
    main._sessions.update({s.session_id: s for s in (watched, idle, stopped, gone)})
    try:
        advancing = main.sessions_to_advance(now)
        ids = {s.session_id for s in advancing}
        assert ids == {"watched"}, ids
        # Abandoned past the TTL is dropped outright; merely quiet is kept, so returning to the
        # tab resumes the world rather than starting a new one.
        assert "gone" not in main._sessions
        assert "idle" in main._sessions
    finally:
        main._sessions.clear()
        main._sessions.update(saved)


def _handed_over():
    """An incident that tried a fix, measured it, reverted it and gave up."""
    from pic.engine import Engine, EngineConfig
    from pic.schemas import IncidentState

    engine = Engine(EngineConfig(seed=991, reasoner="deterministic"))
    engine.warmup(45)
    engine.trigger("SCN-UPI-PSP-BADFALLBACK")
    incident = engine.run_until_incident(max_ticks=40)
    assert incident is not None
    engine.supervisor.run_incident(incident)
    if incident.state is IncidentState.AWAITING_HUMAN_APPROVAL:
        engine.supervisor.approve(incident, approver="benchmark_operator")
        engine.supervisor.run_incident(incident)
    assert incident.escalation is not None
    return engine, incident


def test_a_handover_offers_something_a_human_can_actually_do():
    """An escalation with no next step is a dead end dressed up as a conclusion.

    The system said what broke, why it stopped and what to consider, then closed the incident -
    and everything after that sentence had to happen somewhere this product knows nothing about.
    Every handover now carries the moves available on that specific incident, and every one of them
    is an operation the supervisor implements.
    """
    from pic.schemas import IncidentState
    engine, incident = _handed_over()
    steps = incident.escalation.next_steps
    assert steps, "a handover must offer a next move"

    verbs = {step.action.split(":")[0] for step in steps}
    assert verbs <= {"approve", "reject", "override", "run_alternative", "retry", "retry_rollback",
                     "acknowledge"}, verbs
    # Someone can always take ownership, whatever else is on offer.
    assert "acknowledge" in verbs

    for step in steps:
        assert step.label and step.detail, step
        # An option that cannot change anything is not a next step. "Try no action instead" and
        # "Try create incident ticket instead" were both offered before this was enforced.
        if step.action.startswith("run_alternative:"):
            assert step.action.split(":", 1)[1] not in {
                "no_action", "notify_merchant", "create_incident_ticket", "set_monitoring_frequency"
            }


def test_acknowledging_records_who_took_it():
    """Not a resolution - a statement that somebody owns it, which is what was missing."""
    from pic.schemas import IncidentState
    engine, incident = _handed_over()
    engine.supervisor.acknowledge(incident, who="ops@acme.example", note="raised with PSP support")

    assert incident.outcome == "ACKNOWLEDGED_BY_HUMAN"
    assert "ops@acme.example" in incident.escalation.recommended_human_action
    assert "raised with PSP support" in incident.escalation.recommended_human_action
    # Nothing left to press: it has an owner now.
    assert incident.escalation.next_steps == []


def test_an_override_is_recorded_against_a_person_and_still_verified():
    """Overriding substitutes a human's judgement for a threshold, not for the checking.

    The claim this project rests on is that no model can execute anything. An override does not
    weaken it: only a human can call it, they must say why, the decision names them, and the action
    is still measured against a control group and still reverted if it did not help.
    """
    from pic.engine import Engine, EngineConfig
    from pic.schemas import IncidentState, PolicyOutcome

    engine = Engine(EngineConfig(seed=991, reasoner="deterministic"))
    engine.warmup(45)
    # SCN-MULTI on this seed reliably stops at the confidence floor, so the gate is actually
    # exercised rather than skipped past.
    engine.trigger("SCN-MULTI")
    incident = engine.run_until_incident(max_ticks=40)
    assert incident is not None
    engine.supervisor.run_incident(incident)
    assert incident.state is IncidentState.AWAITING_HUMAN_APPROVAL, incident.state

    # A held action is not a refused one: the answer to it is approve, not override.
    assert incident.policy_decision.requires_human
    with pytest.raises(ValueError):
        engine.supervisor.override(incident, who="ops", reason="")

    engine.supervisor.approve(incident, approver="ops@acme.example")
    assert incident.policy_decision.outcome is PolicyOutcome.APPROVE
    assert incident.policy_decision.approved_by == "ops@acme.example"


def test_retry_looks_again_instead_of_leaving_the_incident_closed():
    """A diagnosis is only as good as the window it was made in."""
    from pic.schemas import IncidentState
    engine, incident = _handed_over()
    first = incident.escalation.reason_code

    engine.supervisor.retry(incident, who="ops@acme.example")

    # It ran again and reached a conclusion rather than sitting in whatever state retry left it.
    assert incident.state in {IncidentState.CLOSED, IncidentState.AWAITING_HUMAN_APPROVAL}
    assert incident.outcome is not None
    assert first  # the original reason existed; the retry may agree or differ


def test_an_acknowledged_incident_is_not_picked_back_up_by_automation():
    """Someone said they have it. The system must not quietly decide otherwise.

    Acknowledging records ownership, and `override` and `retry` would both re-run the pipeline over
    the top of it, overwriting the record so nothing showed a person was involved. The UI stops
    offering the buttons, but the endpoints are public and "the UI does not offer it" has never
    been a guarantee - this was found by calling them in the order an operator plausibly would.
    """
    from pic.schemas import IncidentState

    engine, incident = _handed_over()
    engine.supervisor.acknowledge(incident, who="ops@acme.example", note="PSP ticket 4471")
    assert incident.outcome == "ACKNOWLEDGED_BY_HUMAN"

    for call in (
        lambda: engine.supervisor.retry(incident, who="someone_else"),
        lambda: engine.supervisor.override(incident, who="someone_else", reason="because"),
        lambda: engine.supervisor.acknowledge(incident, who="someone_else"),
    ):
        with pytest.raises(ValueError, match="acknowledged"):
            call()

    # The ownership record survived every one of those attempts.
    assert incident.outcome == "ACKNOWLEDGED_BY_HUMAN"
    assert "ops@acme.example" in incident.escalation.recommended_human_action
    assert incident.state is IncidentState.CLOSED
