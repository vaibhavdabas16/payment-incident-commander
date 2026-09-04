"""Incident Supervisor.

An explicit finite state machine (ADR-005). No agent decides what happens next; each agent only
produces the output for its own state, and this class owns every transition. That makes the
workflow auditable, testable, and impossible to talk out of its safety properties.

Two invariants are enforced here and asserted in tests:

* `EXECUTING` is reachable only from a `PolicyDecision` with `approved is True`.
* Interventions per incident are capped. After the cap, the incident escalates rather than
  continuing to act on a live payment system — an agent that retries indefinitely is worse than
  one that stops and asks.

The supervisor is also the only component that advances simulated time, via an injected clock. That
matters for verification: the system must genuinely wait and observe real post-intervention traffic
before judging its own work.

Two states close the loop:

* `RECOVERY_PLANNING` sits between diagnosis and decision. The Recovery Strategy Agent prices the
  option set there and attaches what memory says about each option, so the Decision Agent chooses
  between priced, evidenced alternatives rather than generating one.
* `RECOVERING_ORDERS` sits after resolution. Fixing the routing stops further loss; this is where
  the payments already lost are gone after, under its own policy decision.

And `LEARNING` is no longer a pass-through. Every incident that closes — resolved, escalated,
rolled back or acknowledged — is priced, reduced to a structured `IncidentOutcomeRecord`, written
to memory, and folded into the merchant's recurring-failure patterns. That is the only way a
future incident can be handled better than this one was.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from ..config import settings
from ..memory.build import build_outcome_record
from ..memory.patterns import find_patterns
from ..revenue import compute_revenue_outcome
from ..schemas import (
    ActionType,
    AgentStep,
    AnomalySignal,
    IncidentRecord,
    IncidentState,
    PolicyOutcome,
    PreventionRecommendation,
    VerificationStatus,
)
from .action import ActionAgent
from .base import IncidentContext
from .decision import DecisionAgent
from .detection import DetectionAgent
from ..schemas import NON_REMEDIAL_ACTIONS
from .escalation import NON_OVERRIDABLE_RULE_FAMILIES, EscalationAgent, _rule_family
from .impact import ImpactAgent
from .investigation import InvestigationAgent
from .recovery import OrderRecoveryAgent
from .root_cause import RootCauseAgent
from .strategy import RecoveryStrategyAgent, route_health
from .verification import VerificationAgent

# How many times to keep waiting when verification cannot yet measure anything.
MAX_INCONCLUSIVE_RETRIES = 2

# After an incident closes, the same signature is suppressed for this long. Long enough to avoid
# immediately reopening a fault a human is already looking at; short enough that a genuinely new
# degradation of the same segment is still caught.
SUPPRESSION_SECONDS = 900

# A partial recovery leaving more than this much of a shortfall justifies another attempt.
PARTIAL_RETRY_GAP = 0.05

# How many times to wait for more evidence before giving up on explaining an incident.
MAX_DIAGNOSIS_RETRIES = 2

_SEVERITY_ORDER = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]

@dataclass
class Clock:
    """Time source. In the demo it advances the simulator; in tests it can be instantaneous.

    `wait` is a deliberate observation pause - the system stands back and lets traffic accumulate.
    `advance` is bookkeeping for time that has already passed while an agent was thinking. Both move
    the simulated clock forward; keeping them separate keeps the distinction legible in the code.
    """

    now: Callable[[], datetime]
    wait: Callable[[float], None]
    advance: Callable[[float], None] | None = None

    def __post_init__(self) -> None:
        if self.advance is None:
            self.advance = self.wait


class IncidentSupervisor:
    def __init__(
        self,
        store: Any,
        detector: Any,
        registry: Any,
        reasoner: Any,
        gateway: Any,
        clock: Clock,
        memory: Any = None,
        control: Any = None,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
        step_cost_s: float | None = None,
        step_pause_s: float = 0.0,
    ) -> None:
        self.store = store
        self.detector = detector
        self.registry = registry
        self.reasoner = reasoner
        self.gateway = gateway
        self.clock = clock
        self.memory = memory
        self.control = control
        self.emit = emit
        # Simulated seconds charged for each agent step. `None` charges the step's measured wall
        # time, which is what the live dashboard wants - an agent really does hold the incident
        # open while it thinks. A benchmark wants the opposite: charging real time makes the
        # simulated clock depend on how busy the machine is, so the same seed advances different
        # distances on a loaded box and borderline runs change outcome between runs.
        self.step_cost_s = step_cost_s
        # Real-time pacing for the live dashboard only; see IncidentContext.step_pause_s.
        self.step_pause_s = step_pause_s

        self.detection_agent = DetectionAgent()
        self.investigation_agent = InvestigationAgent()
        self.impact_agent = ImpactAgent()
        self.root_cause_agent = RootCauseAgent()
        self.strategy_agent = RecoveryStrategyAgent()
        self.decision_agent = DecisionAgent()
        self.action_agent = ActionAgent()
        self.verification_agent = VerificationAgent()
        self.order_recovery_agent = OrderRecoveryAgent()
        self.escalation_agent = EscalationAgent()

        self.incidents: list[IncidentRecord] = []
        self._incident_seq = 0
        self._route_health: dict[str, dict[str, float]] = {}
        self._executed_at: dict[str, datetime] = {}
        # The last action actually executed per incident, retained across a rollback so learning
        # records the intervention that failed rather than losing it with the revert.
        self._executed_action: dict[str, Any] = {}
        self._diagnosis_retries: dict[str, int] = {}
        # Incidents whose diagnosis has already been reviewed once after acting.
        self._reviewed: set[str] = set()
        # Incidents whose failed payments have already been swept for recovery.
        self._recovery_attempted: set[str] = set()
        # Recurring-failure patterns mined from memory, refreshed as incidents close. Advisory:
        # a recommendation is a document, and nothing here can change merchant policy (ADR-011).
        self.prevention: list[PreventionRecommendation] = []
        # Acknowledgements survive the pattern list being recomputed, so a recommendation a person
        # has already dealt with does not reappear as new every time an incident closes.
        self._prevention_status: dict[str, tuple[str, str]] = {}

    # ----------------------------------------------------------------- setup

    def _context(self, incident: IncidentRecord) -> IncidentContext:
        ctx = IncidentContext(
            incident=incident,
            store=self.store,
            registry=self.registry,
            reasoner=self.reasoner,
            gateway=self.gateway,
            detector=self.detector,
            now=self.clock.now(),
            memory=self.memory,
            control=self.control,
            emit=self.emit,
            step_pause_s=self.step_pause_s,
            charge_time=(
                self.clock.advance
                if self.step_cost_s is None
                else lambda _measured: self.clock.advance(self.step_cost_s)
            ),
        )
        # The registry shares the context's clock and incident id so tool results are windowed
        # consistently and every call is attributed to the right incident.
        self.registry.context.now = ctx.now
        self.registry.context.incident_id = incident.incident_id
        return ctx

    def _transition(self, incident: IncidentRecord, state: IncidentState) -> None:
        previous = incident.state
        incident.state = state
        if self.emit:
            self.emit(
                "state_changed",
                {
                    "incident_id": incident.incident_id,
                    "from": previous.value,
                    "to": state.value,
                    "at": self.clock.now().isoformat(),
                },
            )

    # ------------------------------------------------------------- detection

    def observe(self) -> IncidentRecord | None:
        """Run one detection cycle. Returns a new incident if one opened."""
        placeholder = IncidentRecord(
            incident_id="PENDING",
            merchant_id=settings.simulation.merchant_id,
            state=IncidentState.OBSERVING,
            severity=_default_severity(),
            opened_at=self.clock.now(),
        )
        ctx = self._context(placeholder)
        step = self.detection_agent.execute(ctx)
        signal: AnomalySignal | None = ctx.scratch["detection_result"].output

        if signal is None:
            return None

        # A degradation lasts many monitoring cycles, and the detector will keep firing for every
        # one of them. Opening a fresh incident each time produces an incident storm: dozens of
        # duplicates for one fault, a revenue-at-risk figure summed over all of them, and - worst -
        # the merchant's hourly action budget spent on the same problem until every later incident
        # escalates for rate limiting. Repeat detections are correlated into the incident that is
        # already tracking them.
        existing = self._correlate(signal)
        if existing is not None:
            existing.correlated_detections += 1
            if _severity_rank(signal.severity) > _severity_rank(existing.severity):
                existing.severity = signal.severity
            if self.emit:
                self.emit(
                    "detection_correlated",
                    {
                        "incident_id": existing.incident_id,
                        "signature": existing.signature,
                        "detections": existing.correlated_detections,
                    },
                )
            return None

        return self.observe_from_signal(signal, step)

    def _correlate(self, signal: AnomalySignal) -> IncidentRecord | None:
        """Find an incident that already covers this signal, if any.

        Matched on *overlap* between affected segments rather than on the worst-hit segment being
        identical. During a live degradation the ranking reshuffles between cycles - one minute the
        top segment is `upi & psp_axis`, the next it is `route_A`, then `upi` - so exact matching
        recognises almost nothing and the system opens a new incident every cycle for one fault.

        Two cases count as the same incident: one still open, and one that closed very recently.
        The second matters because a degradation usually continues after the agent has finished
        responding; without a suppression window the system would immediately reopen, re-diagnose
        and re-act on a fault it has already handed to a human.
        """
        keys = _segment_keys(signal)
        if not keys:
            return None

        for incident in reversed(self.incidents):
            if not keys & set(incident.segment_keys):
                continue
            if incident.state is not IncidentState.CLOSED:
                return incident
            if incident.closed_at is None:
                continue
            if (self.clock.now() - incident.closed_at).total_seconds() > SUPPRESSION_SECONDS:
                continue
            # A materially worse degradation deserves a fresh look even inside the window.
            if _severity_rank(signal.severity) > _severity_rank(incident.severity) + 1:
                return None
            return incident
        return None

    def observe_from_signal(
        self, signal: AnomalySignal, step: AgentStep | None = None
    ) -> IncidentRecord:
        """Open an incident from a detection signal produced elsewhere.

        The evaluation harness evaluates the detector itself, window by window, so that precision
        and recall have a denominator. It then needs to open an incident from the signal it already
        holds rather than detecting a second time, which would double-count and could even produce
        a different result.
        """
        if step is None:
            placeholder = IncidentRecord(
                incident_id=signal.incident_id,
                merchant_id=settings.simulation.merchant_id,
                state=IncidentState.OBSERVING,
                severity=signal.severity,
                opened_at=signal.detected_at,
            )
            ctx = self._context(placeholder)
            step = self.detection_agent.execute(ctx)

        # Incident ids have to be unique within the memory this supervisor writes to, not merely
        # within this supervisor. The closed-loop demo runs several worlds against one shared
        # memory, and without this every world's first incident is INC-0001 and each one silently
        # overwrites the last - the system appears to learn nothing while working perfectly.
        self._incident_seq += 1
        while self.memory is not None and self.memory.get(f"INC-{self._incident_seq:04d}"):
            self._incident_seq += 1
        signal = signal.model_copy(update={"incident_id": f"INC-{self._incident_seq:04d}"})

        # Anchor the baseline: from here until the incident closes, degraded windows must not be
        # absorbed into "normal".
        self.detector.mark_degraded_from(signal.window_start)

        incident = IncidentRecord(
            incident_id=signal.incident_id,
            merchant_id=settings.simulation.merchant_id,
            state=IncidentState.DETECTED,
            severity=signal.severity,
            opened_at=signal.detected_at,
            title=_title(signal),
            signature=_signature(signal),
            segment_keys=sorted(_segment_keys(signal)),
            anomaly=signal,
            steps=[_reattribute(step, signal.incident_id)],
        )
        self.incidents.append(incident)
        if self.emit:
            self.emit(
                "incident_opened",
                {
                    "incident_id": incident.incident_id,
                    "severity": incident.severity.value,
                    "title": incident.title,
                    "anomaly": signal.model_dump(mode="json"),
                },
            )
        return incident

    # -------------------------------------------------------------- the loop

    def run_incident(self, incident: IncidentRecord, max_steps: int = 40) -> IncidentRecord:
        """Drive an incident to a terminal state."""
        inconclusive = 0
        for _ in range(max_steps):
            if incident.state is IncidentState.CLOSED:
                break
            if incident.state is IncidentState.AWAITING_HUMAN_APPROVAL:
                # Unattended runs do not fabricate an approval; the incident is handed over.
                break

            if incident.state is IncidentState.DETECTED:
                self._step_investigate(incident)
            elif incident.state is IncidentState.INVESTIGATING:
                self._step_impact(incident)
            elif incident.state is IncidentState.IMPACT_ASSESSED:
                self._step_diagnose(incident)
            elif incident.state is IncidentState.DIAGNOSING:
                self._step_plan_recovery(incident)
            elif incident.state is IncidentState.RECOVERY_PLANNING:
                self._step_decide(incident)
            elif incident.state is IncidentState.DECIDING:
                self._step_policy(incident)
            elif incident.state is IncidentState.POLICY_REVIEW:
                self._step_execute(incident)
            elif incident.state is IncidentState.EXECUTING:
                inconclusive = self._step_verify(incident, inconclusive)
            elif incident.state is IncidentState.VERIFYING:
                inconclusive = self._step_verify(incident, inconclusive)
            elif incident.state is IncidentState.ROLLING_BACK:
                self._step_rollback(incident)
            elif incident.state is IncidentState.ESCALATED:
                self._step_escalate(incident)
            elif incident.state is IncidentState.RESOLVED:
                self._review_for_a_second_fault(incident)
                self._step_recover_orders(incident)
            elif incident.state is IncidentState.RECOVERING_ORDERS:
                self._step_learn(incident)
            elif incident.state is IncidentState.LEARNING:
                self._step_close(incident)
            else:
                break
        return incident

    # ----------------------------------------------------------------- steps

    def _step_investigate(self, incident: IncidentRecord) -> None:
        ctx = self._context(incident)
        self.investigation_agent.execute(ctx)
        result = ctx.scratch["investigation_result"]
        if not result.ok:
            return self._escalate(incident, "agent_failure")
        incident.evidence = result.output
        self._transition(incident, IncidentState.INVESTIGATING)

    def _step_impact(self, incident: IncidentRecord) -> None:
        ctx = self._context(incident)
        self.impact_agent.execute(ctx)
        result = ctx.scratch["impact_result"]
        if not result.ok:
            # Never guess at money. If impact cannot be computed, a human decides.
            return self._escalate(incident, "agent_failure")
        incident.impact = result.output
        self._transition(incident, IncidentState.IMPACT_ASSESSED)

    def _step_diagnose(self, incident: IncidentRecord) -> None:
        ctx = self._context(incident)
        self.root_cause_agent.execute(ctx)
        result = ctx.scratch["root_cause_result"]

        if not result.ok or result.output is None:
            # No hypothesis fits the evidence. Early in an incident this usually means detection
            # fired on a real but still-weak signal - the segment tier can see a degradation before
            # there is enough of it to attribute. Escalating immediately would hand a human an
            # incident with no diagnosis attached, when waiting one observation window normally
            # produces a clear answer. Wait and re-investigate; only give up if it stays
            # unexplainable.
            attempts = self._diagnosis_retries.get(incident.incident_id, 0)
            if attempts < MAX_DIAGNOSIS_RETRIES:
                self._diagnosis_retries[incident.incident_id] = attempts + 1
                if self.emit:
                    self.emit(
                        "diagnosis_deferred",
                        {
                            "incident_id": incident.incident_id,
                            "attempt": attempts + 1,
                            "reason": "no hypothesis fits the evidence yet",
                        },
                    )
                self.clock.wait(settings.verification.observation_seconds)
                # Re-detect before re-investigating. The original signal was weak, and every later
                # stage anchors on its segment attribution - the echo test, the primary segment,
                # the affected method. Re-investigating against a stale anomaly produces fresh
                # evidence interpreted through an out-of-date picture of what is broken.
                refreshed = self.detector.evaluate(self.clock.now())
                if refreshed is not None:
                    incident.anomaly = refreshed.model_copy(
                        update={"incident_id": incident.incident_id}
                    )
                    incident.severity = refreshed.severity
                    incident.segment_keys = sorted(
                        set(incident.segment_keys) | _segment_keys(refreshed)
                    )
                self._transition(incident, IncidentState.DETECTED)
                return
            return self._escalate(incident, "no_effective_action")

        incident.root_cause = result.output
        self._transition(incident, IncidentState.DIAGNOSING)

    def _step_plan_recovery(self, incident: IncidentRecord) -> None:
        """Price the options, and retrieve what happened last time each was tried.

        Failing here is not grounds to escalate on its own: the Decision Agent can generate and
        price the same option set from the same inputs, so a strategy-stage failure costs the
        historical evidence rather than the response. Losing the memory lookup should degrade the
        explanation, never stall a live incident.
        """
        ctx = self._context(incident)
        self.strategy_agent.execute(ctx)
        result = ctx.scratch["recovery_strategy_result"]
        if result.ok and result.output is not None:
            incident.recovery_plan = result.output
        # Cached beside the incident rather than on it: IncidentRecord is a strict contract and
        # route health is supervisor bookkeeping, not part of the incident's public shape.
        self._route_health[incident.incident_id] = ctx.scratch.get("route_health") or {}
        self._transition(incident, IncidentState.RECOVERY_PLANNING)

    def _step_decide(self, incident: IncidentRecord) -> None:
        ctx = self._context(incident)
        # Reuse the route health the strategy stage already measured, so the decision is priced
        # against the same picture of the world the options were priced against.
        cached = self._route_health.get(incident.incident_id)
        if cached:
            ctx.scratch["route_health"] = cached
        self.decision_agent.execute(ctx)
        result = ctx.scratch["decision_result"]
        if not result.ok or result.output is None:
            return self._escalate(incident, "no_effective_action")
        incident.proposal = result.output
        self._route_health[incident.incident_id] = ctx.scratch.get("route_health") or cached or {}
        self._transition(incident, IncidentState.DECIDING)

    def _step_policy(self, incident: IncidentRecord) -> None:
        """The gateway runs here, outside any agent, so no agent can influence its verdict."""
        proposal = incident.proposal
        assert proposal is not None
        now = self.clock.now()
        decision = self.gateway.evaluate(
            proposal,
            now=now,
            anomaly=incident.anomaly,
            root_cause=incident.root_cause,
            route_health=self._route_health.get(incident.incident_id)
            or route_health(self._context(incident)),
        )
        incident.policy_decision = decision
        if self.emit:
            self.emit("policy_decision", decision.model_dump(mode="json"))
        self._transition(incident, IncidentState.POLICY_REVIEW)

    def _step_execute(self, incident: IncidentRecord) -> None:
        decision = incident.policy_decision
        proposal = incident.proposal
        assert decision is not None and proposal is not None

        if decision.outcome is PolicyOutcome.DENY:
            return self._escalate(incident, "policy_denied")
        if decision.requires_human:
            # Pausing for approval is not the same as doing nothing. A human has to be told, or an
            # incident sits unattended behind a guardrail that was meant to protect it.
            self._escalate(
                incident, "policy_requires_approval", transition=False
            )
            incident.outcome = "AWAITING_APPROVAL"
            self._transition(incident, IncidentState.AWAITING_HUMAN_APPROVAL)
            if self.emit:
                self.emit(
                    "approval_required",
                    {
                        "incident_id": incident.incident_id,
                        "action": proposal.action.value,
                        "parameters": decision.granted_parameters,
                        "reason": decision.reason,
                    },
                )
            return

        # Deliberately choosing not to intervene is a successful outcome, not a failure. This is
        # the path a traffic-mix change should take.
        if proposal.action is ActionType.NO_ACTION:
            incident.outcome = "NO_ACTION_REQUIRED"
            self._transition(incident, IncidentState.RESOLVED)
            return

        self._execute_now(incident)

    def _execute_now(self, incident: IncidentRecord) -> None:
        decision = incident.policy_decision
        assert decision is not None and decision.approved, "EXECUTING requires an approved decision"

        ctx = self._context(incident)
        self.action_agent.execute(ctx)
        result = ctx.scratch["action_result"]
        incident.attempts += 1

        if not result.ok or result.output is None:
            self.gateway.history.record_failure(incident.incident_id, self.clock.now())
            return self._escalate(incident, "intervention_failed")

        incident.action_result = result.output
        incident.time_to_mitigate_s = (self.clock.now() - incident.opened_at).total_seconds()
        self._executed_at[incident.incident_id] = self.clock.now()
        # Kept separately from `incident.action_result`, which a rollback clears. What was executed
        # is what memory has to learn from, whether or not it survived contact with reality.
        self._executed_action[incident.incident_id] = result.output

        # Some actions never change payment behaviour: notifying the merchant, filing a ticket or
        # watching more closely. Running a statistical recovery test on those would score them as
        # failed interventions and burn a retry attempt, when in truth the system has correctly
        # concluded that the fix belongs to a human. Hand over instead of pretending to verify.
        if incident.proposal is not None and incident.proposal.action in NON_REMEDIAL_ACTIONS:
            incident.outcome = "HANDED_TO_HUMAN"
            return self._escalate(incident, "no_effective_action")

        self._transition(incident, IncidentState.EXECUTING)

    def _step_verify(self, incident: IncidentRecord, inconclusive: int) -> int:
        cfg = settings.verification
        # Genuinely wait and watch. Verification of an intervention that has not had time to take
        # effect is worthless, so the supervisor advances the world before measuring.
        self.clock.wait(cfg.observation_seconds)

        ctx = self._context(incident)
        ctx.scratch["action_executed_at"] = self._executed_at.get(
            incident.incident_id, incident.opened_at
        )
        self.verification_agent.execute(ctx)
        result = ctx.scratch["verification_result"]
        if not result.ok or result.output is None:
            self._escalate(incident, "agent_failure")
            return inconclusive

        verification = result.output
        incident.verification = verification
        if self.emit:
            self.emit("verification", verification.model_dump(mode="json"))

        status = verification.status
        if status in (VerificationStatus.RECOVERED, VerificationStatus.PARTIALLY_RECOVERED):
            incident.outcome = status.value
            incident.revenue_protected_per_hour_paise = (
                verification.estimated_revenue_protected_per_hour_paise
            )
            # A partial recovery that leaves a large shortfall is worth one more push - the policy
            # gateway's cumulative-shift ceiling is what stops this from becoming an endless ratchet.
            residual = verification.baseline_success_rate - verification.after_success_rate
            if (
                status is VerificationStatus.PARTIALLY_RECOVERED
                and residual > PARTIAL_RETRY_GAP
                and incident.attempts < settings.max_intervention_attempts
            ):
                self._transition(incident, IncidentState.DIAGNOSING)
                return inconclusive
            self._transition(incident, IncidentState.RESOLVED)
            return inconclusive

        if status is VerificationStatus.REGRESSED:
            self._transition(incident, IncidentState.ROLLING_BACK)
            return inconclusive

        if status is VerificationStatus.INCONCLUSIVE:
            inconclusive += 1
            if inconclusive > MAX_INCONCLUSIVE_RETRIES:
                self._escalate(incident, "intervention_failed")
            else:
                self._transition(incident, IncidentState.VERIFYING)
            return inconclusive

        # FAILED. Undo the change before doing anything else. An intervention that did not work is
        # not harmless: it leaves the payment system in a modified state for no benefit, and any
        # second attempt would then be measured against a configuration nobody chose. Only
        # REGRESSED implies the action caused damage, but both cases must leave the system as they
        # found it.
        self.gateway.history.record_failure(incident.incident_id, self.clock.now())
        reverted = self._revert_action(incident)
        if not reverted:
            return self._escalate(incident, "rollback_failed") or inconclusive

        if incident.attempts < settings.max_intervention_attempts:
            self._transition(incident, IncidentState.DIAGNOSING)
        else:
            self._escalate(incident, "attempts_exhausted")
        return inconclusive

    def _step_rollback(self, incident: IncidentRecord) -> None:
        """Regression path: the intervention caused harm, so revert and hand to a human."""
        reverted = self._revert_action(incident)
        self._escalate(incident, "intervention_regressed" if reverted else "rollback_failed")

    def _revert_action(self, incident: IncidentRecord) -> bool:
        """Replay the recorded inverse of the executed action. Returns whether it succeeded."""
        ctx = self._context(incident)
        result = self.action_agent.rollback(ctx)
        # `rollback` is invoked directly rather than through `execute`, so record the step by hand.
        incident.steps.append(
            AgentStep(
                step_id=f"step_rollback_{len(incident.steps)}",
                incident_id=incident.incident_id,
                agent="action",
                state=IncidentState.ROLLING_BACK,
                started_at=ctx.now,
                ended_at=self.clock.now(),
                latency_ms=0.0,
                ok=result.ok,
                summary=result.summary,
                output={},
                tool_calls=self.registry.take_calls(),
                error=result.error,
            )
        )
        if self.emit:
            self.emit(
                "rollback",
                {"incident_id": incident.incident_id, "ok": result.ok, "summary": result.summary},
            )
        if result.ok:
            # The reverted change no longer counts against the cumulative-shift ceiling.
            incident.action_result = None
        return result.ok

    def _step_escalate(self, incident: IncidentRecord) -> None:
        ctx = self._context(incident)
        self.escalation_agent.execute(ctx)
        result = ctx.scratch["escalation_result"]
        incident.escalation = result.output
        if incident.outcome is None:
            incident.outcome = "ESCALATED"
        self._transition(incident, IncidentState.LEARNING)

    def _review_for_a_second_fault(self, incident: IncidentRecord) -> None:
        """Look again once the action has been taken, with everything the incident has accumulated.

        A second, independent fault frequently cannot be established when the first decision has
        to be made. `SCN-MULTI` is the case in point: its issuer fault needs roughly six minutes
        of traffic to clear the significance bar, and the decision happens after two. Waiting for
        it before deciding would delay mitigating the fault that *is* established - an earlier
        attempt to do exactly that pushed every incident past its approval and broke the revert
        path entirely.

        So the review happens *after* acting and verifying. The mitigation timeline is untouched;
        what improves is the diagnosis handed to the human, which is the thing a second fault
        actually changes. Evidence is pooled from onset rather than taken from a two-minute
        window, because pooled evidence grows monotonically with the incident while a sliding
        window flickers in and out of significance.

        Only a genuine upgrade is kept: if the wider look does not conclude multi-factor, the
        original diagnosis stands untouched.
        """
        if incident.incident_id in self._reviewed:
            return
        self._reviewed.add(incident.incident_id)
        if incident.root_cause is None or incident.anomaly is None:
            return
        if incident.root_cause.cause_id == "multi_factor":
            return

        pooled_minutes = int((self.clock.now() - incident.anomaly.window_start).total_seconds() // 60)
        if pooled_minutes <= int(
            (incident.anomaly.window_end - incident.anomaly.window_start).total_seconds() // 60
        ):
            return  # nothing has accumulated yet

        before_evidence = incident.evidence
        before_cause = incident.root_cause

        ctx = self._context(incident)
        ctx.scratch["window_minutes"] = pooled_minutes
        self.investigation_agent.execute(ctx)
        investigation = ctx.scratch.get("investigation_result")
        if investigation is None or not investigation.ok or investigation.output is None:
            incident.evidence = before_evidence
            return

        incident.evidence = investigation.output
        self.root_cause_agent.execute(ctx)
        diagnosis = ctx.scratch.get("root_cause_result")
        revised = diagnosis.output if diagnosis is not None and diagnosis.ok else None

        if revised is None or revised.cause_id != "multi_factor":
            # No second fault established. Leave the original diagnosis exactly as it was.
            incident.evidence = before_evidence
            incident.root_cause = before_cause
            return

        incident.root_cause = revised
        if self.emit:
            self.emit(
                "diagnosis_revised",
                {
                    "incident_id": incident.incident_id,
                    "from": before_cause.cause_id,
                    "to": revised.cause_id,
                    "pooled_window_minutes": pooled_minutes,
                    "reason": "a second independent fault became established after acting",
                },
            )

    def _step_recover_orders(self, incident: IncidentRecord) -> None:
        """Go after the payments that failed while the fault was live.

        Runs once per incident, after the infrastructure question is settled. It is skipped when
        the intervention was reverted or the incident was handed over: chasing customers to
        complete payments through a payment stack that is still broken would be recovering them
        into the same failure.
        """
        if incident.incident_id in self._recovery_attempted:
            return self._transition(incident, IncidentState.RECOVERING_ORDERS)
        self._recovery_attempted.add(incident.incident_id)

        if self._reverted(incident):
            self._transition(incident, IncidentState.RECOVERING_ORDERS)
            return

        ctx = self._context(incident)
        self.order_recovery_agent.execute(ctx)
        result = ctx.scratch.get("order_recovery_result")
        if result is not None and result.output is not None:
            incident.order_recovery = result.output
        self._transition(incident, IncidentState.RECOVERING_ORDERS)

    @staticmethod
    def _reverted(incident: IncidentRecord) -> bool:
        return any(
            str(record.approved_by).startswith("policy_engine:rollback")
            for record in incident.audit
        )

    def _step_learn(self, incident: IncidentRecord) -> None:
        self._transition(incident, IncidentState.LEARNING)

    # ------------------------------------------------------------- learning

    def _learn(self, incident: IncidentRecord) -> None:
        """Price the incident, reduce it to a structured record, and remember it.

        Runs for every incident that closes, not only the ones that went well. An agent that
        remembers only its successes will keep repeating whatever produced them, and the failed
        interventions are the records that stop a future incident making the same bet.
        """
        revenue = compute_revenue_outcome(
            incident,
            now=self.clock.now(),
            executed_at=self._executed_at.get(incident.incident_id),
        )
        incident.revenue = revenue
        record = build_outcome_record(
            incident,
            revenue=revenue,
            route_health=self._route_health.get(incident.incident_id),
            executed=self._executed_action.get(incident.incident_id),
        )
        incident.learning = record

        if self.memory is not None:
            self.memory.record_incident(record)
            self._refresh_prevention(incident.merchant_id)

        if self.emit:
            self.emit(
                "learning",
                {
                    "incident_id": incident.incident_id,
                    "action": record.actual_action_executed,
                    "magnitude": record.intervention_magnitude,
                    "verification": record.verification_result,
                    "succeeded": record.succeeded(),
                    "revenue_at_risk_paise": record.revenue_at_risk_paise,
                    "revenue_protected_paise": record.revenue_protected_paise,
                    "revenue_recovered_paise": record.revenue_recovered_paise,
                    "recovery_rate": record.recovery_rate,
                    "memory_size": len(self.memory) if self.memory is not None else 0,
                },
            )

    def _refresh_prevention(self, merchant_id: str) -> None:
        """Re-mine recurring patterns, preserving what a person has already ruled on."""
        if self.memory is None:
            return
        patterns = find_patterns(self.memory.all(), merchant_id=merchant_id)
        for pattern in patterns:
            previous = self._prevention_status.get(pattern.recommendation_id)
            if previous is not None:
                pattern.status, pattern.acknowledged_by = previous
        others = [p for p in self.prevention if p.merchant_id != merchant_id]
        self.prevention = others + patterns
        if self.emit and patterns:
            self.emit(
                "prevention",
                {
                    "merchant_id": merchant_id,
                    "recommendations": len(patterns),
                    "patterns": [p.pattern for p in patterns[:3]],
                },
            )

    def acknowledge_prevention(
        self, recommendation_id: str, who: str = "human", accept: bool = True
    ) -> PreventionRecommendation:
        """Record a person's decision on a preventive recommendation.

        It records, and that is all it does. Merchant policy lives in `merchant_policies.yaml` and
        is changed by a person editing that file; a system that could widen its own authority
        because a pattern looked convincing would have no guardrails at all. Accepting one here
        means "yes, put this in the policy" — it does not put it there.
        """
        for pattern in self.prevention:
            if pattern.recommendation_id != recommendation_id:
                continue
            pattern.status = "ACKNOWLEDGED" if accept else "DISMISSED"
            pattern.acknowledged_by = who
            self._prevention_status[recommendation_id] = (pattern.status, who)
            if self.emit:
                self.emit(
                    "prevention_acknowledged",
                    {
                        "recommendation_id": recommendation_id,
                        "status": pattern.status,
                        "who": who,
                        "note": (
                            "Recorded as a request. Merchant policy is unchanged and must be "
                            "edited by a person."
                        ),
                    },
                )
            return pattern
        raise ValueError(f"unknown prevention recommendation {recommendation_id}")

    def _step_close(self, incident: IncidentRecord) -> None:
        incident.closed_at = self.clock.now()
        # Let the baseline start learning again only once nothing is open.
        if not any(
            i.state is not IncidentState.CLOSED for i in self.incidents if i is not incident
        ):
            self.detector.mark_recovered(incident.closed_at)
        # The learning work happens here, while the incident is still in LEARNING, because this
        # is the first moment the whole story exists: the close timestamp is what turns per-hour
        # rates into what the incident actually cost and saved.
        self._learn(incident)
        if self.emit:
            self.emit(
                "incident_closed",
                {
                    "incident_id": incident.incident_id,
                    "outcome": incident.outcome,
                    "duration_s": (incident.closed_at - incident.opened_at).total_seconds(),
                    "revenue_protected_per_hour_paise": incident.revenue_protected_per_hour_paise,
                    "revenue_at_risk_paise": (
                        incident.revenue.revenue_at_risk_paise if incident.revenue else 0
                    ),
                    "revenue_protected_paise": (
                        incident.revenue.revenue_protected_paise if incident.revenue else 0
                    ),
                    "revenue_recovered_paise": (
                        incident.revenue.revenue_recovered_paise if incident.revenue else 0
                    ),
                    "recovery_rate": incident.revenue.recovery_rate if incident.revenue else 0.0,
                },
            )
        self._transition(incident, IncidentState.CLOSED)

    def _escalate(
        self, incident: IncidentRecord, reason: str, transition: bool = True
    ) -> None:
        """Hand the incident to a human.

        `transition=False` records and notifies without ending the workflow, which is what an
        approval pause needs: the incident is still live and may yet be approved.
        """
        ctx = self._context(incident)
        ctx.scratch["escalation_reason"] = reason
        self.escalation_agent.execute(ctx)
        incident.escalation = ctx.scratch["escalation_result"].output
        if self.emit:
            self.emit("escalated", {"incident_id": incident.incident_id, "reason": reason})
        if not transition:
            return
        if incident.outcome is None:
            incident.outcome = "ESCALATED"
        self._transition(incident, IncidentState.LEARNING)

    # ------------------------------------------------------- human approval

    def approve(self, incident: IncidentRecord, approver: str = "human") -> IncidentRecord:
        """Human grants the approval the policy gateway required."""
        if incident.state is not IncidentState.AWAITING_HUMAN_APPROVAL:
            raise ValueError(f"incident {incident.incident_id} is not awaiting approval")
        decision = incident.policy_decision
        assert decision is not None
        incident.outcome = None
        incident.policy_decision = decision.model_copy(
            update={
                "approved": True,
                "requires_human": False,
                "outcome": PolicyOutcome.APPROVE,
                "approved_by": approver,
                "reason": f"{decision.reason} | approved by {approver}",
            }
        )
        self._execute_now(incident)
        return self.run_incident(incident)

    @staticmethod
    def _refuse_if_owned(incident: IncidentRecord, verb: str) -> None:
        """A human has this one. Automation does not get to take it back unprompted."""
        if incident.outcome == "ACKNOWLEDGED_BY_HUMAN":
            raise ValueError(
                f"{incident.incident_id} was acknowledged by a person; {verb} would overwrite that "
                f"record and act on an incident somebody already owns"
            )

    def acknowledge(
        self, incident: IncidentRecord, who: str = "human", note: str = ""
    ) -> IncidentRecord:
        """A person has taken this on. Record it and take it off the board.

        Not a resolution and not pretending to be one: the fix may belong to a provider, a deploy
        or another team entirely. What it settles is the question an escalated incident otherwise
        leaves open forever - whether anybody actually picked it up.
        """
        if incident.escalation is None:
            raise ValueError(f"incident {incident.incident_id} was not handed to a human")
        self._refuse_if_owned(incident, "acknowledging again")
        incident.outcome = "ACKNOWLEDGED_BY_HUMAN"
        incident.escalation = incident.escalation.model_copy(
            update={
                "recommended_human_action": (
                    f"{incident.escalation.recommended_human_action} "
                    f"| acknowledged by {who}" + (f": {note}" if note else "")
                ),
                "next_steps": [],
            }
        )
        if self.emit:
            self.emit(
                "acknowledged",
                {"incident_id": incident.incident_id, "who": who, "note": note},
            )
        # An escalated incident is already closed; acknowledging records who owns it now rather
        # than closing it a second time.
        if incident.state is not IncidentState.CLOSED:
            self._step_close(incident)
        elif self.memory is not None:
            # It is already in memory under its previous resolution. Amend that record rather than
            # writing a second one, so the history says who ended up owning the incident.
            self.memory.update_incident(
                incident.incident_id,
                final_resolution="ACKNOWLEDGED_BY_HUMAN",
                human_intervention_required=True,
            )
        return incident

    def override(
        self, incident: IncidentRecord, who: str = "human", reason: str = ""
    ) -> IncidentRecord:
        """Run the action the policy gateway refused, on a named human's authority.

        The gateway is not bypassed, it is answered: a new decision is recorded carrying the
        overridden rules and the person who overrode them. Verification and rollback are unchanged,
        so an override that does not help is still undone.
        """
        self._refuse_if_owned(incident, "overriding")
        decision = incident.policy_decision
        proposal = incident.proposal
        if decision is None or proposal is None:
            raise ValueError(f"incident {incident.incident_id} has no proposed action to override")
        if decision.approved:
            raise ValueError(f"incident {incident.incident_id} was not blocked by policy")
        blocked_by_safety = [
            r for r in decision.bound_by if _rule_family(r) in NON_OVERRIDABLE_RULE_FAMILIES
        ]
        if blocked_by_safety:
            # Refused here as well as hidden in the UI: a rule that protects against harm should
            # not be one API call away from being ignored.
            raise ValueError(
                f"{', '.join(blocked_by_safety)} cannot be overridden - it exists to prevent the "
                f"action causing harm, not to express uncertainty"
            )
        if not reason.strip():
            raise ValueError("an override must say why")

        incident.outcome = None
        incident.attempts = 0
        incident.policy_decision = decision.model_copy(
            update={
                "approved": True,
                "requires_human": False,
                "outcome": PolicyOutcome.APPROVE,
                "approved_by": f"human_override:{who}",
                "reason": (
                    f"{decision.reason} | overridden by {who}: {reason}"
                ),
            }
        )
        if self.emit:
            self.emit(
                "overridden",
                {
                    "incident_id": incident.incident_id,
                    "who": who,
                    "reason": reason,
                    "rules": decision.bound_by,
                },
            )
        self._execute_now(incident)
        return self.run_incident(incident)

    def retry(self, incident: IncidentRecord, who: str = "human") -> IncidentRecord:
        """Look again, now, against the traffic that has arrived since.

        A diagnosis is only ever as good as the window it was made in. An incident that was
        ambiguous when three minutes of evidence existed is often unambiguous with fifteen, and
        re-running costs nothing but a detection sweep.
        """
        if incident.escalation is None:
            raise ValueError(f"incident {incident.incident_id} was not handed to a human")
        self._refuse_if_owned(incident, "retrying")
        refreshed = self.detector.evaluate(self.clock.now())
        if refreshed is not None:
            incident.anomaly = refreshed.model_copy(update={"incident_id": incident.incident_id})
            incident.severity = refreshed.severity
            incident.segment_keys = sorted(set(incident.segment_keys) | _segment_keys(refreshed))
        incident.outcome = None
        incident.escalation = None
        incident.proposal = None
        incident.policy_decision = None
        incident.verification = None
        # The plan was priced against traffic that has since moved on; it is rebuilt from the
        # refreshed anomaly rather than carried over.
        incident.recovery_plan = None
        if self.emit:
            self.emit("retried", {"incident_id": incident.incident_id, "who": who})
        self._transition(incident, IncidentState.DETECTED)
        return self.run_incident(incident)

    def reject(self, incident: IncidentRecord, approver: str = "human") -> IncidentRecord:
        if incident.state is not IncidentState.AWAITING_HUMAN_APPROVAL:
            raise ValueError(f"incident {incident.incident_id} is not awaiting approval")
        incident.outcome = "REJECTED_BY_HUMAN"
        self._escalate(incident, "policy_requires_approval")
        return self.run_incident(incident)


def _segment_keys(signal: AnomalySignal) -> set[str]:
    """Every segment this signal implicates, used to correlate detections across cycles."""
    return {s.segment.key() for s in signal.affected_segments} or {signal.metric}


def _signature(signal: AnomalySignal) -> str:
    """Stable identity for what is broken.

    Keyed on the worst affected segment rather than the incident id, so the same fault detected
    five minutes later is recognised as the same fault.
    """
    if signal.affected_segments:
        return signal.affected_segments[0].segment.key()
    return signal.metric


def _severity_rank(severity) -> int:
    return _SEVERITY_ORDER.index(getattr(severity, "value", str(severity)))


def _title(signal: AnomalySignal) -> str:
    if signal.affected_segments:
        return (
            f"Payment success rate down {abs(signal.deviation):.1%} — "
            f"{signal.affected_segments[0].segment.label()}"
        )
    return f"Payment success rate down {abs(signal.deviation):.1%}"


def _reattribute(step: AgentStep, incident_id: str) -> AgentStep:
    return step.model_copy(update={"incident_id": incident_id})


def _default_severity():
    from ..schemas import Severity

    return Severity.LOW
