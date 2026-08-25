"""Business Impact Agent.

Translates a technical degradation into money, and shows its working. Every figure it emits is
accompanied by the arithmetic that produced it, because a revenue number an operator cannot check
is a number they will not act on — and because "do not invent numbers" has to be verifiable.

This agent deliberately measures over a longer window than the detector. Detection optimises for
latency and will fire on two minutes of data; a rupee estimate from two minutes of a heavy-tailed
amount distribution is far too noisy to put in front of a merchant. The detector's figure is
triage; this one is the reported number.
"""

from __future__ import annotations

from datetime import timedelta

from ..schemas import AgentResult, ImpactAssessment, IncidentState
from .base import Agent, IncidentContext

# Below this many attempts the rupee estimate is flagged as provisional in its assumptions.
MIN_SAMPLE_FOR_STABLE_ESTIMATE = 400
# Horizon for the "if nobody intervenes" projection.
PROJECTION_MINUTES = 60


def format_inr(paise: int | float) -> str:
    """Indian-format currency for operator-facing text (lakh/crore, as merchants read it)."""
    rupees = paise / 100.0
    if abs(rupees) >= 1_00_00_000:
        return f"INR {rupees / 1_00_00_000:.2f} Cr"
    if abs(rupees) >= 1_00_000:
        return f"INR {rupees / 1_00_000:.1f}L"
    return f"INR {rupees:,.0f}"


class ImpactAgent(Agent):
    name = "impact"
    state = IncidentState.IMPACT_ASSESSED

    def run(self, ctx: IncidentContext) -> AgentResult:
        anomaly = ctx.incident.anomaly
        if anomaly is None:
            return AgentResult(ok=False, summary="no anomaly to assess", error="missing_anomaly")

        # Measure from incident onset to now, and never earlier. Reaching further back to gather
        # samples would mix healthy pre-incident traffic into the estimate and systematically
        # understate the loss - the opposite of the error we can afford to make here.
        end = ctx.now
        start = anomaly.window_start
        window_seconds = max(1.0, (end - start).total_seconds())

        base = ctx.detector.baseline(start)
        if not base.windows:
            return AgentResult(
                ok=False,
                summary="insufficient baseline history to estimate impact",
                error="insufficient_baseline",
            )

        current = ctx.store.metric_window(start, end)
        baseline_window = ctx.store.metric_window(base.start, base.end)
        if current.total == 0 or baseline_window.total == 0:
            return AgentResult(ok=False, summary="no traffic in window", error="no_traffic")

        window_hours = window_seconds / 3600.0
        baseline_rate = base.ewma_baseline or baseline_window.success_rate
        drop = max(0.0, baseline_rate - current.success_rate)
        attempts_per_hour = current.total / window_hours

        revenue_at_risk = ctx.detector.estimate_revenue_at_risk(start, end, base)
        excess_failures_per_hour = drop * attempts_per_hour

        avg_order_value = (baseline_window.gmv_paise + baseline_window.failed_gmv_paise) / baseline_window.total
        attempted_gmv_per_hour = (current.gmv_paise + current.failed_gmv_paise) / window_hours

        # One order can be attempted several times; counting distinct orders avoids reporting a
        # customer twice because they retried.
        affected_customers = ctx.store.unique_customers(start, end, failed_only=True)
        affected_customers_per_hour = int(affected_customers / window_hours)

        projected = int(revenue_at_risk * PROJECTION_MINUTES / 60.0)

        calculation = [
            f"Observation window: {window_seconds / 60:.0f} min ending {end:%H:%M:%S} UTC "
            f"({current.total:,} attempts).",
            f"Success rate {current.success_rate:.2%} vs baseline {baseline_rate:.2%} "
            f"= {drop:.2%} shortfall.",
            f"Attempt rate {attempts_per_hour:,.0f}/hour x {drop:.2%} shortfall "
            f"= {excess_failures_per_hour:,.0f} additional failed payments per hour.",
            f"Revenue at risk computed per (payment method x order-value band) stratum against each "
            f"stratum's own baseline, then summed: {format_inr(revenue_at_risk)}/hour.",
            f"Baseline average order value {format_inr(avg_order_value)}; "
            f"attempted GMV {format_inr(attempted_gmv_per_hour)}/hour.",
            f"Unmitigated {PROJECTION_MINUTES}-minute projection: "
            f"{format_inr(revenue_at_risk)}/hr x {PROJECTION_MINUTES / 60:.0f}h "
            f"= {format_inr(projected)}.",
        ]

        assumptions = [
            "Failed payments are assumed lost rather than recovered later by the customer, so this "
            "is an upper bound on true loss.",
            "The pre-incident baseline is assumed to represent healthy performance for this hour.",
            "Revenue is valued per stratum rather than at a blended average, because failures "
            "concentrate in segments whose order values differ substantially.",
            "The projection assumes the degradation continues at its current severity.",
        ]
        if current.total < MIN_SAMPLE_FOR_STABLE_ESTIMATE:
            assumptions.append(
                f"Only {current.total:,} payments observed since onset, so the rupee figure carries "
                "meaningful sampling error and will tighten as the incident develops."
            )
        if ctx.incident.evidence and ctx.incident.evidence.degraded:
            assumptions.append(
                "Some investigation tools failed, so segment attribution behind this estimate is "
                "incomplete."
            )

        assessment = ImpactAssessment(
            incident_id=ctx.incident.incident_id,
            revenue_at_risk_per_hour_paise=revenue_at_risk,
            transactions_at_risk_per_hour=int(excess_failures_per_hour),
            affected_customers_estimate=affected_customers_per_hour,
            affected_gmv_paise=int(attempted_gmv_per_hour),
            projected_loss_if_unmitigated_paise=projected,
            projection_horizon_minutes=PROJECTION_MINUTES,
            calculation=calculation,
            assumptions=assumptions,
        )
        return AgentResult(
            ok=True,
            summary=(
                f"{format_inr(revenue_at_risk)}/hour at risk; "
                f"{int(excess_failures_per_hour):,} extra failures/hour affecting about "
                f"{affected_customers_per_hour:,} customers/hour"
            ),
            output=assessment,
        )
