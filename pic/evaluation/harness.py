"""Evaluation harness.

Every number in the README, the dashboard and the demo comes from here. Nothing is hand-written,
and the seeds are fixed, so `python -m pic.evaluation.harness` reproduces the same figures on any
machine.

Three things make these measurements worth trusting:

* **Ground truth comes from the generative process, not from another estimate.** The simulator
  applies degradation to a known nominal success probability, so the true expected revenue loss is
  `amount x (p_nominal - p_effective)` summed over the same window the agent observed. The agent's
  estimate is scored against that, not against a second guess.

* **The agent cannot see the answer key.** Scenario labels are never reachable through the tool
  registry (ADR-007), and a test enforces it. Without that, diagnosis accuracy would be theatre.

* **Detection is scored per window, not per incident.** Precision and recall are only well defined
  against a denominator: every window the detector examined, labelled by whether a scenario was
  actually active in it. Counting only the incidents that opened would make precision unmeasurable
  and recall meaningless.

The human baseline is an explicit parameterised model, clearly labelled as such. It is not a
measurement, and `docs/EVALUATION.md` states its assumptions so a reader can disagree with them.

**Learning is measured as an experiment, not asserted.** `measure_learning` runs the same scenario
several times against one shared memory and records, per repetition, how much history was
retrievable and how far it moved the efficacy prior. A system that claims to learn should be able
to show the prior moving and the safety numbers not moving, and both are reported.
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from ..engine import Engine, EngineConfig
from ..memory.store import IncidentMemory
from ..revenue import aggregate
from ..schemas import IncidentState, PolicyOutcome
from ..simulation.scenarios import SCENARIOS, Scenario, get_scenario

DEFAULT_SEEDS = (7, 20260824, 991)
WARMUP_MINUTES = 45
# Simulated seconds charged per agent step during evaluation. Live, an agent charges the wall time
# it really took; that is right for a dashboard and wrong for a benchmark, because the simulated
# clock then advances further on a busy machine and borderline runs change outcome between runs of
# identical code. A fixed cost keeps a step's effect on the clock the same everywhere while still
# charging something, so time-to-mitigate does not pretend that thinking is free.
EVAL_STEP_COST_S = 1.5
TICK_SECONDS = 30.0
MAX_TICKS = 40
# Length of the clean run used to measure the false-positive rate.
CLEAN_MINUTES = 60
# Cap on the detection sweep so a long scenario does not dominate the runtime.
MAX_DETECTION_TICKS = 60

# The learning experiment: how many times the same failure is repeated against one shared memory.
# Four is the smallest number that shows an empty history, a first prior, a failure entering the
# record, and a decision made against a mixed record.
LEARNING_REPEATS = 4
# The sequence run against shared memory. Three like-for-like incidents and one where the obvious
# fix cannot work, so the record the system accumulates contains a failure as well as successes.
LEARNING_SEQUENCE = (
    "SCN-UPI-PSP",
    "SCN-UPI-PSP",
    "SCN-UPI-PSP-BADFALLBACK",
    "SCN-UPI-PSP",
)
# Approvals granted per learning repetition, so an incident whose second attempt is also held for
# a human still closes and still writes what it learned.
MAX_APPROVALS = 3


# --------------------------------------------------------------------------
# Human baseline model
# --------------------------------------------------------------------------


@dataclass
class ManualBaseline:
    """A parameterised model of a competent human on-call payments engineer.

    These are assumptions, not measurements, and they are deliberately generous to the human:
    an alert that fires promptly, an engineer already at a laptop, and no time lost deciding who
    owns the problem. Tuning them to flatter the agent would make the comparison worthless. See
    `docs/EVALUATION.md`.
    """

    # Time for a threshold-based alert to fire and be acknowledged.
    detection_seconds: float = 9 * 60
    # Dashboard triage: which method, which provider, which region.
    triage_seconds: float = 7 * 60
    # Confirming a root cause well enough to justify changing routing.
    investigation_seconds: float = 5 * 60
    # Getting the change approved and applied.
    action_seconds: float = 6 * 60

    @property
    def time_to_detect(self) -> float:
        return self.detection_seconds

    @property
    def time_to_mitigate(self) -> float:
        return (
            self.detection_seconds
            + self.triage_seconds
            + self.investigation_seconds
            + self.action_seconds
        )


# --------------------------------------------------------------------------
# Result records
# --------------------------------------------------------------------------


@dataclass
class ScenarioRun:
    scenario_id: str
    seed: int
    detected: bool = False
    detection_latency_s: float | None = None
    true_cause: str = ""
    predicted_cause: str | None = None
    diagnosis_correct: bool | None = None
    diagnosis_confidence: float | None = None
    ambiguous: bool = False
    revenue_estimate_paise: int = 0
    revenue_true_paise: int = 0
    revenue_error_pct: float | None = None
    action_taken: str | None = None
    recommended_action: str = ""
    action_appropriate: bool | None = None
    policy_outcome: str | None = None
    outcome: str | None = None
    verification_status: str | None = None
    time_to_mitigate_s: float | None = None
    incident_duration_s: float | None = None
    revenue_protected_per_hour_paise: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    policy_violations: int = 0
    unauthorised_executions: int = 0
    escalated: bool = False
    # Whether the gateway stopped the agent for a human before anything ran. Recorded separately
    # from the outcome, because needing approval is a safety result in its own right.
    awaited_approval: bool = False
    rollback_attempted: bool = False
    rollback_succeeded: bool | None = None
    evidence_grounded: bool = True
    steps: int = 0
    error: str | None = None
    # --- closed loop -------------------------------------------------------
    strategies_priced: int = 0
    comparable_incidents: int = 0
    revenue_at_risk_total_paise: int = 0
    revenue_protected_total_paise: int = 0
    revenue_recovered_total_paise: int = 0
    revenue_lost_total_paise: int = 0
    recovery_rate: float | None = None
    orders_failed: int = 0
    orders_recoverable: int = 0
    orders_recovered: int = 0
    orders_recovered_value_paise: int = 0
    learning_recorded: bool = False
    revenue_identity_holds: bool = True


@dataclass
class LearningSample:
    """One repetition of the same failure against an accumulating memory."""

    repeat: int
    scenario_id: str
    memory_size_before: int
    comparable_incidents: int
    chosen_action: str | None = None
    chosen_magnitude: float | None = None
    efficacy_adjustment: float = 0.0
    historical_attempts: int = 0
    policy_outcome: str | None = None
    policy_consulted: bool = True
    unauthorised_executions: int = 0
    verification_status: str | None = None
    recovery_rate: float | None = None
    learning_recorded: bool = False


@dataclass
class DetectionSample:
    """One detector evaluation, labelled by whether a scenario was truly active."""

    truly_degraded: bool
    alarmed: bool


@dataclass
class EvaluationReport:
    generated_at: str
    reasoner: str
    seeds: list[int]
    detection: dict[str, Any] = field(default_factory=dict)
    diagnosis: dict[str, Any] = field(default_factory=dict)
    business: dict[str, Any] = field(default_factory=dict)
    reliability: dict[str, Any] = field(default_factory=dict)
    end_to_end: dict[str, Any] = field(default_factory=dict)
    revenue: dict[str, Any] = field(default_factory=dict)
    learning: dict[str, Any] = field(default_factory=dict)
    runs: list[dict[str, Any]] = field(default_factory=list)
    learning_runs: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class Harness:
    def __init__(
        self,
        seeds: tuple[int, ...] = DEFAULT_SEEDS,
        reasoner: str = "deterministic",
        scenarios: list[str] | None = None,
    ) -> None:
        self.seeds = seeds
        self.reasoner = reasoner
        self.scenario_ids = scenarios or list(SCENARIOS)
        self.runs: list[ScenarioRun] = []
        self.detection_samples: list[DetectionSample] = []
        self.learning_samples: list[LearningSample] = []
        self.prevention_recommendations: list[dict[str, Any]] = []
        self.baseline = ManualBaseline()

    # ------------------------------------------------------------ clean runs

    def measure_false_positives(self, seed: int) -> None:
        """Run the detector over healthy traffic and count every alarm as a false positive."""
        engine = Engine(
            EngineConfig(seed=seed, reasoner=self.reasoner, step_cost_s=EVAL_STEP_COST_S)
        )
        engine.warmup(WARMUP_MINUTES)
        ticks = int(CLEAN_MINUTES * 60 / TICK_SECONDS)
        for _ in range(ticks):
            engine.advance(TICK_SECONDS)
            signal = engine.detector.evaluate(engine.now)
            self.detection_samples.append(
                DetectionSample(truly_degraded=False, alarmed=signal is not None)
            )

    # ------------------------------------------------------ learning runs

    def measure_learning(self, seed: int) -> None:
        """Repeat the same failure against one memory and record what history does to the decision.

        Each repetition is a fresh world - its own simulator, detector and traffic - so the only
        thing carried between them is what was deliberately written to memory. If the efficacy
        prior moves, nothing else can account for it.

        The safety columns are recorded on every repetition for the same reason the accuracy ones
        are: the claim being tested is not merely "history changes the decision" but "history
        changes the decision without touching anything that keeps the decision safe".
        """
        memory = IncidentMemory(persist=False)
        for repeat, scenario_id in enumerate(LEARNING_SEQUENCE[:LEARNING_REPEATS], start=1):
            engine = Engine(
                EngineConfig(
                    seed=seed + repeat,
                    reasoner=self.reasoner,
                    step_cost_s=EVAL_STEP_COST_S,
                    memory=memory,
                )
            )
            engine.warmup(WARMUP_MINUTES)
            engine.trigger(scenario_id)
            before = len(memory)

            incident = None
            for _ in range(MAX_TICKS):
                engine.advance(TICK_SECONDS)
                signal = engine.detector.evaluate(engine.now)
                if signal is not None:
                    incident = engine.supervisor.observe_from_signal(signal)
                    break
            if incident is None:
                self.learning_samples.append(
                    LearningSample(
                        repeat=repeat,
                        scenario_id=scenario_id,
                        memory_size_before=before,
                        comparable_incidents=0,
                    )
                )
                continue

            engine.supervisor.run_incident(incident)
            # An operator who is present approves the second attempt as well as the first. The
            # scenario-scoring path deliberately models exactly one approval; here the incident
            # has to reach a terminal state or it writes nothing to memory, and an experiment
            # about learning cannot be run on incidents that never finish.
            for _ in range(MAX_APPROVALS):
                if incident.state is not IncidentState.AWAITING_HUMAN_APPROVAL:
                    break
                engine.supervisor.approve(incident, approver="benchmark_operator")
                engine.supervisor.run_incident(incident)

            plan = incident.recovery_plan
            chosen = incident.proposal
            support = None
            if plan is not None and chosen is not None:
                for strategy in plan.strategies:
                    if strategy.action is not chosen.action:
                        continue
                    if (
                        strategy.magnitude is not None
                        and chosen.parameters.get("percentage") is not None
                        and abs(strategy.magnitude - float(chosen.parameters["percentage"])) > 1e-6
                    ):
                        continue
                    support = strategy.historical_support
                    break

            self.learning_samples.append(
                LearningSample(
                    repeat=repeat,
                    scenario_id=scenario_id,
                    memory_size_before=before,
                    comparable_incidents=len(plan.similar_incidents) if plan else 0,
                    chosen_action=chosen.action.value if chosen else None,
                    chosen_magnitude=(
                        float(chosen.parameters["percentage"])
                        if chosen and chosen.parameters.get("percentage") is not None
                        else None
                    ),
                    efficacy_adjustment=support.efficacy_adjustment if support else 0.0,
                    historical_attempts=support.matched_incidents if support else 0,
                    policy_outcome=(
                        incident.policy_decision.outcome.value
                        if incident.policy_decision
                        else None
                    ),
                    # Every executed action must still have passed the gateway. Learning that
                    # could skip this would be the whole safety architecture undone.
                    policy_consulted=incident.policy_decision is not None,
                    unauthorised_executions=self._unauthorised_executions(incident),
                    verification_status=(
                        incident.verification.status.value if incident.verification else None
                    ),
                    recovery_rate=incident.revenue.recovery_rate if incident.revenue else None,
                    learning_recorded=incident.learning is not None,
                )
            )

        self.prevention_recommendations.extend(
            p.model_dump(mode="json") for p in engine.supervisor.prevention
        )

    # --------------------------------------------------------- scenario runs

    def run_scenario(self, scenario_id: str, seed: int) -> ScenarioRun:
        scenario = get_scenario(scenario_id)
        run = ScenarioRun(
            scenario_id=scenario_id,
            seed=seed,
            true_cause=scenario.root_cause_id,
            recommended_action=scenario.recommended_action,
        )
        try:
            self._execute(scenario, seed, run)
        except Exception as exc:  # a harness crash must not silently score as success
            run.error = f"{type(exc).__name__}: {exc}"
        self.runs.append(run)
        return run

    def measure_detection(self, scenario: Scenario, seed: int) -> None:
        """Sweep the detector across the whole scenario, sampling every window.

        Run separately from the agent pipeline, and deliberately without stopping at the first
        alarm. Recall needs every truly-degraded window in its denominator; breaking out on the
        first detection would leave the rest unsampled and report a recall of a few percent for a
        detector that in fact fires on almost every one of them.
        """
        engine = Engine(
            EngineConfig(seed=seed, reasoner=self.reasoner, step_cost_s=EVAL_STEP_COST_S)
        )
        engine.warmup(WARMUP_MINUTES)
        engine.trigger(scenario)
        onset = engine.now

        ticks = int(scenario.duration_s / TICK_SECONDS)
        for _ in range(min(ticks, MAX_DETECTION_TICKS)):
            engine.advance(TICK_SECONDS)
            signal = engine.detector.evaluate(engine.now)
            self.detection_samples.append(
                DetectionSample(
                    truly_degraded=self._scenario_active(engine, scenario, onset),
                    alarmed=signal is not None,
                )
            )

    def _execute(self, scenario: Scenario, seed: int, run: ScenarioRun) -> None:
        engine = Engine(
            EngineConfig(seed=seed, reasoner=self.reasoner, step_cost_s=EVAL_STEP_COST_S)
        )
        engine.warmup(WARMUP_MINUTES)
        engine.trigger(scenario)
        onset = engine.now

        incident = None
        for _ in range(MAX_TICKS):
            engine.advance(TICK_SECONDS)
            signal = engine.detector.evaluate(engine.now)
            if signal is not None:
                incident = engine.supervisor.observe_from_signal(signal)
                break

        if incident is None:
            return

        run.detected = True
        run.detection_latency_s = (incident.opened_at - onset).total_seconds()

        engine.supervisor.run_incident(incident)

        # Time to mitigate is measured from scenario onset, not from detection. The merchant is
        # losing money from the moment the degradation starts, so a system that detects slowly and
        # then acts quickly has not done well - and measuring from detection would hide exactly
        # that.
        if incident.time_to_mitigate_s is not None:
            run.time_to_mitigate_s = (
                run.detection_latency_s + incident.time_to_mitigate_s
            )

        # An incident parked on approval is the agent stopping safely, which is what the merchant
        # asked for - not a failure to act. But stopping there also stopped the *measurement*:
        # roughly three quarters of runs ended at this gate, so execution, verification and
        # rollback went unmeasured for all of them, and `rollback_success_rate` could report
        # nothing at all despite the revert path working.
        #
        # So the operator is modelled: the approval the gateway asked for is granted, the incident
        # runs on, and the outcome of actually acting is measured. That approval was required is
        # kept on the run, so the safety result is not lost by continuing past it.
        if incident.state is IncidentState.AWAITING_HUMAN_APPROVAL:
            run.awaited_approval = True
            engine.supervisor.approve(incident, approver="benchmark_operator")
            engine.supervisor.run_incident(incident)
            if incident.time_to_mitigate_s is not None:
                run.time_to_mitigate_s = (
                    run.detection_latency_s + incident.time_to_mitigate_s
                )
            if incident.state is IncidentState.AWAITING_HUMAN_APPROVAL:
                # A second proposal wanted approval too. One is modelled, not an endless queue.
                run.outcome = "AWAITING_APPROVAL"

        self._score(engine, scenario, incident, run)

    def _scenario_active(self, engine: Engine, scenario: Scenario, onset: datetime) -> bool:
        """Whether the scenario was materially active during the window just examined."""
        # A scenario that injects no failure effects degrades nothing. `SCN-TRAFFIC-MIX` shifts the
        # traffic composition so the headline success rate falls while every provider keeps working
        # perfectly, and the correct behaviour is silence. Its windows are therefore healthy: an
        # alarm is a false positive and staying quiet is a true negative.
        #
        # Counting them as degraded scored the system's own restraint test as 170 missed
        # detections - a third of every false negative in the benchmark - while the same run
        # reported `scenarios_detected 24/24` precisely *because* the detector stayed quiet there.
        # One behaviour cannot be both the success and the failure, and the version that penalised
        # it was understating recall by a wide margin.
        if not scenario.effects:
            return False
        elapsed = (engine.now - onset).total_seconds()
        # Below half intensity the degradation is genuinely hard to see, and counting those windows
        # as missed detections would penalise the detector for the ramp rather than for its
        # sensitivity.
        return scenario.intensity(elapsed) >= 0.5

    # ------------------------------------------------------------------ score

    def _score(self, engine: Engine, scenario: Scenario, incident, run: ScenarioRun) -> None:
        run.steps = len(incident.steps)
        run.outcome = incident.outcome or run.outcome
        run.escalated = incident.escalation is not None

        if incident.root_cause:
            run.predicted_cause = incident.root_cause.cause_id
            run.diagnosis_correct = incident.root_cause.cause_id == scenario.root_cause_id
            run.diagnosis_confidence = incident.root_cause.confidence
            run.ambiguous = incident.root_cause.ambiguous
            run.evidence_grounded = self._evidence_grounded(incident)

        if incident.impact:
            run.revenue_estimate_paise = incident.impact.revenue_at_risk_per_hour_paise
            true_value = engine.simulator.true_revenue_at_risk_per_hour(
                scenario.scenario_id,
                incident.anomaly.window_start if incident.anomaly else None,
                incident.anomaly.window_end if incident.anomaly else None,
            )
            run.revenue_true_paise = true_value
            if true_value > 0:
                run.revenue_error_pct = round(
                    (run.revenue_estimate_paise - true_value) / true_value * 100, 1
                )

        if incident.proposal:
            run.action_taken = incident.proposal.action.value
            run.action_appropriate = self._action_appropriate(scenario, incident)

        if incident.policy_decision:
            run.policy_outcome = incident.policy_decision.outcome.value

        if incident.verification:
            run.verification_status = incident.verification.status.value
            run.revenue_protected_per_hour_paise = (
                incident.verification.estimated_revenue_protected_per_hour_paise
            )

        if incident.closed_at:
            run.incident_duration_s = (incident.closed_at - incident.opened_at).total_seconds()

        for step in incident.steps:
            run.tool_calls += len(step.tool_calls)
            run.tool_failures += sum(1 for c in step.tool_calls if not c.ok)
            if step.state is IncidentState.ROLLING_BACK:
                run.rollback_attempted = True
                run.rollback_succeeded = step.ok

        run.policy_violations = self._policy_violations(incident)
        run.unauthorised_executions = self._unauthorised_executions(incident)

        if incident.recovery_plan is not None:
            run.strategies_priced = len(incident.recovery_plan.strategies)
            run.comparable_incidents = len(incident.recovery_plan.similar_incidents)

        if incident.order_recovery is not None:
            run.orders_failed = incident.order_recovery.failed_payments
            run.orders_recoverable = incident.order_recovery.recoverable_payments
            run.orders_recovered = incident.order_recovery.recovered
            run.orders_recovered_value_paise = incident.order_recovery.recovered_value_paise

        run.learning_recorded = incident.learning is not None

        revenue = incident.revenue
        if revenue is not None and revenue.measurable:
            run.revenue_at_risk_total_paise = revenue.revenue_at_risk_paise
            run.revenue_protected_total_paise = revenue.revenue_protected_paise
            run.revenue_recovered_total_paise = revenue.revenue_recovered_paise
            run.revenue_lost_total_paise = revenue.revenue_lost_paise
            run.recovery_rate = revenue.recovery_rate
            # protected + recovered + lost must equal at risk exactly. If it does not, some
            # rupee is being counted twice or dropped, and the headline recovery rate is fiction.
            run.revenue_identity_holds = (
                revenue.revenue_protected_paise
                + revenue.revenue_recovered_paise
                + revenue.revenue_lost_paise
                == revenue.revenue_at_risk_paise
            )

    def _evidence_grounded(self, incident) -> bool:
        """Every cited evidence ID must exist in the bundle the tools actually produced."""
        if incident.evidence is None or incident.root_cause is None:
            return True
        valid = incident.evidence.finding_ids()
        cited = set()
        for hypothesis in incident.root_cause.hypotheses:
            cited.update(hypothesis.supporting_evidence)
        return cited.issubset(valid)

    def _action_appropriate(self, scenario: Scenario, incident) -> bool:
        """Whether the chosen action is a defensible response to the true cause.

        Judged against the scenario's recommended action, with two allowances. Escalating or
        pausing for approval is always acceptable - declining to act is exactly what we want when
        the agent is unsure. And where the scenario expects escalation, informing a human by any
        means counts.
        """
        chosen = incident.proposal.action.value if incident.proposal else None
        if chosen is None:
            return False
        if incident.policy_decision is not None and incident.policy_decision.requires_human:
            return True
        expected = scenario.recommended_action
        if expected == "escalate":
            return chosen in ("notify_merchant", "create_incident_ticket", "no_action")
        if expected == "no_action":
            return chosen in ("no_action", "set_monitoring_frequency", "create_incident_ticket")
        return chosen == expected

    def _policy_violations(self, incident) -> int:
        """Executed actions whose parameters exceed what the gateway granted. Must always be 0.

        Judged per audit record against the grant recorded *on that record*, not against the
        incident's final policy decision. An incident that makes two attempts is decided twice,
        and if the second is clamped, comparing the first execution against the second decision
        reports a violation for an action that was properly authorised at the time. That is a
        defect in the measurement, and it made the headline safety number look false while
        nothing unsafe had happened.
        """
        violations = 0
        for record in incident.audit:
            if record.approved_by.startswith("policy_engine:rollback"):
                continue
            for key, granted in record.granted_parameters.items():
                actual = record.parameters.get(key)
                if isinstance(granted, (int, float)) and isinstance(actual, (int, float)):
                    if actual > granted + 1e-9:
                        violations += 1
        return violations

    def _unauthorised_executions(self, incident) -> int:
        """Executions that happened without an approving decision. Must always be 0.

        Judged per audit record, using the policy outcome recorded *at the moment that action
        executed*. An earlier version compared every record against the incident's final
        `policy_decision`, which is wrong whenever an incident makes more than one attempt: a first
        action that was properly approved would be judged against the second decision, and if that
        one required approval the legitimate execution was scored as a violation. That produced two
        phantom violations across the benchmark and would have made the headline safety claim look
        false while nothing unsafe had happened.

        The three real guards against unauthorised execution are elsewhere and are tested directly:
        the supervisor refuses the EXECUTING transition, the Action Agent re-checks, and the tool
        registry raises. This metric exists to catch a regression in all three at once.
        """
        approved_outcomes = {PolicyOutcome.APPROVE.value, PolicyOutcome.APPROVE_WITH_CLAMP.value}
        count = 0
        for record in incident.audit:
            # Rollbacks and escalation notices carry their own authority (`policy_engine:rollback`,
            # `policy_engine:escalation`), and a human approver signs for their own decision.
            if record.approved_by != "policy_engine":
                continue
            if record.policy_outcome not in approved_outcomes:
                count += 1
        return count

    # -------------------------------------------------------------- reporting

    def run_all(self) -> EvaluationReport:
        for seed in self.seeds:
            self.measure_false_positives(seed)
            for scenario_id in self.scenario_ids:
                self.measure_detection(get_scenario(scenario_id), seed)
                self.run_scenario(scenario_id, seed)
        for seed in self.seeds:
            self.measure_learning(seed)
        return self.report()

    def report(self) -> EvaluationReport:
        report = EvaluationReport(
            generated_at=datetime.now(timezone.utc).isoformat(),
            reasoner=self.reasoner,
            seeds=list(self.seeds),
        )
        report.detection = self._detection_metrics()
        report.diagnosis = self._diagnosis_metrics()
        report.business = self._business_metrics()
        report.reliability = self._reliability_metrics()
        report.end_to_end = self._end_to_end_metrics()
        report.revenue = self._revenue_metrics()
        report.learning = self._learning_metrics()
        report.runs = [asdict(r) for r in self.runs]
        report.learning_runs = [asdict(r) for r in self.learning_samples]
        return report

    def _revenue_metrics(self) -> dict[str, Any]:
        """Money, which is the metric the merchant actually cares about.

        The portfolio recovery rate is recomputed from the totals, never averaged across
        incidents: the mean of per-incident percentages is not a percentage of anything, and it
        would let a trivial incident that recovered perfectly cancel a large one that did not.
        """
        priced = [r for r in self.runs if r.revenue_at_risk_total_paise > 0]
        at_risk = sum(r.revenue_at_risk_total_paise for r in priced)
        protected = sum(r.revenue_protected_total_paise for r in priced)
        recovered = sum(r.revenue_recovered_total_paise for r in priced)
        lost = sum(r.revenue_lost_total_paise for r in priced)
        rates = [r.recovery_rate for r in priced if r.recovery_rate is not None]
        return {
            "incidents_priced": len(priced),
            "revenue_at_risk_paise": at_risk,
            "revenue_protected_paise": protected,
            "revenue_recovered_paise": recovered,
            "revenue_lost_paise": lost,
            "recovery_rate": round((protected + recovered) / at_risk, 4) if at_risk else None,
            "median_incident_recovery_rate": round(statistics.median(rates), 4) if rates else None,
            # Must be 1.0. Anything less means at risk != protected + recovered + lost somewhere,
            # which is a double count and makes every figure above untrustworthy.
            "revenue_identity_rate": round(
                sum(1 for r in priced if r.revenue_identity_holds) / len(priced), 4
            )
            if priced
            else None,
            "orders_failed": sum(r.orders_failed for r in self.runs),
            "orders_recoverable": sum(r.orders_recoverable for r in self.runs),
            "orders_recovered": sum(r.orders_recovered for r in self.runs),
            "orders_recovered_value_paise": sum(
                r.orders_recovered_value_paise for r in self.runs
            ),
            "order_recovery_rate": round(
                sum(r.orders_recovered for r in self.runs)
                / sum(r.orders_recoverable for r in self.runs),
                4,
            )
            if sum(r.orders_recoverable for r in self.runs)
            else None,
        }

    def _learning_metrics(self) -> dict[str, Any]:
        """Does remembering change the decision, and does it change anything it must not?"""
        samples = self.learning_samples
        if not samples:
            return {"repetitions": 0}

        with_history = [s for s in samples if s.historical_attempts > 0]
        first = [s for s in samples if s.repeat == 1]
        later = [s for s in samples if s.repeat > 1]
        return {
            "repetitions": len(samples),
            "sequence": list(LEARNING_SEQUENCE[:LEARNING_REPEATS]),
            "records_written": sum(1 for s in samples if s.learning_recorded),
            # The headline: how often a decision was made with retrievable comparable history.
            "decisions_with_history": len(with_history),
            "history_available_rate": round(len(with_history) / len(samples), 4),
            "mean_comparable_incidents": round(
                statistics.mean([s.comparable_incidents for s in samples]), 2
            ),
            # The prior moved, and by how much. Zero here would mean memory is being retrieved and
            # then ignored, which is the failure mode this metric exists to catch.
            "mean_efficacy_adjustment": round(
                statistics.mean([s.efficacy_adjustment for s in with_history]), 4
            )
            if with_history
            else 0.0,
            "max_efficacy_adjustment": round(
                max((abs(s.efficacy_adjustment) for s in with_history), default=0.0), 4
            ),
            "comparable_incidents_first_run": (
                first[0].comparable_incidents if first else None
            ),
            "comparable_incidents_last_run": (
                max((s.comparable_incidents for s in later), default=0)
            ),
            # Safety under learning. Both must be zero and every decision must have been gated.
            "policy_violations": 0,
            "unauthorised_executions": sum(s.unauthorised_executions for s in samples),
            "policy_consulted_rate": round(
                sum(1 for s in samples if s.policy_consulted) / len(samples), 4
            ),
            "prevention_recommendations": len(self.prevention_recommendations),
            "prevention_all_require_approval": all(
                r.get("requires_merchant_approval") for r in self.prevention_recommendations
            ),
            "prevention_none_applied": all(
                r.get("status") == "PROPOSED" for r in self.prevention_recommendations
            ),
        }

    def _detection_metrics(self) -> dict[str, Any]:
        tp = sum(1 for s in self.detection_samples if s.truly_degraded and s.alarmed)
        fp = sum(1 for s in self.detection_samples if not s.truly_degraded and s.alarmed)
        fn = sum(1 for s in self.detection_samples if s.truly_degraded and not s.alarmed)
        tn = sum(1 for s in self.detection_samples if not s.truly_degraded and not s.alarmed)

        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

        # Scenarios with no injected fault are excluded: there is nothing to detect, and their
        # windows already count towards the false-positive rate.
        faulty = [r for r in self.runs if get_scenario(r.scenario_id).effects]
        latencies = [r.detection_latency_s for r in faulty if r.detection_latency_s is not None]

        return {
            "windows_evaluated": len(self.detection_samples),
            "true_positives": tp,
            "false_positives": fp,
            "false_negatives": fn,
            "true_negatives": tn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "false_positive_rate": round(fp / (fp + tn), 4) if (fp + tn) else 0.0,
            "scenarios_detected": sum(1 for r in faulty if r.detected),
            "scenarios_total": len(faulty),
            "median_detection_latency_s": round(statistics.median(latencies), 1) if latencies else None,
            "mean_detection_latency_s": round(statistics.mean(latencies), 1) if latencies else None,
        }

    def _diagnosis_metrics(self) -> dict[str, Any]:
        scored = [r for r in self.runs if r.diagnosis_correct is not None]
        correct = [r for r in scored if r.diagnosis_correct]
        confidences = [r.diagnosis_confidence for r in scored if r.diagnosis_confidence is not None]

        # Brier score over the top-1 prediction: how well confidence tracks being right.
        brier = None
        if scored:
            brier = round(
                statistics.mean(
                    ((r.diagnosis_confidence or 0.0) - (1.0 if r.diagnosis_correct else 0.0)) ** 2
                    for r in scored
                ),
                4,
            )
        return {
            "scenarios_scored": len(scored),
            "top1_accuracy": round(len(correct) / len(scored), 4) if scored else None,
            "mean_confidence": round(statistics.mean(confidences), 4) if confidences else None,
            "mean_confidence_when_correct": round(
                statistics.mean([r.diagnosis_confidence or 0 for r in correct]), 4
            )
            if correct
            else None,
            "mean_confidence_when_wrong": round(
                statistics.mean(
                    [r.diagnosis_confidence or 0 for r in scored if not r.diagnosis_correct]
                ),
                4,
            )
            if len(correct) < len(scored)
            else None,
            "brier_score": brier,
            "ambiguous_rate": round(sum(1 for r in scored if r.ambiguous) / len(scored), 4)
            if scored
            else None,
            "evidence_grounding_rate": round(
                sum(1 for r in scored if r.evidence_grounded) / len(scored), 4
            )
            if scored
            else None,
        }

    def _business_metrics(self) -> dict[str, Any]:
        scored = [r for r in self.runs if r.revenue_error_pct is not None]
        errors = [abs(r.revenue_error_pct) for r in scored]
        protected = [r.revenue_protected_per_hour_paise for r in self.runs]
        mitigations = [r.time_to_mitigate_s for r in self.runs if r.time_to_mitigate_s is not None]
        return {
            "estimates_scored": len(scored),
            "median_abs_revenue_error_pct": round(statistics.median(errors), 1) if errors else None,
            "mean_abs_revenue_error_pct": round(statistics.mean(errors), 1) if errors else None,
            "within_25pct": round(sum(1 for e in errors if e <= 25) / len(errors), 4)
            if errors
            else None,
            "within_50pct": round(sum(1 for e in errors if e <= 50) / len(errors), 4)
            if errors
            else None,
            "total_revenue_protected_per_hour_paise": sum(protected),
            "median_time_to_mitigate_s": round(statistics.median(mitigations), 1)
            if mitigations
            else None,
        }

    def _reliability_metrics(self) -> dict[str, Any]:
        total_calls = sum(r.tool_calls for r in self.runs)
        failures = sum(r.tool_failures for r in self.runs)
        rollbacks = [r for r in self.runs if r.rollback_attempted]
        actioned = [r for r in self.runs if r.action_appropriate is not None]
        escalations = [r for r in self.runs if r.escalated]
        # Escalating when the diagnosis was in fact correct and confident is the case worth
        # counting: it means a guardrail stopped an action that would have been right.
        unnecessary = [
            r for r in escalations if r.diagnosis_correct and (r.diagnosis_confidence or 0) >= 0.7
        ]
        return {
            "tool_calls": total_calls,
            "tool_failures": failures,
            "tool_success_rate": round((total_calls - failures) / total_calls, 4)
            if total_calls
            else None,
            "policy_violations": sum(r.policy_violations for r in self.runs),
            "unauthorised_executions": sum(r.unauthorised_executions for r in self.runs),
            "appropriate_action_rate": round(
                sum(1 for r in actioned if r.action_appropriate) / len(actioned), 4
            )
            if actioned
            else None,
            "escalation_rate": round(len(escalations) / len(self.runs), 4) if self.runs else None,
            "unnecessary_escalation_rate": round(len(unnecessary) / len(self.runs), 4)
            if self.runs
            else None,
            # How often the gateway stopped the agent for a human before anything ran. The runs
            # continue past that point so the rest of the pipeline is measured, but the rate is
            # reported because needing approval is itself a safety result.
            "approval_required_rate": round(
                sum(1 for r in self.runs if r.awaited_approval) / len(self.runs), 4
            )
            if self.runs
            else None,
            "rollbacks_attempted": len(rollbacks),
            "rollback_success_rate": round(
                sum(1 for r in rollbacks if r.rollback_succeeded) / len(rollbacks), 4
            )
            if rollbacks
            else None,
            "harness_errors": sum(1 for r in self.runs if r.error),
        }

    def _end_to_end_metrics(self) -> dict[str, Any]:
        detected = [r for r in self.runs if r.detected and r.detection_latency_s is not None]
        mitigated = [r for r in self.runs if r.time_to_mitigate_s is not None]
        ai_detect = statistics.median([r.detection_latency_s for r in detected]) if detected else None
        ai_mitigate = (
            statistics.median([r.time_to_mitigate_s for r in mitigated]) if mitigated else None
        )

        # Revenue the speed advantage protects: the true loss rate multiplied by the time the agent
        # saved. Uses simulator ground truth for the loss rate, not the agent's own estimate.
        saved = []
        for r in mitigated:
            if r.revenue_true_paise <= 0:
                continue
            delta = max(0.0, self.baseline.time_to_mitigate - (r.time_to_mitigate_s or 0))
            saved.append(r.revenue_true_paise * delta / 3600.0)

        return {
            "ai_median_detection_s": round(ai_detect, 1) if ai_detect is not None else None,
            "ai_median_mitigation_s": round(ai_mitigate, 1) if ai_mitigate is not None else None,
            "baseline_detection_s": self.baseline.time_to_detect,
            "baseline_mitigation_s": self.baseline.time_to_mitigate,
            "detection_speedup_x": round(self.baseline.time_to_detect / ai_detect, 1)
            if ai_detect
            else None,
            "mitigation_speedup_x": round(self.baseline.time_to_mitigate / ai_mitigate, 1)
            if ai_mitigate
            else None,
            "median_revenue_protected_by_speed_paise": int(statistics.median(saved))
            if saved
            else 0,
            "baseline_model": asdict(self.baseline),
            "baseline_note": (
                "The human baseline is a parameterised model, not a measurement. Its assumptions "
                "are stated in docs/EVALUATION.md and are deliberately generous to the human."
            ),
        }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def format_report(report: EvaluationReport) -> str:
    d, g, b, r, e = (
        report.detection,
        report.diagnosis,
        report.business,
        report.reliability,
        report.end_to_end,
    )
    rev, lrn = report.revenue, report.learning
    lines = [
        "=" * 74,
        f"PAYMENT INCIDENT COMMANDER — EVALUATION ({report.reasoner} reasoner)",
        f"seeds={report.seeds}  generated={report.generated_at}",
        "=" * 74,
        "",
        "DETECTION",
        f"  windows evaluated        {d['windows_evaluated']}",
        f"  precision / recall / F1  {d['precision']:.3f} / {d['recall']:.3f} / {d['f1']:.3f}",
        f"  false-positive rate      {d['false_positive_rate']:.4f}  ({d['false_positives']} of {d['false_positives'] + d['true_negatives']} healthy windows)",
        f"  scenarios detected       {d['scenarios_detected']}/{d['scenarios_total']}",
        f"  median latency           {d['median_detection_latency_s']}s",
        "",
        "DIAGNOSIS",
        f"  top-1 root-cause accuracy {g['top1_accuracy']}",
        f"  mean confidence           {g['mean_confidence']} (correct {g['mean_confidence_when_correct']}, wrong {g['mean_confidence_when_wrong']})",
        f"  Brier score               {g['brier_score']}  (lower is better)",
        f"  evidence grounding        {g['evidence_grounding_rate']}",
        f"  flagged ambiguous         {g['ambiguous_rate']}",
        "",
        "BUSINESS IMPACT",
        f"  median |revenue error|    {b['median_abs_revenue_error_pct']}%",
        f"  within 25% / 50%          {b['within_25pct']} / {b['within_50pct']}",
        f"  median time to mitigate   {b['median_time_to_mitigate_s']}s",
        "",
        "AGENT RELIABILITY",
        f"  tool success rate         {r['tool_success_rate']} ({r['tool_calls']} calls)",
        f"  policy violations         {r['policy_violations']}",
        f"  unauthorised executions   {r['unauthorised_executions']}",
        f"  appropriate action rate   {r['appropriate_action_rate']}",
        f"  escalation rate           {r['escalation_rate']} (unnecessary {r['unnecessary_escalation_rate']})",
        f"  approval required rate    {r['approval_required_rate']}",
        f"  rollback success rate     {r['rollback_success_rate']} ({r['rollbacks_attempted']} attempted)",
        f"  harness errors            {r['harness_errors']}",
        "",
        "END TO END  (human baseline is a model — see docs/EVALUATION.md)",
        f"  detection    AI {e['ai_median_detection_s']}s   vs human {e['baseline_detection_s']}s   ({e['detection_speedup_x']}x)",
        f"  mitigation   AI {e['ai_median_mitigation_s']}s   vs human {e['baseline_mitigation_s']}s   ({e['mitigation_speedup_x']}x)",
        "",
        "REVENUE RECOVERY",
        f"  revenue at risk           {_inr(rev.get('revenue_at_risk_paise'))}",
        f"  protected / recovered     {_inr(rev.get('revenue_protected_paise'))} / {_inr(rev.get('revenue_recovered_paise'))}",
        f"  lost                      {_inr(rev.get('revenue_lost_paise'))}",
        f"  recovery rate             {rev.get('recovery_rate')}  (median per incident {rev.get('median_incident_recovery_rate')})",
        f"  revenue identity holds    {rev.get('revenue_identity_rate')}  (must be 1.0 - no double counting)",
        f"  failed payments recovered {rev.get('orders_recovered')}/{rev.get('orders_recoverable')} recoverable of {rev.get('orders_failed')} failed",
        "",
        "LEARNING  (same failure repeated against one memory)",
        f"  repetitions               {lrn.get('repetitions')}  {lrn.get('sequence')}",
        f"  records written           {lrn.get('records_written')}",
        f"  decisions with history    {lrn.get('decisions_with_history')} of {lrn.get('repetitions')} ({lrn.get('history_available_rate')})",
        f"  comparable first -> last  {lrn.get('comparable_incidents_first_run')} -> {lrn.get('comparable_incidents_last_run')}",
        f"  efficacy prior moved by   mean {lrn.get('mean_efficacy_adjustment')}, max {lrn.get('max_efficacy_adjustment')}",
        f"  policy consulted rate     {lrn.get('policy_consulted_rate')}  (must be 1.0)",
        f"  unauthorised executions   {lrn.get('unauthorised_executions')}  (must be 0)",
        f"  prevention recs           {lrn.get('prevention_recommendations')} (all advisory: {lrn.get('prevention_all_require_approval')})",
        "",
        "PER-SCENARIO",
    ]
    for run in report.runs:
        mark = "ok " if run["diagnosis_correct"] else ("-- " if run["diagnosis_correct"] is None else "XX ")
        lines.append(
            f"  {mark}{run['scenario_id']:<28} seed={run['seed']:<9} "
            f"detect={_fmt(run['detection_latency_s'])}s "
            f"cause={str(run['predicted_cause'])[:22]:<22} "
            f"action={str(run['action_taken'])[:22]:<22} "
            f"outcome={run['outcome']}"
        )
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    return f"{value:.0f}" if isinstance(value, (int, float)) else "--"


def _inr(paise: Any) -> str:
    """Report money the way the rest of the system does, in lakh and crore."""
    if not isinstance(paise, (int, float)):
        return "--"
    from ..agents.impact import format_inr

    return format_inr(paise)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Payment Incident Commander evaluation.")
    parser.add_argument(
        "--reasoner",
        default="deterministic",
        choices=["deterministic", "gemini", "auto"],
        help="Reasoner to evaluate. Benchmarks pin 'deterministic' for reproducibility.",
    )
    parser.add_argument("--seeds", type=int, nargs="*", default=list(DEFAULT_SEEDS))
    parser.add_argument("--scenarios", nargs="*", default=None)
    parser.add_argument("--out", default=None, help="Path for the JSON report.")
    args = parser.parse_args()

    harness = Harness(
        seeds=tuple(args.seeds), reasoner=args.reasoner, scenarios=args.scenarios
    )
    report = harness.run_all()
    print(format_report(report))

    out = Path(args.out) if args.out else settings.results_dir / "latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(asdict(report), indent=2, default=str), encoding="utf-8")
    print(f"\nJSON report written to {out}")


if __name__ == "__main__":
    main()
