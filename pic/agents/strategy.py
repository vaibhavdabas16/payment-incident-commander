"""Recovery Strategy Agent.

Between diagnosis and decision there is a question the old pipeline answered implicitly: *what
could we do about this?* This agent answers it explicitly. It produces the closed set of candidate
recovery strategies, prices each one deterministically, and attaches to each the record of what
happened the last time the system tried that action under conditions like these.

The split from the Decision Agent is the point. **Generating and pricing options is arithmetic**
— rates, volumes, rupees, priors — and belongs in code that a merchant can audit line by line.
**Choosing between priced options under competing trade-offs is judgement**, and that is what the
Decision Agent (and, where configured, the model) does. Neither half can do the other's job: this
agent cannot select, and the Decision Agent cannot invent an option that was never priced.

**How history enters, and how far it is allowed to go.** For each candidate, memory is queried for
the observed success rate of that action at that magnitude under a similar failure signature. The
result updates the action's efficacy prior through a shrinkage estimator with a hard cap of
±`MAX_HISTORY_ADJUSTMENT`, so:

* nine successes out of eleven raise the probability that the fix works, which raises expected
  value, which makes the option more likely to be chosen;
* four failures out of six lower it;
* a single past incident moves almost nothing, because the estimator is weighted by sample size;
* nothing history says can move the estimate more than the cap, can create an option, can change a
  measured rupee figure, or can reach the policy gateway. History argues. It does not decide.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from ..memory.profile import MerchantProfile, build_profile
from ..memory.store import build_signature
from ..schemas import (
    ActionOutcomeStats,
    ActionType,
    AgentResult,
    FailureSignature,
    HistoricalSupport,
    IncidentState,
    RecoveryPlan,
    RecoveryStrategy,
    magnitude_band,
)
from .base import Agent, IncidentContext
from .impact import format_inr

# How long an intervention is assumed to hold before review. Benefit is valued over this horizon.
BENEFIT_HORIZON_HOURS = 1.0

# Prior probability that each action class actually fixes the cause it targets. These encode
# operational reality: rerouting away from a broken provider usually works; asking a merchant to
# call their bank rarely produces a fix inside the hour. History updates these; it does not
# replace them.
EFFICACY_PRIOR = {
    ActionType.SHIFT_TRAFFIC: 0.85,
    ActionType.DISABLE_PAYMENT_METHOD: 0.75,
    ActionType.CONFIGURE_RETRY: 0.45,
    ActionType.ROLLBACK_CHANGE: 0.80,
    ActionType.RECOVER_FAILED_PAYMENTS: 0.55,
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
    ActionType.RECOVER_FAILED_PAYMENTS: 0.20,
    ActionType.NOTIFY_MERCHANT: 0.02,
    ActionType.SET_MONITORING_FREQUENCY: 0.01,
    ActionType.CREATE_INCIDENT_TICKET: 0.01,
    ActionType.NO_ACTION: 0.0,
}

DEFAULT_SHIFT_PCT = 15.0

# Success-rate points the destination route might plausibly lose under the extra load. This is the
# downside a traffic shift actually risks, and it is what expected value is charged for.
DESTINATION_DEGRADATION_RISK = 0.05

# The larger the shift, the more load lands on one route, and the more likely that route degrades
# under it. Without this the downside is linear in magnitude and so is the benefit, which makes
# the largest permitted shift trivially optimal every time — a model of rerouting in which
# concentration is free. Charging the per-payment downside a surcharge for every point *above*
# the default shift makes the total downside grow quadratically past that point while the benefit
# stays linear, so expected value has an interior maximum and an oversized shift can genuinely be
# the worse option.
#
# Anchored at `DEFAULT_SHIFT_PCT` rather than at zero, so a routine shift is priced exactly as it
# always was and only an unusually large one pays for its concentration. Anchoring at zero
# repriced every reroute by 60% more downside, which turned marginal-but-correct interventions
# into "file a ticket" — the agent declining to act because the arithmetic moved, not because the
# world had.
DEGRADATION_SCALE_PCT = 25.0

# The most of a source route's own traffic that one shift may take.
#
# This is not a caution about moving too much traffic - the merchant's policy bound is what governs
# that. It is about keeping the intervention *measurable*. Verification's primary test is a
# concurrent control: the traffic left behind on the source route, living through the same ramp and
# the same hour. A shift sized to drain the source leaves nothing to compare against, verification
# silently falls back to before/after, and the system loses the one mechanism that lets it tell "my
# action hurt" from "the incident got worse".
#
# Concretely: on a route carrying 25% of a method's traffic, a 20-point shift leaves 5%, which is
# usually below the sample the control test needs. The shift is not merely over-ambitious, it is
# unverifiable - and an unverifiable intervention on a live payment system is worse than a smaller
# one that can be checked.
MAX_SOURCE_DRAIN = 0.6
# Never price a shift smaller than this: below it the moved volume is too small to measure either.
MIN_SHIFT_PCT = 5.0

# Route health is measured from incident onset, but never over less than this - a couple of
# minutes of traffic per route is the minimum that separates a real gap from noise.
MIN_HEALTH_WINDOW_S = 150
# Below this a route's rate is too noisy to route traffic on.
MIN_ROUTE_VOLUME = 25

# --- how far history may move an efficacy prior -----------------------------
# Equivalent prior sample size in the shrinkage estimator: history has to accumulate about four
# comparable incidents before it counts as much as the prior does.
HISTORY_PRIOR_WEIGHT = 4.0
# Hard cap on the adjustment, in probability points. A run of similar past incidents can tilt a
# close call; it can never override what the live evidence says.
MAX_HISTORY_ADJUSTMENT = 0.20
# Extra risk charged when comparable interventions had to be rolled back. Bounded for the same
# reason.
MAX_HISTORY_RISK_PENALTY = 0.15


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


# --------------------------------------------------------------------------
# Historical support
# --------------------------------------------------------------------------


def historical_support(
    memory: Any,
    signature: FailureSignature | None,
    action: ActionType,
    magnitude: float | None = None,
) -> HistoricalSupport:
    """What memory says about this action, at this magnitude, under conditions like these.

    Returns an empty, zero-adjustment support object when memory is absent or silent. "We have
    never tried this" and "we tried this and it failed" must never look the same, so the caller
    can distinguish them on `matched_incidents`.
    """
    empty = HistoricalSupport(recommendation="No comparable history for this action yet.")
    if memory is None or signature is None or not hasattr(memory, "get_success_rate_for_action"):
        return empty

    stats = memory.get_success_rate_for_action(
        action.value, signature=signature, magnitude=magnitude
    )
    magnitude_matched = stats.attempts > 0 and magnitude is not None
    if stats.attempts == 0 and magnitude is not None:
        # Nothing at this exact magnitude. Fall back to the action overall, which is weaker
        # evidence about sizing but real evidence about the action.
        stats = memory.get_success_rate_for_action(action.value, signature=signature)
    if stats.attempts == 0:
        return empty

    prior = EFFICACY_PRIOR.get(action, 0.3)
    adjustment = _shrunk_adjustment(prior, stats)
    return HistoricalSupport(
        matched_incidents=stats.attempts,
        stats=stats,
        magnitude_matched=magnitude_matched,
        efficacy_adjustment=round(adjustment, 4),
        recommendation=_describe_stats(stats, magnitude if magnitude_matched else None),
        evidence=[
            f"{stats.successes}/{stats.attempts} comparable incidents fully recovered and "
            f"{stats.partials} partially recovered with {stats.action}"
            + (f" at {stats.magnitude_band}" if stats.magnitude_band != "any" else "")
            + ".",
            *(
                [f"{stats.rollbacks} of them had to be rolled back."]
                if stats.rollbacks
                else []
            ),
            *(
                [f"Median time to recovery: {stats.median_recovery_time_s / 60:.1f} min."]
                if stats.median_recovery_time_s
                else []
            ),
            *(
                [
                    "Median revenue protected: "
                    f"{format_inr(stats.median_revenue_protected_paise)}."
                ]
                if stats.median_revenue_protected_paise
                else []
            ),
            f"Incidents: {', '.join(stats.incident_ids[:6])}.",
        ],
    )


def _shrunk_adjustment(prior: float, stats: ActionOutcomeStats) -> float:
    """Move the efficacy prior toward the observed rate, weighted by how much has been observed.

    A single outcome barely moves the estimate; a dozen consistent ones move it to the cap. This
    is the whole mechanism by which learning changes future behaviour, and it is deliberately a
    six-line arithmetic function rather than anything a model participates in.

    It blends toward `helped_rate`, not `success_rate`. The prior being updated is *P(this action
    helps)*, and a control-verified partial recovery is evidence that it does. Using the strict
    rate here would teach the system that an intervention which measurably improved payments was
    a failure, and it would eventually stop proposing the thing that works.
    """
    if stats.attempts <= 0:
        return 0.0
    blended = (HISTORY_PRIOR_WEIGHT * prior + stats.attempts * stats.helped_rate) / (
        HISTORY_PRIOR_WEIGHT + stats.attempts
    )
    return max(-MAX_HISTORY_ADJUSTMENT, min(MAX_HISTORY_ADJUSTMENT, blended - prior))


def _describe_stats(stats: ActionOutcomeStats, magnitude: float | None) -> str:
    where = f" at {magnitude:g}%" if magnitude is not None else ""
    parts = [
        f"{stats.successes} of {stats.attempts} comparable incidents fully recovered with this "
        f"action{where}"
        + (f", {stats.partials} partially." if stats.partials else ".")
    ]
    if stats.median_recovery_time_s:
        parts.append(f"Median recovery {stats.median_recovery_time_s / 60:.1f} min.")
    if stats.median_revenue_protected_paise:
        parts.append(
            f"Median revenue protected {format_inr(stats.median_revenue_protected_paise)}."
        )
    if stats.rollbacks:
        parts.append(f"{stats.rollbacks} required a rollback.")
    return " ".join(parts)


def _history_risk_penalty(support: HistoricalSupport | None) -> float:
    """Extra risk charged for an action that has historically had to be undone here."""
    if support is None or support.stats is None or support.stats.attempts == 0:
        return 0.0
    rate = support.stats.rollbacks / support.stats.attempts
    return min(MAX_HISTORY_RISK_PENALTY, round(rate * 0.3, 4))


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


class _Ids:
    """Stable per-plan strategy ids, so a candidate can be referred to and audited."""

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"STR-{self._n:02d}"


def price_traffic_shift(
    ctx: IncidentContext,
    health: dict[str, float],
    *,
    from_route: str | None,
    to_route: str | None,
    percentage: float,
    payment_method: str | None = None,
    memory: Any = None,
    signature: FailureSignature | None = None,
    strategy_id: str | None = None,
) -> RecoveryStrategy | None:
    """Price moving `percentage` points of traffic from one route to another."""
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

    support = historical_support(memory, signature, ActionType.SHIFT_TRAFFIC, percentage)
    efficacy = max(
        0.02, min(0.98, EFFICACY_PRIOR[ActionType.SHIFT_TRAFFIC] + support.efficacy_adjustment)
    )
    p_success = efficacy * max(0.3, diagnosis.confidence)
    expected_benefit = benefit_per_hour * BENEFIT_HORIZON_HOURS * p_success

    risk = RISK_PRIOR[ActionType.SHIFT_TRAFFIC] + _history_risk_penalty(support)
    if rate_gain < 0.02:
        # Barely worth doing, and concentrating load on a route for no gain is pure downside.
        risk += 0.25

    # A shift that drains its own source leaves no control group to verify it against. Charged as
    # risk rather than refused, so a human or a model that resizes upward still gets a priced
    # option and can see what the size costs - but it will rank below one that can be measured.
    ceiling = verifiable_shift_ceiling(
        source_traffic_share(ctx, from_route, payment_method)
    )
    unverifiable = ceiling > 0 and percentage > ceiling
    if unverifiable:
        risk += 0.20

    # The realistic harm is that the destination degrades under the added load - a few points of
    # success rate on the moved traffic, worsening as more of it is piled on. Pricing the downside
    # as a share of the moved GMV instead would treat a routine reroute as risking the entire
    # volume, which makes every shift look catastrophic and the agent never acts at all.
    concentration = 1.0 + max(0.0, percentage - DEFAULT_SHIFT_PCT) / DEGRADATION_SCALE_PCT
    downside = (
        moved_per_hour
        * BENEFIT_HORIZON_HOURS
        * avg_value
        * DESTINATION_DEGRADATION_RISK
        * concentration
    )
    expected_value = int(expected_benefit - min(1.0, risk) * downside)

    scope = f" for {payment_method}" if payment_method else ""
    reasoning = (
        f"Route {from_route} is at {source_rate:.1%} success{scope} while {to_route} is at "
        f"{dest_rate:.1%}. Moving {percentage:.0f}% of that traffic converts roughly "
        f"{moved_per_hour:,.0f} payments/hour from the worse rate to the better one."
    )
    if unverifiable:
        reasoning += (
            f" At this size the shift would take most of {from_route}'s traffic, leaving too "
            f"little behind to act as a control group - the intervention would be hard to verify."
        )
    if support.matched_incidents:
        reasoning += f" {support.recommendation}"

    return RecoveryStrategy(
        strategy_id=strategy_id or f"STR-{uuid.uuid4().hex[:4]}",
        action=ActionType.SHIFT_TRAFFIC,
        target=f"{from_route} -> {to_route}",
        magnitude=percentage,
        parameters={
            "from_route": from_route,
            "to_route": to_route,
            "percentage": percentage,
            **({"payment_method": payment_method} if payment_method else {}),
        },
        expected_revenue_protected_per_hour_paise=int(benefit_per_hour),
        expected_cost_paise=0,
        expected_value_paise=expected_value,
        confidence=round(diagnosis.confidence, 4),
        risk_score=round(min(1.0, risk), 3),
        reversible=True,
        p_success=round(p_success, 3),
        reasoning=reasoning,
        supporting_findings=_findings_for(ctx, {"psp", "route_id", "payment_method", "gateway"}),
        historical_support=support,
    )


def source_traffic_share(
    ctx: IncidentContext, from_route: str, payment_method: str | None = None
) -> float:
    """Fraction of the scoped traffic currently flowing through `from_route`.

    Measured over the same window the shift is priced against, so "20 points of traffic" is
    compared against how much traffic that route actually carries rather than against an assumed
    routing table.
    """
    end = ctx.now
    anomaly = ctx.incident.anomaly
    onset = anomaly.window_start if anomaly else end - timedelta(seconds=MIN_HEALTH_WINDOW_S)
    start = min(onset, end - timedelta(seconds=MIN_HEALTH_WINDOW_S))
    base = {"payment_method": payment_method} if payment_method else {}
    total = ctx.store.metric_window(start, end, base or None)
    if total.total == 0:
        return 0.0
    on_route = ctx.store.metric_window(start, end, {**base, "route_id": from_route})
    return on_route.total / total.total


def verifiable_shift_ceiling(share: float) -> float:
    """The largest shift, in points of traffic, that still leaves a usable control group."""
    return round(share * 100.0 * MAX_SOURCE_DRAIN, 1)


def _findings_for(ctx: IncidentContext, dimensions: set[str]) -> list[str]:
    """Finding ids from the evidence bundle that bear on this kind of action.

    Real ids from the real bundle, so every strategy can be traced back to the tool output that
    justifies it — the same grounding rule the Root Cause Agent is held to.
    """
    evidence = ctx.incident.evidence
    if evidence is None:
        return []
    return [f.finding_id for f in evidence.findings if f.dimension in dimensions][:6]


# --------------------------------------------------------------------------
# Candidate generation
# --------------------------------------------------------------------------


def _worst_route(ctx: IncidentContext, health: dict[str, float]) -> str | None:
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


def _best_destination(health: dict[str, float], exclude: str) -> str | None:
    options = {r: v for r, v in health.items() if r != exclude}
    if not options:
        return None
    return max(options, key=lambda r: options[r])


def _affected_method(anomaly: Any) -> str | None:
    for segment in anomaly.affected_segments:
        method = segment.segment.dimensions.get("payment_method")
        if method:
            return method
    return None


def _traffic_shifts(
    ctx: IncidentContext,
    health: dict[str, float],
    ids: _Ids,
    profile: MerchantProfile,
    memory: Any,
    signature: FailureSignature | None,
) -> list[RecoveryStrategy]:
    """One priced option per magnitude the merchant permits, not a single take-it-or-leave-it.

    Offering several sizes is what lets history express *"a 20% shift worked here and a 50% one
    did not"*. With one candidate the only thing history could do is make the same action look
    better or worse; with a ladder it can change which size gets chosen, which is the behaviour
    change this whole subsystem exists to produce.
    """
    anomaly = ctx.incident.anomaly
    if anomaly is None:
        return []

    # Move only the traffic that is actually failing. Rerouting a merchant's healthy card volume
    # because UPI is broken adds risk and buys nothing.
    method = _affected_method(anomaly)
    scoped = route_health(ctx, payment_method=method) if method else health
    if len(scoped) < 2:
        scoped = health

    degraded_route = _worst_route(ctx, scoped)
    if degraded_route is None:
        return []
    destination = _best_destination(scoped, exclude=degraded_route)
    if destination is None:
        return []

    # Only price sizes that leave a control group behind. Where the merchant's ladder is entirely
    # above that ceiling, price the ceiling itself rather than nothing: the incident still deserves
    # an option, and a smaller verifiable shift is a better answer than an unmeasurable one.
    ceiling = verifiable_shift_ceiling(source_traffic_share(ctx, degraded_route, method))
    wanted = profile.preferred_magnitudes(DEFAULT_SHIFT_PCT)
    if ceiling > 0:
        usable = [m for m in wanted if m <= ceiling]
        if not usable:
            # The whole ladder is above what stays verifiable. Offer the ceiling and a smaller
            # option rather than a single take-it-or-leave-it size: the merchant should still see
            # a trade-off, and both of these can be measured.
            usable = sorted(
                {round(max(MIN_SHIFT_PCT, ceiling), 1), round(max(MIN_SHIFT_PCT, ceiling / 2), 1)}
            )
        wanted = usable

    out: list[RecoveryStrategy] = []
    for magnitude in wanted:
        priced = price_traffic_shift(
            ctx,
            scoped,
            from_route=degraded_route,
            to_route=destination,
            percentage=magnitude,
            payment_method=method,
            memory=memory,
            signature=signature,
            strategy_id=ids.next(),
        )
        if priced is not None:
            out.append(priced)
    return out


def _rollback(
    ctx: IncidentContext, ids: _Ids, memory: Any, signature: FailureSignature | None
) -> RecoveryStrategy | None:
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

    support = historical_support(memory, signature, ActionType.ROLLBACK_CHANGE)
    efficacy = max(
        0.02, min(0.98, EFFICACY_PRIOR[ActionType.ROLLBACK_CHANGE] + support.efficacy_adjustment)
    )
    p_success = efficacy * max(0.3, diagnosis.confidence)
    benefit = impact.revenue_at_risk_per_hour_paise * BENEFIT_HORIZON_HOURS * p_success
    risk = RISK_PRIOR[ActionType.ROLLBACK_CHANGE] + _history_risk_penalty(support)
    downside = impact.revenue_at_risk_per_hour_paise * 0.15
    return RecoveryStrategy(
        strategy_id=ids.next(),
        action=ActionType.ROLLBACK_CHANGE,
        target=change.change_id,
        parameters={"change_id": change.change_id},
        expected_revenue_protected_per_hour_paise=int(
            impact.revenue_at_risk_per_hour_paise * p_success
        ),
        expected_value_paise=int(benefit - min(1.0, risk) * downside),
        confidence=round(diagnosis.confidence, 4),
        risk_score=round(min(1.0, risk), 3),
        # Not reversible by replaying an inverse: undoing a rollback means another deploy.
        reversible=False,
        p_success=round(p_success, 3),
        reasoning=(
            f"Configuration change {change.change_id} to {change.component} immediately precedes "
            f"the degradation ({change.description}). Reverting it addresses the cause directly "
            "rather than routing around it."
        ),
        supporting_findings=_findings_for(ctx, {"config_change"}),
        historical_support=support,
    )


def _retry(
    ctx: IncidentContext, ids: _Ids, memory: Any, signature: FailureSignature | None
) -> RecoveryStrategy | None:
    """Only worth proposing when failures are transient; retries on hard declines add load."""
    evidence = ctx.incident.evidence
    impact = ctx.incident.impact
    if evidence is None or impact is None:
        return None
    transient = {"AUTH_TIMEOUT", "GATEWAY_TIMEOUT", "PSP_UNAVAILABLE", "BANK_UNAVAILABLE"}
    if evidence.dominant_error_code not in transient:
        return None

    support = historical_support(memory, signature, ActionType.CONFIGURE_RETRY)
    efficacy = max(
        0.02, min(0.98, EFFICACY_PRIOR[ActionType.CONFIGURE_RETRY] + support.efficacy_adjustment)
    )
    benefit = impact.revenue_at_risk_per_hour_paise * 0.25 * efficacy
    return RecoveryStrategy(
        strategy_id=ids.next(),
        action=ActionType.CONFIGURE_RETRY,
        target="retry policy",
        parameters={"max_retries": 2, "enabled": True},
        expected_revenue_protected_per_hour_paise=int(benefit),
        expected_value_paise=int(benefit * 0.6),
        confidence=round(ctx.incident.root_cause.confidence if ctx.incident.root_cause else 0.5, 4),
        risk_score=round(
            min(1.0, RISK_PRIOR[ActionType.CONFIGURE_RETRY] + _history_risk_penalty(support)), 3
        ),
        reversible=True,
        p_success=round(efficacy, 3),
        reasoning=(
            f"Failures are dominated by {evidence.dominant_error_code}, a transient class that "
            "retries can recover. This adds load to an already degraded provider, so the expected "
            "gain is modest."
        ),
        supporting_findings=_findings_for(ctx, {"error_code"}),
        historical_support=support,
    )


def _notify(
    ctx: IncidentContext, ids: _Ids, subject: str, body: str
) -> RecoveryStrategy:
    impact = ctx.incident.impact
    risk = impact.revenue_at_risk_per_hour_paise if impact else 0
    return RecoveryStrategy(
        strategy_id=ids.next(),
        action=ActionType.NOTIFY_MERCHANT,
        target="merchant",
        parameters={"subject": subject, "body": body, "urgency": "high"},
        # Notification protects revenue only insofar as it shortens human response time.
        expected_revenue_protected_per_hour_paise=int(risk * 0.15),
        expected_value_paise=int(risk * 0.15 * EFFICACY_PRIOR[ActionType.NOTIFY_MERCHANT] * 10),
        confidence=round(ctx.incident.root_cause.confidence if ctx.incident.root_cause else 0.5, 4),
        risk_score=RISK_PRIOR[ActionType.NOTIFY_MERCHANT],
        reversible=True,
        p_success=EFFICACY_PRIOR[ActionType.NOTIFY_MERCHANT],
        reasoning=(
            "The fault is outside our control plane, so the only useful action is to inform the "
            "merchant quickly and accurately."
        ),
        supporting_findings=_findings_for(ctx, {"issuer", "error_code"}),
    )


def _ticket(ctx: IncidentContext, ids: _Ids) -> RecoveryStrategy:
    diagnosis = ctx.incident.root_cause
    return RecoveryStrategy(
        strategy_id=ids.next(),
        action=ActionType.CREATE_INCIDENT_TICKET,
        target="incident tracker",
        parameters={
            "title": (
                f"{ctx.incident.incident_id}: "
                f"{diagnosis.most_likely_root_cause if diagnosis else 'payment degradation'}"
            ),
            "description": diagnosis.narrative if diagnosis else "",
            "severity": ctx.incident.severity.value,
        },
        expected_value_paise=1,
        confidence=1.0,
        risk_score=RISK_PRIOR[ActionType.CREATE_INCIDENT_TICKET],
        reversible=True,
        reasoning="Records the incident for human follow-up without changing payment behaviour.",
    )


def _monitoring(ids: _Ids) -> RecoveryStrategy:
    return RecoveryStrategy(
        strategy_id=ids.next(),
        action=ActionType.SET_MONITORING_FREQUENCY,
        target="observation cadence",
        parameters={"interval_seconds": 60},
        expected_value_paise=1,
        confidence=1.0,
        risk_score=RISK_PRIOR[ActionType.SET_MONITORING_FREQUENCY],
        reversible=True,
        reasoning="Observe more frequently while the incident is live. Safe and reversible.",
    )


def _no_action(ids: _Ids, reasoning: str) -> RecoveryStrategy:
    return RecoveryStrategy(
        strategy_id=ids.next(),
        action=ActionType.NO_ACTION,
        target="none",
        parameters={},
        # Zero, not negative: doing nothing is the correct baseline every other option must beat.
        expected_value_paise=0,
        confidence=1.0,
        risk_score=0.0,
        reversible=True,
        reasoning=reasoning,
    )


def generate_strategies(
    ctx: IncidentContext,
    health: dict[str, float],
    *,
    memory: Any = None,
    signature: FailureSignature | None = None,
    profile: MerchantProfile | None = None,
) -> list[RecoveryStrategy]:
    """The closed set of candidate recovery strategies for this diagnosis.

    Which families are offered is driven by the cause, not by what would look impressive: there is
    no reroute on the menu for an issuer decline, because no routing change can make an issuing
    bank approve its own cards.
    """
    diagnosis = ctx.incident.root_cause
    assert diagnosis is not None
    profile = profile or build_profile(ctx.incident.merchant_id)
    ids = _Ids()
    cause = diagnosis.cause_id
    out: list[RecoveryStrategy] = []

    if cause in (
        "psp_degradation",
        "gateway_degradation",
        "latency_timeout_cascade",
        "payment_method_degradation",
    ):
        out.extend(_traffic_shifts(ctx, health, ids, profile, memory, signature))

    if cause in ("config_regression", "checkout_client_issue"):
        rollback = _rollback(ctx, ids, memory, signature)
        if rollback:
            out.append(rollback)

    if cause == "checkout_client_issue":
        out.append(
            _notify(
                ctx,
                ids,
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
            _notify(
                ctx,
                ids,
                subject="Issuer-side payment failures detected",
                body=(
                    f"{diagnosis.most_likely_root_cause}. This originates at the issuing bank and "
                    "cannot be resolved by routing changes. Recommend contacting the acquirer and "
                    "considering a temporary issuer-level fallback."
                ),
            )
        )
        retry = _retry(ctx, ids, memory, signature)
        if retry:
            out.append(retry)

    if cause == "traffic_mix_shift":
        out.append(
            _no_action(
                ids,
                "Success rate fell because the traffic mix changed, not because anything broke. "
                "Rerouting healthy traffic would add risk without adding revenue.",
            )
        )

    if cause == "multi_factor":
        out.append(
            _no_action(
                ids,
                "Multiple independent degradations are in progress and no single intervention "
                "addresses them. This needs a human.",
            )
        )

    if cause not in ("traffic_mix_shift",):
        out.append(_ticket(ctx, ids))
    out.append(_monitoring(ids))
    if all(s.action is not ActionType.NO_ACTION for s in out):
        out.append(_no_action(ids, "Continue observing without intervening."))
    return out


# --------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------


class RecoveryStrategyAgent(Agent):
    """Builds the priced option set and the historical case for and against each option."""

    name = "recovery_strategy"
    state = IncidentState.RECOVERY_PLANNING

    def run(self, ctx: IncidentContext) -> AgentResult:
        incident = ctx.incident
        if incident.root_cause is None or incident.impact is None or incident.anomaly is None:
            return AgentResult(
                ok=False, summary="incomplete incident state", error="missing_inputs"
            )

        health = route_health(ctx)
        ctx.scratch["route_health"] = health

        signature = build_signature(incident, route_health=health)
        policy = getattr(ctx.gateway, "policy", {}) or {}
        records = ctx.memory.all() if ctx.memory is not None else []
        profile = build_profile(incident.merchant_id, policy=policy, records=records)

        # Pull the historical outcomes through the tool registry as well, so the historical claims
        # this agent makes appear in the audit trail as a recorded tool call rather than as an
        # assertion. Both read the same deterministic store and cannot disagree; a test asserts
        # that the numbers in the plan match the numbers in this call.
        recorded = ctx.call_tool(
            "get_action_outcomes", signature=signature.model_dump(mode="json")
        )
        ctx.scratch["recorded_action_outcomes"] = recorded or {}

        strategies = generate_strategies(
            ctx, health, memory=ctx.memory, signature=signature, profile=profile
        )
        if not strategies:
            strategies = [
                _no_action(_Ids(), "No intervention is available for this diagnosis.")
            ]

        similar, similar_count = [], 0
        if ctx.memory is not None and hasattr(ctx.memory, "find_similar_incidents"):
            matches = ctx.memory.find_similar_incidents(signature, limit=200)
            similar_count = len(matches)
            similar = [m.as_dict() for m in matches[:12]]

        plan = RecoveryPlan(
            incident_id=incident.incident_id,
            signature=signature,
            strategies=strategies,
            similar_incidents=similar,
            similar_incident_count=similar_count,
            historical_recommendation=_recommend(strategies, similar),
            memory_size=len(records),
            profile_applied={
                "risk_tolerance": profile.risk_tolerance,
                "max_traffic_shift_pct": profile.max_traffic_shift_pct,
                "preferred_shift_pct": profile.preferred_shift_pct,
                "incidents_seen": profile.incidents_seen,
                "magnitudes_priced": profile.preferred_magnitudes(DEFAULT_SHIFT_PCT),
                "notes": profile.notes,
            },
        )
        ctx.publish(
            "recovery_plan",
            {
                "incident_id": incident.incident_id,
                "strategies": len(strategies),
                "similar_incidents": len(similar),
                "recommendation": plan.historical_recommendation,
            },
        )
        summary = (
            f"{len(strategies)} recovery strategies priced; "
            f"{len(similar)} comparable incidents in memory"
        )
        return AgentResult(ok=True, summary=summary, output=plan)


def _recommend(strategies: list[RecoveryStrategy], similar: list[dict[str, Any]]) -> str:
    """One line stating what history favours, or plainly saying it has nothing to offer."""
    supported = [
        s
        for s in strategies
        if s.historical_support is not None and s.historical_support.matched_incidents > 0
    ]
    if not supported:
        if similar:
            return (
                f"{len(similar)} comparable incidents are in memory, but none of them tried the "
                "actions available here, so history does not favour any option yet."
            )
        return "No comparable incidents in memory yet; this decision rests on live evidence alone."

    # Ranked on the rate that answers "did this help", then on how much evidence there is. A
    # candidate that helped four times out of four on one incident's worth of evidence should not
    # outrank one that helped nine times out of eleven.
    best = max(
        supported,
        key=lambda s: (
            s.historical_support.stats.helped_rate if s.historical_support.stats else 0.0,
            s.historical_support.matched_incidents,
            # Prefer a candidate whose own size is the size the evidence is actually about.
            1 if s.historical_support.magnitude_matched else 0,
        ),
    )
    stats = best.historical_support.stats
    assert stats is not None
    # Quote the band the evidence covers, not the candidate's size. Where the stats came from the
    # action overall rather than from this magnitude, saying "at 15%" would attribute to one size
    # a result that was measured across all of them.
    where = "" if stats.magnitude_band == "any" else f" at {stats.magnitude_band}"
    line = (
        f"Historical recommendation: {best.action.value}{where} has performed best under similar "
        f"conditions - {stats.helped} of {stats.attempts} comparable incidents improved "
        f"({stats.helped_rate:.0%}), {stats.successes} of them fully recovered."
    )
    if stats.median_recovery_time_s:
        line += f" Median recovery {stats.median_recovery_time_s / 60:.1f} min."
    if stats.median_revenue_protected_paise:
        line += f" Median revenue protected {format_inr(stats.median_revenue_protected_paise)}."

    worse = [
        s
        for s in supported
        if s is not best
        and s.action is best.action
        and s.magnitude is not None
        and best.magnitude is not None
        and s.magnitude > best.magnitude
        and s.historical_support.stats is not None
        and s.historical_support.stats.helped_rate < stats.helped_rate
    ]
    if worse:
        other = max(worse, key=lambda s: s.magnitude or 0.0)
        assert other.historical_support is not None and other.historical_support.stats is not None
        line += (
            f" Larger shifts have done worse here: {other.magnitude:g}% helped "
            f"{other.historical_support.stats.helped} of "
            f"{other.historical_support.stats.attempts}."
        )
    return line


def band_of(magnitude: float | None) -> str:
    """Re-exported so callers reporting a strategy do not import the schema module for one name."""
    return magnitude_band(magnitude)
