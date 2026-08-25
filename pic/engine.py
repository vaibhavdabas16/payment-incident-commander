"""Engine — assembles the whole system and drives it.

One place where the simulator, event store, detector, tools, policy gateway, reasoner, memory and
supervisor are wired together, so the demo, the API and the evaluation harness all exercise exactly
the same code path. If the benchmark and the demo ran through different wiring, neither number
would mean anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .agents.supervisor import Clock, IncidentSupervisor
from .config import settings
from .detection.detector import Detector
from .llm.base import build_reasoner
from .memory.store import IncidentMemory
from .policies.gateway import PolicyGateway
from .schemas import IncidentRecord, IncidentState
from .simulation.generator import PaymentSimulator
from .simulation.scenarios import Scenario, get_scenario
from .store import EventStore
from .tools.registry import ToolContext, build_registry


@dataclass
class EngineEvent:
    kind: str
    payload: dict[str, Any]
    at: datetime


@dataclass
class EngineConfig:
    seed: int = 20260824
    warmup_minutes: int = 45
    reasoner: str | None = None
    persist_memory: bool = False
    start_time: datetime = field(
        default_factory=lambda: datetime(2026, 8, 24, 10, 0, 0, tzinfo=timezone.utc)
    )


class Engine:
    def __init__(
        self,
        config: EngineConfig | None = None,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config or EngineConfig()
        self.events: list[EngineEvent] = []
        self._external_emit = emit

        self.store = EventStore()
        self.simulator = PaymentSimulator(
            self.store, seed=self.config.seed, start_time=self.config.start_time
        )
        self.detector = Detector(self.store)
        self.memory = IncidentMemory(persist=self.config.persist_memory)
        self.gateway = PolicyGateway()
        self.reasoner = build_reasoner(self.config.reasoner)

        self.tool_context = ToolContext(
            store=self.store,
            now=self.simulator.now,
            control=self.simulator.control,
            memory=self.memory,
        )
        self.registry = build_registry(self.tool_context)

        self.clock = Clock(now=lambda: self.simulator.now, wait=self._wait)
        self.supervisor = IncidentSupervisor(
            store=self.store,
            detector=self.detector,
            registry=self.registry,
            reasoner=self.reasoner,
            gateway=self.gateway,
            clock=self.clock,
            memory=self.memory,
            control=self.simulator.control,
            emit=self._emit,
        )

    # ---------------------------------------------------------------- events

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        event = EngineEvent(kind=kind, payload=payload, at=self.simulator.now)
        self.events.append(event)
        if self._external_emit:
            self._external_emit(kind, {**payload, "_at": event.at.isoformat()})

    # ----------------------------------------------------------------- clock

    def _wait(self, seconds: float) -> None:
        """Advance simulated time, generating the traffic that occurs during the wait.

        This is what makes verification real: the events the Verification Agent measures are
        produced by the simulator under whatever configuration the Action Agent just applied.
        """
        self.simulator.advance_seconds(seconds)
        self.tool_context.now = self.simulator.now

    # ------------------------------------------------------------- lifecycle

    def warmup(self, minutes: int | None = None) -> "Engine":
        self.simulator.warmup(minutes if minutes is not None else self.config.warmup_minutes)
        self.tool_context.now = self.simulator.now
        return self

    def trigger(self, scenario: str | Scenario) -> Scenario:
        resolved = get_scenario(scenario) if isinstance(scenario, str) else scenario
        self.simulator.activate(resolved)
        self._emit(
            "scenario_triggered",
            {
                "scenario_id": resolved.scenario_id,
                "name": resolved.name,
                "description": resolved.description,
            },
        )
        return resolved

    def advance(self, seconds: float) -> None:
        self._wait(seconds)

    def tick(self, seconds: float = 30.0) -> IncidentRecord | None:
        """Advance time by one monitoring interval, then run a detection cycle.

        Returns a newly opened incident, already driven to a terminal or awaiting-approval state.
        """
        self.advance(seconds)
        incident = self.supervisor.observe()
        if incident is None:
            return None
        return self.supervisor.run_incident(incident)

    def run_until_incident(
        self, max_ticks: int = 40, tick_seconds: float = 30.0
    ) -> IncidentRecord | None:
        for _ in range(max_ticks):
            incident = self.tick(tick_seconds)
            if incident is not None:
                return incident
        return None

    # ------------------------------------------------------------- accessors

    @property
    def now(self) -> datetime:
        return self.simulator.now

    def incidents(self) -> list[IncidentRecord]:
        return self.supervisor.incidents

    def open_incidents(self) -> list[IncidentRecord]:
        return [
            i
            for i in self.supervisor.incidents
            if i.state not in (IncidentState.CLOSED,)
        ]

    def notifications(self) -> list[dict[str, Any]]:
        return self.tool_context.notifications

    def tickets(self) -> list[dict[str, Any]]:
        return self.tool_context.tickets

    def current_metrics(self, window_seconds: int = 120) -> dict[str, Any]:
        """Live headline numbers for the dashboard."""
        from datetime import timedelta

        end = self.simulator.now
        start = end - timedelta(seconds=window_seconds)
        window = self.store.metric_window(start, end)
        baseline = self.detector.baseline(end)
        active = [
            i
            for i in self.supervisor.incidents
            if i.state not in (IncidentState.CLOSED, IncidentState.LEARNING)
        ]
        revenue_at_risk = sum(
            i.impact.revenue_at_risk_per_hour_paise
            for i in active
            if i.impact is not None
        )
        protected = sum(
            i.revenue_protected_per_hour_paise for i in self.supervisor.incidents
        )
        return {
            "timestamp": end.isoformat(),
            "success_rate": round(window.success_rate, 4),
            "baseline_success_rate": round(baseline.ewma_baseline, 4),
            "deviation": round(window.success_rate - baseline.ewma_baseline, 4),
            "transactions": window.total,
            "gmv_paise": window.gmv_paise,
            "p95_latency_ms": round(window.p95_latency_ms),
            "active_incidents": len(active),
            "resolved_incidents": sum(
                1 for i in self.supervisor.incidents if i.outcome in ("RECOVERED", "PARTIALLY_RECOVERED", "NO_ACTION_REQUIRED")
            ),
            "escalated_incidents": sum(
                1 for i in self.supervisor.incidents if i.outcome == "ESCALATED"
            ),
            "revenue_at_risk_per_hour_paise": revenue_at_risk,
            "revenue_protected_per_hour_paise": protected,
            "agent_status": _agent_status(active),
            "control_plane": self.simulator.control.snapshot(),
        }

    def success_rate_series(self, window_seconds: int = 60, count: int = 60) -> list[dict[str, Any]]:
        series = self.store.success_rate_series(self.simulator.now, window_seconds, count)
        return [
            {
                "t": w.start.isoformat(),
                "success_rate": round(w.success_rate, 4),
                "transactions": w.total,
            }
            for w in series
        ]


def _agent_status(active: list[IncidentRecord]) -> str:
    if not active:
        return "Monitoring"
    state = active[-1].state
    return {
        IncidentState.DETECTED: "Investigating...",
        IncidentState.INVESTIGATING: "Assessing impact...",
        IncidentState.IMPACT_ASSESSED: "Diagnosing root cause...",
        IncidentState.DIAGNOSING: "Deciding on action...",
        IncidentState.DECIDING: "Checking merchant policy...",
        IncidentState.POLICY_REVIEW: "Executing intervention...",
        IncidentState.AWAITING_HUMAN_APPROVAL: "Awaiting human approval",
        IncidentState.EXECUTING: "Verifying recovery...",
        IncidentState.VERIFYING: "Verifying recovery...",
        IncidentState.ROLLING_BACK: "Rolling back...",
        IncidentState.ESCALATED: "Escalated to human",
    }.get(state, "Working...")
