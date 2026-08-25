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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from ..config import settings
from ..schemas import (
    ActionType,
    AgentStep,
    AnomalySignal,
    IncidentRecord,
    IncidentState,
    PolicyOutcome,
    VerificationStatus,
)
from .action import ActionAgent
from .base import IncidentContext
from .decision import DecisionAgent, route_health
from .detection import DetectionAgent
from .escalation import EscalationAgent
from .impact import ImpactAgent
from .investigation import InvestigationAgent
from .root_cause import RootCauseAgent
from .verification import VerificationAgent

# How many times to keep waiting when verification cannot yet measure anything.
MAX_INCONCLUSIVE_RETRIES = 2

# Actions that inform or observe but never change payment behaviour, so there is nothing for the
# Verification Agent to measure.
NON_REMEDIAL_ACTIONS = {
    ActionType.NOTIFY_MERCHANT,
    ActionType.CREATE_INCIDENT_TICKET,
    ActionType.SET_MONITORING_FREQUENCY,
}


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

        self.detection_agent = DetectionAgent()
        self.investigation_agent = InvestigationAgent()
        self.impact_agent = ImpactAgent()
        self.root_cause_agent = RootCauseAgent()
        self.decision_agent = DecisionAgent()
        self.action_agent = ActionAgent()
        self.verification_agent = VerificationAgent()
        self.escalation_agent = EscalationAgent()

        self.incidents: list[IncidentRecord] = []
        self._route_health: dict[str, dict[str, float]] = {}
        self._executed_at: dict[str, datetime] = {}

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
            charge_time=self.clock.advance,
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
        return self.observe_from_signal(signal, step)

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

        incident = IncidentRecord(
            incident_id=signal.incident_id,
            merchant_id=settings.simulation.merchant_id,
            state=IncidentState.DETECTED,
            severity=signal.severity,
            opened_at=signal.detected_at,
            title=_title(signal),
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
            return self._escalate(incident, "no_effective_action")
        incident.root_cause = result.output
        self._transition(incident, IncidentState.DIAGNOSING)

    def _step_decide(self, incident: IncidentRecord) -> None:
        ctx = self._context(incident)
        self.decision_agent.execute(ctx)
        result = ctx.scratch["decision_result"]
        if not result.ok or result.output is None:
            return self._escalate(incident, "no_effective_action")
        incident.proposal = result.output
        # Cached beside the incident rather than on it: IncidentRecord is a strict contract and
        # route health is supervisor bookkeeping, not part of the incident's public shape.
        self._route_health[incident.incident_id] = ctx.scratch.get("route_health") or {}
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
            self.gateway.history.record_failure(self.clock.now())
            return self._escalate(incident, "intervention_failed")

        incident.action_result = result.output
        incident.time_to_mitigate_s = (self.clock.now() - incident.opened_at).total_seconds()
        self._executed_at[incident.incident_id] = self.clock.now()

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
        self.gateway.history.record_failure(self.clock.now())
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

    def _step_learn(self, incident: IncidentRecord) -> None:
        self._transition(incident, IncidentState.LEARNING)

    def _step_close(self, incident: IncidentRecord) -> None:
        incident.closed_at = self.clock.now()
        if self.memory is not None:
            self.memory.record(incident)
        if self.emit:
            self.emit(
                "incident_closed",
                {
                    "incident_id": incident.incident_id,
                    "outcome": incident.outcome,
                    "duration_s": (incident.closed_at - incident.opened_at).total_seconds(),
                    "revenue_protected_per_hour_paise": incident.revenue_protected_per_hour_paise,
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

    def reject(self, incident: IncidentRecord, approver: str = "human") -> IncidentRecord:
        if incident.state is not IncidentState.AWAITING_HUMAN_APPROVAL:
            raise ValueError(f"incident {incident.incident_id} is not awaiting approval")
        incident.outcome = "REJECTED_BY_HUMAN"
        self._escalate(incident, "policy_requires_approval")
        return self.run_incident(incident)


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
