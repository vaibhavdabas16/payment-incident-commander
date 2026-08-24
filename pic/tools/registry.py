"""Tool registry.

Every fact an agent asserts must come from a tool call recorded here. Agents never read the event
store directly — that indirection is what makes "never invent evidence" checkable rather than
aspirational, because the evaluation harness can verify each cited finding against the recorded
tool calls.

Two structural guarantees:

* **Ground truth is unreachable.** No tool touches the `ground_truth` table. `tests/` asserts this
  by scanning the tool modules, so the diagnosis-accuracy metric cannot be quietly corrupted by an
  agent that can see the answer key (ADR-007).
* **Write tools are gated.** `call()` refuses any write tool unless handed a `PolicyDecision` with
  `approved=True`. The Action Agent physically cannot execute an unapproved action (ADR-002).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..schemas import PolicyDecision, ToolCallRecord
from ..store import EventStore


class ToolError(RuntimeError):
    pass


class PolicyViolation(RuntimeError):
    """Raised when a write tool is invoked without a valid approval. Never caught silently."""


@dataclass
class ToolContext:
    """Everything a tool is allowed to touch."""

    store: EventStore
    now: datetime
    incident_id: str = "-"
    control: Any = None  # ControlPlane; typed loosely to avoid a simulation import cycle
    memory: Any = None
    notifications: list[dict[str, Any]] = field(default_factory=list)
    tickets: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    write: bool = False
    # Name of the tool that undoes this one, used by ROLLING_BACK.
    inverse: str | None = None

    def schema(self) -> dict[str, Any]:
        """OpenAI/Gemini-style function declaration."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": self.parameters,
                "required": [k for k, v in self.parameters.items() if v.get("required")],
            },
        }


class ToolRegistry:
    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self._tools: dict[str, ToolSpec] = {}
        self.calls: list[ToolCallRecord] = []

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise ToolError(f"unknown tool {name!r}")
        return self._tools[name]

    def names(self, write: bool | None = None) -> list[str]:
        if write is None:
            return sorted(self._tools)
        return sorted(n for n, s in self._tools.items() if s.write == write)

    def declarations(self, only: list[str] | None = None) -> list[dict[str, Any]]:
        chosen = only or self.names(write=False)
        return [self._tools[n].schema() for n in chosen if n in self._tools]

    def call(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        approval: PolicyDecision | None = None,
    ) -> tuple[Any, ToolCallRecord]:
        arguments = arguments or {}
        spec = self.get(name)

        if spec.write:
            if approval is None or not approval.approved:
                raise PolicyViolation(
                    f"write tool {name!r} invoked without an approved policy decision"
                )
            if approval.action.value != name:
                raise PolicyViolation(
                    f"approval authorises {approval.action.value!r}, not {name!r}"
                )

        started = time.perf_counter()
        try:
            result = spec.func(self.context, **arguments)
            record = ToolCallRecord(
                tool=name,
                arguments=arguments,
                ok=True,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
                result_summary=_summarise(result),
            )
        except PolicyViolation:
            raise
        except Exception as exc:  # a failing tool must degrade the agent, not crash the incident
            record = ToolCallRecord(
                tool=name,
                arguments=arguments,
                ok=False,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
                error=f"{type(exc).__name__}: {exc}",
            )
            self.calls.append(record)
            return None, record

        self.calls.append(record)
        return result, record

    def take_calls(self) -> list[ToolCallRecord]:
        out, self.calls = self.calls, []
        return out


def _summarise(result: Any) -> str:
    if isinstance(result, dict):
        return ", ".join(list(result)[:6])
    if isinstance(result, list):
        return f"{len(result)} rows"
    return str(result)[:120]


def build_registry(context: ToolContext) -> ToolRegistry:
    """Assemble the full tool surface."""
    from . import read_tools, write_tools

    registry = ToolRegistry(context)
    for spec in read_tools.SPECS:
        registry.register(spec)
    for spec in write_tools.SPECS:
        registry.register(spec)
    return registry
