"""Engine — assembles the whole system and drives it.

One place where the simulator, event store, detector, tools, policy gateway, reasoner, memory and
supervisor are wired together, so the demo, the API and the evaluation harness all exercise exactly
the same code path. If the benchmark and the demo ran through different wiring, neither number
would mean anything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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
    # Fixed simulated seconds per agent step. `None` charges measured wall time (live use); the
    # evaluation harness pins it so a run does not depend on how fast the machine is.
    step_cost_s: float | None = None
    # Real seconds to pause between agent steps so a viewer can follow along. Live use only.
    step_pause_s: float = 0.0
    # Live mode: events arrive by ingestion rather than being generated, the clock is the real
    # one, and `control` is where approved actions are actually applied. Without a control plane
    # the system still detects, investigates, prices and diagnoses — it simply cannot act.
    live: bool = False
    control: Any = None
    # The merchant's own limits: which routes traffic may move to, how much may move at once, what
    # needs a human. Defaults to the bundled policy, which describes the simulator.
    policy_path: Path | None = None
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

        self.live = self.config.live
        self.store = EventStore()
        self.simulator = PaymentSimulator(
            self.store, seed=self.config.seed, start_time=self.config.start_time
        )
        # In live mode the simulator is constructed but never advanced: it is not the source of
        # traffic, and reading the clock off a world that never moves would freeze every window.
        self._now: Callable[[], datetime] = (
            (lambda: datetime.now(timezone.utc)) if self.live else (lambda: self.simulator.now)
        )
        control = self.config.control if self.live else self.simulator.control
        if self.live and control is None:
            from .integration.control import ReadOnlyControlPlane

            control = ReadOnlyControlPlane()
        self.control = control
        self.detector = Detector(self.store)
        self.memory = IncidentMemory(persist=self.config.persist_memory)
        self.gateway = PolicyGateway(policy_path=self.config.policy_path)
        self.reasoner = build_reasoner(self.config.reasoner)

        self.tool_context = ToolContext(
            store=self.store,
            now=self._now(),
            control=self.control,
            memory=self.memory,
        )
        self.registry = build_registry(self.tool_context)

        self.clock = Clock(now=self._now, wait=self._wait)
        self.supervisor = IncidentSupervisor(
            store=self.store,
            detector=self.detector,
            registry=self.registry,
            reasoner=self.reasoner,
            gateway=self.gateway,
            clock=self.clock,
            memory=self.memory,
            control=self.control,
            emit=self._emit,
            step_cost_s=self.config.step_cost_s,
            step_pause_s=self.config.step_pause_s,
        )

    # ---------------------------------------------------------------- events

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        event = EngineEvent(kind=kind, payload=payload, at=self._now())
        self.events.append(event)
        if self._external_emit:
            self._external_emit(kind, {**payload, "_at": event.at.isoformat()})

    # ----------------------------------------------------------------- clock

    def _wait(self, seconds: float) -> None:
        """Stand back and let traffic accumulate, then carry on.

        This is what makes verification real: the events the Verification Agent measures are
        produced *after* the Action Agent's change, under whatever configuration it applied. In
        simulation that means generating the traffic for the interval; live it means genuinely
        waiting, while other requests ingest the payments that are actually happening.
        """
        if self.live:
            time.sleep(max(0.0, seconds))
        else:
            self.simulator.advance_seconds(seconds)
        self.tool_context.now = self._now()

    # ------------------------------------------------------------- lifecycle

    def warmup(self, minutes: int | None = None) -> "Engine":
        """Generate history so the detector has a baseline. Simulation only.

        A live deployment earns its baseline the slow way, from real traffic, which is the only
        honest version of it — a synthetic baseline would describe a merchant that does not exist.
        """
        if self.live:
            return self
        self.simulator.warmup(minutes if minutes is not None else self.config.warmup_minutes)
        self.tool_context.now = self._now()
        return self

    def trigger(self, scenario: str | Scenario) -> Scenario:
        if self.live:
            raise RuntimeError("scenarios cannot be injected into a live merchant's traffic")
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
        return self._now()

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

        end = self._now()
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
            "control_plane": self.control.snapshot(),
        }

    def success_rate_series(self, window_seconds: int = 60, count: int = 60) -> list[dict[str, Any]]:
        series = self.store.success_rate_series(self._now(), window_seconds, count)
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
