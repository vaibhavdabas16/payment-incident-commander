"""Detection Agent.

A thin agent wrapper over the deterministic detector. It exists so detection appears in the same
audited step stream as every other stage — not because detection needs an agent to make decisions.
The reasoning lives in `pic/detection/`, and no model participates (ADR-001).
"""

from __future__ import annotations

from ..schemas import AgentResult, IncidentState
from .base import Agent, IncidentContext
from .impact import format_inr


class DetectionAgent(Agent):
    name = "detection"
    state = IncidentState.DETECTED

    def run(self, ctx: IncidentContext) -> AgentResult:
        signal = ctx.detector.evaluate(ctx.now)
        if signal is None:
            return AgentResult(ok=True, summary="no anomaly", output=None)

        summary = (
            f"{signal.severity.value}: success rate {signal.current_value:.1%} vs baseline "
            f"{signal.baseline:.1%} ({signal.deviation:+.1%}), z={signal.z_score}, "
            f"confidence {signal.confidence:.2f}, "
            f"{format_inr(signal.estimated_revenue_at_risk_paise)}/hr at risk"
        )
        return AgentResult(ok=True, summary=summary, output=signal)
