"""Revenue accounting for one incident.

The primary business metric of this system is not success rate, it is money, and this module is
where the money is turned from per-hour rates into what an incident actually cost and saved. It is
deliberately a small pure module with no agent, no model and no I/O: every figure is arithmetic
over measurements other components already recorded, so a merchant can check the sums.

**The three figures are disjoint, which is what stops double counting.**

    revenue_at_risk     what the degradation threatened, over the incident's own duration
    revenue_protected   future loss the intervention prevented, for the time it was in force
    revenue_recovered   payments that had already failed and were then completed

`protected` is forward-looking and `recovered` is backward-looking, so a rupee can appear in only
one of them. `lost` is the remainder, and the recovery rate is what fraction of the exposure the
system converted into one of the two good outcomes.

**Rates are only ever applied to the time they were measured over.** Revenue at risk is a per-hour
figure measured from onset; it is charged for the seconds the incident was actually open. Revenue
protected is a per-hour figure measured after the intervention; it is credited only for the seconds
between the intervention and the incident closing. Applying either rate to the whole incident is
how a system reports protecting more than it ever risked.
"""

from __future__ import annotations

from datetime import datetime

from .schemas import IncidentRecord, RevenueOutcome, VerificationStatus
from .agents.impact import format_inr


def compute_revenue_outcome(
    incident: IncidentRecord,
    *,
    now: datetime | None = None,
    executed_at: datetime | None = None,
) -> RevenueOutcome:
    """The financial story of `incident`, from its own recorded measurements.

    Returns a `measurable=False` outcome with every figure zero when the incident never produced
    an impact assessment. An unpriced incident has no financial story, and inventing one is
    precisely the failure mode this system exists to avoid.
    """
    end = incident.closed_at or now or incident.opened_at
    impact = incident.impact

    if impact is None or impact.revenue_at_risk_per_hour_paise <= 0:
        return RevenueOutcome(
            incident_id=incident.incident_id,
            measurable=False,
            exposure_seconds=max(0.0, (end - incident.opened_at).total_seconds()),
            calculation=[
                "No impact assessment was produced for this incident, so there is no revenue "
                "figure to report. Nothing is estimated in its place."
            ],
        )

    exposure_s = max(0.0, (end - incident.opened_at).total_seconds())
    at_risk_rate = impact.revenue_at_risk_per_hour_paise
    at_risk = int(at_risk_rate * exposure_s / 3600.0)

    verification = incident.verification
    protected_rate = 0
    protected_s = 0.0
    if verification is not None and verification.status in (
        VerificationStatus.RECOVERED,
        VerificationStatus.PARTIALLY_RECOVERED,
    ):
        protected_rate = verification.estimated_revenue_protected_per_hour_paise
        start = executed_at or (
            incident.action_result.executed_at if incident.action_result else None
        )
        if start is not None and end > start:
            protected_s = (end - start).total_seconds()
        else:
            # No recorded execution timestamp: credit the intervention for nothing rather than
            # for the whole incident. Under-reporting is the safe direction here.
            protected_s = 0.0
    protected = int(protected_rate * protected_s / 3600.0)

    recovery = incident.order_recovery
    recovered = recovery.recovered_value_paise if recovery is not None else 0

    # An intervention cannot protect more than the incident threatened, and recovered payments are
    # drawn from the same exposure. Capping here rather than trusting the inputs keeps the identity
    # `at_risk = protected + recovered + lost` true by construction.
    good = min(at_risk, protected + recovered)
    capped = good < protected + recovered
    uncapped_protected, uncapped_recovered = protected, recovered
    if capped and (protected + recovered) > 0:
        scale = good / (protected + recovered)
        protected = int(protected * scale)
        recovered = good - protected
    lost = max(0, at_risk - protected - recovered)
    rate = round(good / at_risk, 4) if at_risk > 0 else 0.0

    calculation = [
        f"Exposure: {exposure_s / 60:.1f} min open x "
        f"{format_inr(at_risk_rate)}/hour at risk = {format_inr(at_risk)} threatened.",
    ]
    if protected_rate:
        calculation.append(
            f"Protected: {protected_s / 60:.1f} min under the intervention x "
            f"{format_inr(protected_rate)}/hour measured against the control = "
            f"{format_inr(protected)}."
        )
    else:
        calculation.append(
            "Protected: nothing, because no intervention was verified to have worked."
        )
    if recovery is not None and recovery.executed:
        calculation.append(
            f"Recovered: {recovery.recovered:,} of {recovery.recoverable_payments:,} recoverable "
            f"failed payments completed = {format_inr(recovered)}."
        )
    if capped:
        # Otherwise a reader sees the recovery stage report one figure and the ledger another,
        # with nothing explaining the gap.
        calculation.append(
            f"Capped: {format_inr(uncapped_protected)} protected and "
            f"{format_inr(uncapped_recovered)} recovered together exceed the "
            f"{format_inr(at_risk)} this incident threatened, so both are scaled down to fit it. "
            "An incident cannot save more than it put at risk."
        )
    calculation.append(
        f"Lost: {format_inr(at_risk)} threatened - {format_inr(protected)} protected - "
        f"{format_inr(recovered)} recovered = {format_inr(lost)}."
    )
    calculation.append(
        f"Recovery rate: ({format_inr(protected)} + {format_inr(recovered)}) / "
        f"{format_inr(at_risk)} = {rate:.0%}."
    )

    return RevenueOutcome(
        incident_id=incident.incident_id,
        revenue_at_risk_paise=at_risk,
        revenue_at_risk_per_hour_paise=at_risk_rate,
        revenue_protected_paise=protected,
        revenue_recovered_paise=recovered,
        revenue_lost_paise=lost,
        recovery_rate=rate,
        exposure_seconds=round(exposure_s, 1),
        protected_seconds=round(protected_s, 1),
        calculation=calculation,
        measurable=True,
    )


def aggregate(outcomes: list[RevenueOutcome]) -> dict[str, int | float]:
    """Portfolio totals across incidents. The recovery rate is recomputed, never averaged.

    Averaging per-incident rates would let a trivial incident that recovered perfectly cancel out
    a large one that did not — the arithmetic mean of percentages is not a percentage of anything.
    """
    at_risk = sum(o.revenue_at_risk_paise for o in outcomes)
    protected = sum(o.revenue_protected_paise for o in outcomes)
    recovered = sum(o.revenue_recovered_paise for o in outcomes)
    lost = sum(o.revenue_lost_paise for o in outcomes)
    return {
        "incidents": len(outcomes),
        "revenue_at_risk_paise": at_risk,
        "revenue_protected_paise": protected,
        "revenue_recovered_paise": recovered,
        "revenue_lost_paise": lost,
        "recovery_rate": round(min(1.0, (protected + recovered) / at_risk), 4)
        if at_risk > 0
        else 0.0,
    }
