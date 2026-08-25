"""Agent base class and shared incident context.

Every agent is a pure function of its context that returns a typed `AgentResult`. The `execute`
wrapper handles timing, tool-call capture, error containment and step recording, so no individual
agent has to remember to do any of it — the audit trail is a property of the framework, not of each
author's diligence.

Failure containment matters more than it looks: an exception inside one agent must degrade that
step, not abort a live incident. A crashed Investigation Agent should still leave the supervisor
able to escalate to a human.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..schemas import AgentResult, AgentStep, IncidentRecord, IncidentState, utcnow


@dataclass
class IncidentContext:
    """Everything an agent may touch. Passed by the supervisor, never constructed by an agent."""

    incident: IncidentRecord
    store: Any
    registry: Any
    reasoner: Any
    gateway: Any
    detector: Any
    now: datetime
    memory: Any = None
    control: Any = None
    # Emits lifecycle events for the dashboard's live stream.
    emit: Callable[[str, dict[str, Any]], None] | None = None
    # Advances simulated time to account for how long an agent step really took.
    charge_time: Callable[[float], None] | None = None
    scratch: dict[str, Any] = field(default_factory=dict)

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        if self.emit is not None:
            self.emit(kind, payload)

    def call_tool(self, name: str, **arguments: Any) -> Any:
        result, _record = self.registry.call(name, arguments)
        return result


class Agent(ABC):
    """One responsibility, one state, one typed output."""

    name: str = "agent"
    state: IncidentState = IncidentState.OBSERVING
    # Seconds after which the step is abandoned and treated as a failure.
    timeout_s: float = 30.0

    @abstractmethod
    def run(self, ctx: IncidentContext) -> AgentResult:
        """Do the work. Raise on unrecoverable failure; `execute` contains it."""

    def execute(self, ctx: IncidentContext) -> AgentStep:
        started_wall = utcnow()
        started = time.perf_counter()
        ctx.publish("agent_started", {"agent": self.name, "state": self.state.value})

        try:
            result = self.run(ctx)
        except Exception as exc:
            result = AgentResult(
                ok=False,
                summary=f"{self.name} failed: {type(exc).__name__}",
                error=f"{type(exc).__name__}: {exc}",
            )

        # Tool calls are drained from the registry rather than reported by the agent, so an agent
        # cannot under-report what it actually did.
        tool_calls = ctx.registry.take_calls() if ctx.registry else []
        result.tool_calls = tool_calls

        step = AgentStep(
            step_id=f"step_{uuid.uuid4().hex[:12]}",
            incident_id=ctx.incident.incident_id,
            agent=self.name,
            state=self.state,
            started_at=started_wall,
            ended_at=utcnow(),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            ok=result.ok,
            summary=result.summary,
            output=_serialise(result.output),
            tool_calls=tool_calls,
            error=result.error,
            reasoner=result.reasoner,
        )
        ctx.incident.steps.append(step)
        ctx.publish(
            "agent_finished",
            {
                "agent": self.name,
                "state": self.state.value,
                "ok": step.ok,
                "summary": step.summary,
                "latency_ms": step.latency_ms,
                "tool_calls": [c.tool for c in tool_calls],
                "error": step.error,
            },
        )
        ctx.scratch[f"{self.name}_result"] = result

        # Charge the incident for the wall-clock time this agent actually took. Without it the
        # whole pipeline appears to complete instantaneously in simulated time, and time-to-mitigate
        # would be reported as zero - flattering, and false. Real reasoning costs real seconds
        # (noticeably more when a model is in the loop), and the merchant is losing revenue
        # throughout, so the clock has to reflect it.
        if ctx.charge_time is not None:
            ctx.charge_time(step.latency_ms / 1000.0)
        return step


def _serialise(output: Any) -> dict[str, Any]:
    if output is None:
        return {}
    if hasattr(output, "model_dump"):
        return output.model_dump(mode="json")
    if isinstance(output, dict):
        return output
    return {"value": str(output)}
