"""Decision Agent.

Chooses one strategy from the priced set the Recovery Strategy Agent produced. It does not
generate options and it does not price them: that separation is what keeps expected value
checkable arithmetic and keeps the model's contribution to what a model is actually good at
(ADR-001).

The objective is **not** payment success rate. Optimising success rate alone would happily disable
a merchant's most popular payment method to make a graph look better. The objective is:

    expected value = expected revenue protected x P(the fix works)
                     - cost of the intervention
                     - risk exposure if it backfires

where `P(the fix works)` is an efficacy prior that the system's own history has already updated,
inside a hard bound, in the strategy layer. Every candidate carries that arithmetic, so the model
is choosing between priced options rather than inventing a plan, and a human reviewing the audit
log can check the sums.
"""

from __future__ import annotations

from typing import Any

from ..llm.base import ReasonerUnavailable
from ..schemas import (
    ActionProposal,
    ActionType,
    AgentResult,
    IncidentState,
    RecoveryStrategy,
)
from .base import Agent, IncidentContext
from .impact import format_inr
from .strategy import (
    DEFAULT_SHIFT_PCT,
    build_profile,
    build_signature,
    generate_strategies,
    price_traffic_shift,
    route_health,
)

__all__ = [
    "DecisionAgent",
    "route_health",
    "DEFAULT_SHIFT_PCT",
]


class DecisionAgent(Agent):
    name = "decision"
    state = IncidentState.DECIDING

    def run(self, ctx: IncidentContext) -> AgentResult:
        incident = ctx.incident
        diagnosis = incident.root_cause
        impact = incident.impact
        anomaly = incident.anomaly
        if diagnosis is None or impact is None or anomaly is None:
            return AgentResult(ok=False, summary="incomplete incident state", error="missing_inputs")

        plan = incident.recovery_plan
        if plan is None:
            # Defensive: the supervisor always runs the strategy stage first, but a caller driving
            # agents directly (a test, the integration harness) should still get a decision rather
            # than a crash. Options are generated on the same code path either way.
            health = route_health(ctx)
            ctx.scratch["route_health"] = health
            signature = build_signature(incident, route_health=health)
            profile = build_profile(
                incident.merchant_id,
                policy=getattr(ctx.gateway, "policy", {}) or {},
                records=ctx.memory.all() if ctx.memory is not None else [],
            )
            strategies = generate_strategies(
                ctx, health, memory=ctx.memory, signature=signature, profile=profile
            )
        else:
            health = ctx.scratch.get("route_health") or route_health(ctx)
            signature = plan.signature
            strategies = list(plan.strategies)

        if not strategies:
            return AgentResult(ok=False, summary="no candidate action", error="no_candidates")

        by_action: dict[str, RecoveryStrategy] = {}
        catalogue: list[dict[str, Any]] = []
        for strategy in _ranked(strategies):
            entry = strategy.as_catalogue_entry()
            catalogue.append(entry)
            # The reasoner answers with an action, not a strategy id, so the best-ranked candidate
            # for each action is the one its answer resolves to.
            by_action.setdefault(strategy.action.value, strategy)

        context = {
            "root_cause": diagnosis.most_likely_root_cause,
            "root_cause_id": diagnosis.cause_id,
            "root_cause_confidence": diagnosis.confidence,
            "ambiguous": diagnosis.ambiguous,
            "revenue_at_risk_per_hour_paise": impact.revenue_at_risk_per_hour_paise,
            "current_success_rate": anomaly.current_value,
            "baseline_success_rate": anomaly.baseline,
            "affected_segments": [s.segment.dimensions for s in anomaly.affected_segments[:4]],
            "route_health": health,
            # Historical evidence is handed to the reasoner as structured data it may weigh and
            # explain. It cannot add to it: every entry was computed from stored records before
            # the model was called.
            "historical_recommendation": plan.historical_recommendation if plan else "",
            "comparable_incidents": (plan.similar_incidents[:5] if plan else []),
        }

        reasoner_name = "deterministic"
        try:
            choice = ctx.reasoner.propose_action(context, catalogue)
            reasoner_name = choice.reasoner
        except ReasonerUnavailable as exc:
            from ..llm.deterministic import DeterministicReasoner

            choice = DeterministicReasoner().propose_action(context, catalogue)
            ctx.publish("reasoner_degraded", {"error": str(exc)})

        chosen = by_action.get(choice.action.value) or _ranked(strategies)[0]
        parameters = dict(choice.parameters or chosen.parameters)

        # The model may resize a traffic shift, but the expected value must be recomputed from the
        # parameters actually chosen - never carried over from the candidate it was quoted against,
        # and never without re-applying the historical prior for the new magnitude.
        if choice.action is ActionType.SHIFT_TRAFFIC and parameters.get("percentage"):
            method = parameters.get("payment_method")
            repriced = price_traffic_shift(
                ctx,
                route_health(ctx, payment_method=method) if method else health,
                from_route=parameters.get("from_route", chosen.parameters.get("from_route")),
                to_route=parameters.get("to_route", chosen.parameters.get("to_route")),
                percentage=float(parameters["percentage"]),
                payment_method=method,
                memory=ctx.memory,
                signature=signature,
                strategy_id=chosen.strategy_id,
            )
            if repriced is not None:
                chosen = repriced
                parameters = dict(chosen.parameters)

        proposal = ActionProposal(
            incident_id=incident.incident_id,
            action=choice.action,
            parameters=parameters,
            rationale=choice.rationale or chosen.reasoning,
            expected_revenue_protected_per_hour_paise=(
                chosen.expected_revenue_protected_per_hour_paise
            ),
            expected_cost_paise=chosen.expected_cost_paise,
            risk_score=choice.risk_score,
            confidence=diagnosis.confidence,
            reversible=chosen.reversible,
            expected_value_paise=chosen.expected_value_paise,
            alternatives_considered=choice.alternatives
            or [c for c in catalogue if c["action"] != choice.action.value][:4],
            proposer=reasoner_name,
        )
        summary = (
            f"{proposal.action.value} "
            f"(EV {format_inr(proposal.expected_value_paise)}, risk {proposal.risk_score:.2f})"
        )
        if chosen.historical_support and chosen.historical_support.matched_incidents:
            stats = chosen.historical_support.stats
            if stats is not None:
                summary += f", history {stats.successes}/{stats.attempts}"
        return AgentResult(ok=True, summary=summary, output=proposal, reasoner=reasoner_name)


def _ranked(strategies: list[RecoveryStrategy]) -> list[RecoveryStrategy]:
    """Best first, by expected value then by lower risk.

    Risk breaks ties rather than being subtracted a second time: it is already priced into
    expected value, and charging for it twice would make the agent refuse cheap, useful actions.
    """
    return sorted(strategies, key=lambda s: (s.expected_value_paise, -s.risk_score), reverse=True)
