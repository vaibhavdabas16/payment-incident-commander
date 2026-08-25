"""Decision Agent.

Generates candidate interventions from the diagnosis, costs each one deterministically, then asks
the reasoner to choose between them. The split is deliberate: expected value is arithmetic and
belongs in code, while choosing under competing trade-offs is judgement and is where a model
genuinely helps (ADR-001).

The objective is **not** payment success rate. Optimising success rate alone would happily disable
a merchant's most popular payment method to make a graph look better. The objective is:

    expected value = expected revenue protected x P(the fix works)
                     - cost of the intervention
                     - risk exposure if it backfires

Every candidate carries that arithmetic, so the model is choosing between priced options rather
than inventing a plan, and a human reviewing the audit log can check the sums.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..llm.base import ReasonerUnavailable
from ..schemas import (
    ActionProposal,
    ActionType,
    AgentResult,
    IncidentState,
)
from .base import Agent, IncidentContext
from .impact import format_inr

# How long an intervention is assumed to hold before review. Benefit is valued over this horizon.
BENEFIT_HORIZON_HOURS = 1.0

# Prior probability that each action class actually fixes the cause it targets. These encode
# operational reality: rerouting away from a broken provider usually works; asking a merchant to
# call their bank rarely produces a fix inside the hour.
EFFICACY_PRIOR = {
    ActionType.SHIFT_TRAFFIC: 0.85,
    ActionType.DISABLE_PAYMENT_METHOD: 0.75,
    ActionType.CONFIGURE_RETRY: 0.45,
    ActionType.ROLLBACK_CHANGE: 0.80,
    ActionType.NOTIFY_MERCHANT: 0.15,
    ActionType.SET_MONITORING_FREQUENCY: 0.0,
    ActionType.CREATE_INCIDENT_TICKET: 0.0,
    ActionType.NO_ACTION: 0.0,
}

# Baseline risk that the action itself causes harm.
RISK_PRIOR = {
    ActionType.SHIFT_TRAFFIC: 0.25,
    ActionType.DISABLE_PAYMENT_METHOD: 0.55,
    ActionType.CONFIGURE_RETRY: 0.35,
    ActionType.ROLLBACK_CHANGE: 0.40,
    ActionType.NOTIFY_MERCHANT: 0.02,
    ActionType.SET_MONITORING_FREQUENCY: 0.01,
    ActionType.CREATE_INCIDENT_TICKET: 0.01,
    ActionType.NO_ACTION: 0.0,
}

DEFAULT_SHIFT_PCT = 15.0

# Success-rate points the destination route might plausibly lose under the extra load. This is the
# downside a traffic shift actually risks, and it is what expected value is charged for.
DESTINATION_DEGRADATION_RISK = 0.05


# Route health is measured from incident onset, but never over less than this - a couple of
# minutes of traffic per route is the minimum that separates a real gap from noise.
MIN_HEALTH_WINDOW_S = 150
# Below this a route's rate is too noisy to route traffic on.
MIN_ROUTE_VOLUME = 25


def route_health(
    ctx: IncidentContext, minutes: int | None = None, payment_method: str | None = None
) -> dict[str, float]:
    """Success rate per route since incident onset, optionally scoped to one payment method.

    Two scoping decisions, both of which changed the agent's behaviour materially:

    **Scoped to the incident window, not a fixed lookback.** A ten-minute average is mostly
    pre-incident traffic while an incident is young, so a route that has collapsed to 32% success
    still reports near 90%. Deciding on that number makes rerouting look pointless and leaves the
    incident unmitigated; worse, it feeds a benefit estimate that the intervention cannot deliver.

    **Scoped to one payment method.** A route carries every method, so a route catastrophically
    broken for UPI still shows a healthy blended rate because its card traffic is fine. The blended
    number both understates the benefit and obscures which traffic should move.
    """
    end = ctx.now
    if minutes is not None:
        start = end - timedelta(minutes=minutes)
    else:
        anomaly = ctx.incident.anomaly if ctx.incident else None
        onset = anomaly.window_start if anomaly else end - timedelta(seconds=MIN_HEALTH_WINDOW_S)
        start = min(onset, end - timedelta(seconds=MIN_HEALTH_WINDOW_S))
    out: dict[str, float] = {}
    for route in sorted({e.route_id for e in ctx.store.slice(start, end)}):
        filters = {"route_id": route}
        if payment_method:
            filters["payment_method"] = payment_method
        window = ctx.store.metric_window(start, end, filters)
        if window.total >= MIN_ROUTE_VOLUME:
            out[route] = round(window.success_rate, 4)
    return out


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

        health = route_health(ctx)
        ctx.scratch["route_health"] = health

        catalogue = self._candidates(ctx, health)
        if not catalogue:
            catalogue = [self._no_action(ctx, "No intervention is available for this diagnosis.")]

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
        }

        reasoner_name = "deterministic"
        try:
            choice = ctx.reasoner.propose_action(context, catalogue)
            reasoner_name = choice.reasoner
        except ReasonerUnavailable as exc:
            from ..llm.deterministic import DeterministicReasoner

            choice = DeterministicReasoner().propose_action(context, catalogue)
            ctx.publish("reasoner_degraded", {"error": str(exc)})

        chosen = next(
            (c for c in catalogue if c["action"] == choice.action.value),
            catalogue[0],
        )
        parameters = dict(choice.parameters or chosen.get("parameters", {}))

        # The model may resize a traffic shift, but the expected value must be recomputed from the
        # parameters actually chosen - never carried over from the candidate it was quoted against.
        if choice.action is ActionType.SHIFT_TRAFFIC and parameters.get("percentage"):
            method = parameters.get("payment_method")
            chosen = self._price_traffic_shift(
                ctx,
                route_health(ctx, payment_method=method) if method else health,
                from_route=parameters.get("from_route", chosen["parameters"].get("from_route")),
                to_route=parameters.get("to_route", chosen["parameters"].get("to_route")),
                percentage=float(parameters["percentage"]),
                payment_method=method,
            ) or chosen
            parameters = dict(chosen["parameters"])

        proposal = ActionProposal(
            incident_id=incident.incident_id,
            action=choice.action,
            parameters=parameters,
            rationale=choice.rationale or chosen.get("rationale", ""),
            expected_revenue_protected_per_hour_paise=chosen.get(
                "expected_revenue_protected_per_hour_paise", 0
            ),
            expected_cost_paise=chosen.get("expected_cost_paise", 0),
            risk_score=choice.risk_score,
            confidence=diagnosis.confidence,
            reversible=chosen.get("reversible", True),
            expected_value_paise=chosen.get("expected_value_paise", 0),
            alternatives_considered=choice.alternatives or [
                {"action": c["action"], "expected_value_paise": c["expected_value_paise"]}
                for c in catalogue
                if c["action"] != choice.action.value
            ][:4],
            proposer=reasoner_name,
        )
        summary = (
            f"{proposal.action.value} "
            f"(EV {format_inr(proposal.expected_value_paise)}, risk {proposal.risk_score:.2f})"
        )
        return AgentResult(ok=True, summary=summary, output=proposal, reasoner=reasoner_name)

    # ----------------------------------------------------------- candidates

    def _candidates(self, ctx: IncidentContext, health: dict[str, float]) -> list[dict[str, Any]]:
        diagnosis = ctx.incident.root_cause
        assert diagnosis is not None
        cause = diagnosis.cause_id
        out: list[dict[str, Any]] = []

        if cause in ("psp_degradation", "gateway_degradation", "latency_timeout_cascade", "payment_method_degradation"):
            shift = self._propose_traffic_shift(ctx, health)
            if shift:
                out.append(shift)

        if cause == "config_regression":
            rollback = self._propose_rollback(ctx)
            if rollback:
                out.append(rollback)

        if cause == "checkout_client_issue":
            rollback = self._propose_rollback(ctx)
            if rollback:
                out.append(rollback)
            out.append(
                self._notify(
                    ctx,
                    subject="Checkout failures isolated to one client build",
                    body=(
                        f"{diagnosis.most_likely_root_cause}. Failures are concentrated on a single "
                        "app version; consider halting that rollout."
                    ),
                )
            )

        if cause == "issuer_degradation":
            # No routing change can fix an issuer declining its own cards; the honest action is to
            # tell the merchant and, if failures are transient, retry.
            out.append(
                self._notify(
                    ctx,
                    subject="Issuer-side payment failures detected",
                    body=(
                        f"{diagnosis.most_likely_root_cause}. This originates at the issuing bank "
                        "and cannot be resolved by routing changes. Recommend contacting the "
                        "acquirer and considering a temporary issuer-level fallback."
                    ),
                )
            )
            retry = self._propose_retry(ctx)
            if retry:
                out.append(retry)

        if cause == "traffic_mix_shift":
            out.append(
                self._no_action(
                    ctx,
                    "Success rate fell because the traffic mix changed, not because anything "
                    "broke. Rerouting healthy traffic would add risk without adding revenue.",
                )
            )

        if cause == "multi_factor":
            out.append(
                self._no_action(
                    ctx,
                    "Multiple independent degradations are in progress and no single intervention "
                    "addresses them. This needs a human.",
                )
            )

        if cause not in ("traffic_mix_shift",):
            out.append(self._ticket(ctx))
        out.append(self._monitoring(ctx))
        if all(c["action"] != ActionType.NO_ACTION.value for c in out):
            out.append(self._no_action(ctx, "Continue observing without intervening."))
        return out

    # ------------------------------------------------------------- pricing

    def _propose_traffic_shift(
        self, ctx: IncidentContext, health: dict[str, float]
    ) -> dict[str, Any] | None:
        anomaly = ctx.incident.anomaly
        if anomaly is None:
            return None

        # Move only the traffic that is actually failing. Rerouting a merchant's healthy card
        # volume because UPI is broken adds risk and buys nothing.
        method = self._affected_method(anomaly)
        scoped = route_health(ctx, payment_method=method) if method else health
        if len(scoped) < 2:
            scoped = health

        degraded_route = self._worst_route(ctx, scoped)
        if degraded_route is None:
            return None
        destination = self._best_destination(scoped, exclude=degraded_route)
        if destination is None:
            return None
        return self._price_traffic_shift(
            ctx,
            scoped,
            from_route=degraded_route,
            to_route=destination,
            percentage=DEFAULT_SHIFT_PCT,
            payment_method=method,
        )

    def _affected_method(self, anomaly: Any) -> str | None:
        for segment in anomaly.affected_segments:
            method = segment.segment.dimensions.get("payment_method")
            if method:
                return method
        return None

    def _price_traffic_shift(
        self,
        ctx: IncidentContext,
        health: dict[str, float],
        from_route: str | None,
        to_route: str | None,
        percentage: float,
        payment_method: str | None = None,
    ) -> dict[str, Any] | None:
        if not from_route or not to_route or from_route == to_route:
            return None
        source_rate = health.get(from_route)
        dest_rate = health.get(to_route)
        if source_rate is None or dest_rate is None:
            return None

        diagnosis = ctx.incident.root_cause
        if diagnosis is None:
            return None

        end = ctx.now
        anomaly = ctx.incident.anomaly
        onset = anomaly.window_start if anomaly else end - timedelta(seconds=MIN_HEALTH_WINDOW_S)
        start = min(onset, end - timedelta(seconds=MIN_HEALTH_WINDOW_S))
        filters = {"payment_method": payment_method} if payment_method else None
        window = ctx.store.metric_window(start, end, filters)
        if window.total == 0:
            return None
        hours = (end - start).total_seconds() / 3600.0
        attempts_per_hour = window.total / hours
        avg_value = (window.gmv_paise + window.failed_gmv_paise) / window.total

        # Moving `percentage` of traffic converts it from the source route's success rate to the
        # destination's. Benefit only ever applies to the traffic actually moved.
        moved_per_hour = attempts_per_hour * (percentage / 100.0)
        rate_gain = max(0.0, dest_rate - source_rate)
        benefit_per_hour = moved_per_hour * rate_gain * avg_value

        p_success = EFFICACY_PRIOR[ActionType.SHIFT_TRAFFIC] * max(0.3, diagnosis.confidence)
        expected_benefit = benefit_per_hour * BENEFIT_HORIZON_HOURS * p_success

        risk = RISK_PRIOR[ActionType.SHIFT_TRAFFIC]
        if rate_gain < 0.02:
            # Barely worth doing, and concentrating load on a route for no gain is pure downside.
            risk += 0.25

        # The realistic harm is that the destination degrades under the added load - a few points
        # of success rate on the moved traffic only. Pricing the downside as a share of the moved
        # GMV instead would treat a routine reroute as risking the entire volume, which makes every
        # shift look catastrophic and the agent never acts at all.
        downside = moved_per_hour * BENEFIT_HORIZON_HOURS * avg_value * DESTINATION_DEGRADATION_RISK
        expected_value = int(expected_benefit - risk * downside)

        scope = f" for {payment_method}" if payment_method else ""
        return {
            "action": ActionType.SHIFT_TRAFFIC.value,
            "parameters": {
                "from_route": from_route,
                "to_route": to_route,
                "percentage": percentage,
                **({"payment_method": payment_method} if payment_method else {}),
            },
            "rationale": (
                f"Route {from_route} is at {source_rate:.1%} success{scope} while {to_route} is at "
                f"{dest_rate:.1%}. Moving {percentage:.0f}% of that traffic converts roughly "
                f"{moved_per_hour:,.0f} payments/hour from the worse rate to the better one."
            ),
            "expected_revenue_protected_per_hour_paise": int(benefit_per_hour),
            "expected_cost_paise": 0,
            "expected_value_paise": expected_value,
            "risk_score": round(min(1.0, risk), 3),
            "reversible": True,
            "p_success": round(p_success, 3),
        }

    def _propose_rollback(self, ctx: IncidentContext) -> dict[str, Any] | None:
        evidence = ctx.incident.evidence
        impact = ctx.incident.impact
        diagnosis = ctx.incident.root_cause
        if not evidence or not evidence.recent_config_changes or impact is None or diagnosis is None:
            return None
        change = min(
            evidence.recent_config_changes,
            key=lambda c: abs((ctx.now - c.timestamp).total_seconds()),
        )
        if not change.reversible:
            return None
        p_success = EFFICACY_PRIOR[ActionType.ROLLBACK_CHANGE] * max(0.3, diagnosis.confidence)
        benefit = impact.revenue_at_risk_per_hour_paise * BENEFIT_HORIZON_HOURS * p_success
        risk = RISK_PRIOR[ActionType.ROLLBACK_CHANGE]
        downside = impact.revenue_at_risk_per_hour_paise * 0.15
        return {
            "action": ActionType.ROLLBACK_CHANGE.value,
            "parameters": {"change_id": change.change_id},
            "rationale": (
                f"Configuration change {change.change_id} to {change.component} immediately "
                f"precedes the degradation ({change.description}). Reverting it addresses the "
                "cause directly rather than routing around it."
            ),
            "expected_revenue_protected_per_hour_paise": int(
                impact.revenue_at_risk_per_hour_paise * p_success
            ),
            "expected_cost_paise": 0,
            "expected_value_paise": int(benefit - risk * downside),
            "risk_score": RISK_PRIOR[ActionType.ROLLBACK_CHANGE],
            # Not reversible by replaying an inverse: undoing a rollback means another deploy.
            "reversible": False,
            "p_success": round(p_success, 3),
        }

    def _propose_retry(self, ctx: IncidentContext) -> dict[str, Any] | None:
        """Only worth proposing when failures are transient; retries on hard declines add load."""
        evidence = ctx.incident.evidence
        impact = ctx.incident.impact
        if evidence is None or impact is None:
            return None
        transient = {"AUTH_TIMEOUT", "GATEWAY_TIMEOUT", "PSP_UNAVAILABLE", "BANK_UNAVAILABLE"}
        if evidence.dominant_error_code not in transient:
            return None
        p_success = EFFICACY_PRIOR[ActionType.CONFIGURE_RETRY]
        benefit = impact.revenue_at_risk_per_hour_paise * 0.25 * p_success
        return {
            "action": ActionType.CONFIGURE_RETRY.value,
            "parameters": {"max_retries": 2, "enabled": True},
            "rationale": (
                f"Failures are dominated by {evidence.dominant_error_code}, a transient class that "
                "retries can recover. This adds load to an already degraded provider, so the "
                "expected gain is modest."
            ),
            "expected_revenue_protected_per_hour_paise": int(benefit),
            "expected_cost_paise": 0,
            "expected_value_paise": int(benefit * 0.6),
            "risk_score": RISK_PRIOR[ActionType.CONFIGURE_RETRY],
            "reversible": True,
            "p_success": p_success,
        }

    def _notify(self, ctx: IncidentContext, subject: str, body: str) -> dict[str, Any]:
        impact = ctx.incident.impact
        risk = impact.revenue_at_risk_per_hour_paise if impact else 0
        return {
            "action": ActionType.NOTIFY_MERCHANT.value,
            "parameters": {"subject": subject, "body": body, "urgency": "high"},
            "rationale": (
                "The fault is outside our control plane, so the only useful action is to inform "
                "the merchant quickly and accurately."
            ),
            # Notification protects revenue only insofar as it shortens human response time.
            "expected_revenue_protected_per_hour_paise": int(risk * 0.15),
            "expected_cost_paise": 0,
            "expected_value_paise": int(risk * 0.15 * EFFICACY_PRIOR[ActionType.NOTIFY_MERCHANT] * 10),
            "risk_score": RISK_PRIOR[ActionType.NOTIFY_MERCHANT],
            "reversible": True,
            "p_success": EFFICACY_PRIOR[ActionType.NOTIFY_MERCHANT],
        }

    def _ticket(self, ctx: IncidentContext) -> dict[str, Any]:
        diagnosis = ctx.incident.root_cause
        return {
            "action": ActionType.CREATE_INCIDENT_TICKET.value,
            "parameters": {
                "title": f"{ctx.incident.incident_id}: {diagnosis.most_likely_root_cause if diagnosis else 'payment degradation'}",
                "description": diagnosis.narrative if diagnosis else "",
                "severity": ctx.incident.severity.value,
            },
            "rationale": "Records the incident for human follow-up without changing payment behaviour.",
            "expected_revenue_protected_per_hour_paise": 0,
            "expected_cost_paise": 0,
            "expected_value_paise": 1,
            "risk_score": RISK_PRIOR[ActionType.CREATE_INCIDENT_TICKET],
            "reversible": True,
            "p_success": 0.0,
        }

    def _monitoring(self, ctx: IncidentContext) -> dict[str, Any]:
        _ = ctx
        return {
            "action": ActionType.SET_MONITORING_FREQUENCY.value,
            "parameters": {"interval_seconds": 60},
            "rationale": "Observe more frequently while the incident is live. Safe and reversible.",
            "expected_revenue_protected_per_hour_paise": 0,
            "expected_cost_paise": 0,
            "expected_value_paise": 1,
            "risk_score": RISK_PRIOR[ActionType.SET_MONITORING_FREQUENCY],
            "reversible": True,
            "p_success": 0.0,
        }

    def _no_action(self, ctx: IncidentContext, rationale: str) -> dict[str, Any]:
        _ = ctx
        return {
            "action": ActionType.NO_ACTION.value,
            "parameters": {},
            "rationale": rationale,
            "expected_revenue_protected_per_hour_paise": 0,
            "expected_cost_paise": 0,
            # Zero, not negative: doing nothing is the correct baseline every other option must beat.
            "expected_value_paise": 0,
            "risk_score": 0.0,
            "reversible": True,
            "p_success": 0.0,
        }

    # -------------------------------------------------------------- helpers

    def _worst_route(self, ctx: IncidentContext, health: dict[str, float]) -> str | None:
        """The route implicated by the evidence, preferring attribution over raw rate."""
        anomaly = ctx.incident.anomaly
        if anomaly:
            for segment in anomaly.affected_segments:
                route = segment.segment.dimensions.get("route_id")
                if route and route in health:
                    return route
            # A PSP maps onto exactly one route in this control plane.
            for segment in anomaly.affected_segments:
                psp = segment.segment.dimensions.get("psp")
                if psp:
                    from ..simulation.generator import ROUTES

                    for route_id, spec in ROUTES.items():
                        if spec["psp"] == psp and route_id in health:
                            return route_id
        if not health:
            return None
        return min(health, key=lambda r: health[r])

    def _best_destination(self, health: dict[str, float], exclude: str) -> str | None:
        options = {r: v for r, v in health.items() if r != exclude}
        if not options:
            return None
        return max(options, key=lambda r: options[r])
