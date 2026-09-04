"""Preventive intelligence: noticing that the same thing keeps breaking.

Responding well to an incident is worth less than not having it. Once enough incidents share a
failure signature, the same evidence that told the system how to *recover* also tells it how to
*avoid* — the same PSP, at the same time of day, above the same latency, has cost this merchant a
known number of rupees, and a small preemptive shift has a measured success rate against it.

**The hard boundary: this module recommends and never applies.** It produces a
`PreventionRecommendation`, which is a document. It cannot write to `merchant_policies.yaml`, it
cannot enter the pipeline as an action, and acknowledging one records who acknowledged it and
nothing more. Merchant policy is the merchant's standing grant of authority, and a system that
could widen its own authority because a pattern looked convincing would have no guardrails at all
(ADR-011).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime
from typing import Iterable

from ..schemas import (
    ActionType,
    IncidentOutcomeRecord,
    PreventionRecommendation,
    magnitude_band,
)

# Below this many recurrences there is no pattern, only a coincidence. Three is the smallest
# number that can distinguish "it happened twice" from "it keeps happening".
MIN_OCCURRENCES = 3

# A preemptive shift is smaller than a reactive one by construction: it fires on a prediction
# rather than on a measured degradation, so it should cost less if the prediction is wrong.
PREEMPTIVE_FRACTION = 0.5
MIN_PREEMPTIVE_PCT = 5.0

# Occurrences whose clock times all fall inside this span are reported as a time-of-day pattern.
TIME_CLUSTER_HOURS = 2.0

# Latency is only quoted as a condition when the incidents actually showed one.
MIN_LATENCY_SHIFT_MS = 400.0


def find_patterns(
    records: Iterable[IncidentOutcomeRecord],
    *,
    merchant_id: str | None = None,
    min_occurrences: int = MIN_OCCURRENCES,
) -> list[PreventionRecommendation]:
    """Group remembered incidents by failure signature and describe the ones that recur.

    Returns recommendations ordered by the money the pattern has actually cost, because that is
    the order a merchant would want to fix them in.
    """
    scoped = [r for r in records if merchant_id is None or r.merchant_id == merchant_id]
    groups: dict[str, list[IncidentOutcomeRecord]] = defaultdict(list)
    for record in scoped:
        if record.false_positive:
            # A traffic-mix change is not a fault, and recommending a preemptive reroute against
            # one would be recommending harm on a schedule.
            continue
        groups[record.failure_signature.key()].append(record)

    out: list[PreventionRecommendation] = []
    for key, rows in groups.items():
        if len(rows) < min_occurrences:
            continue
        recommendation = _describe(key, sorted(rows, key=lambda r: r.timestamp))
        if recommendation is not None:
            out.append(recommendation)

    out.sort(key=lambda r: r.historical_revenue_lost_paise, reverse=True)
    return out


def _describe(key: str, rows: list[IncidentOutcomeRecord]) -> PreventionRecommendation | None:
    signature = rows[0].failure_signature
    merchant_id = signature.merchant_id or rows[0].merchant_id

    lost = sum(r.revenue_lost_paise for r in rows)
    at_risk = sum(r.revenue_at_risk_paise for r in rows)

    conditions = _conditions(rows)
    action, parameters, efficacy = _proposed_action(rows)
    if action is None:
        return None

    # Benefit is the money this pattern has actually lost, discounted by how often the proposed
    # action has actually worked. Quoting the full historical loss as the benefit would assume a
    # preemptive action never fails, which nothing in the record supports.
    benefit = int(lost * efficacy)

    evidence = [
        f"{len(rows)} incidents share this signature: {', '.join(r.incident_id for r in rows[:8])}.",
        f"Total revenue at risk across them: {at_risk / 100:,.0f} INR; "
        f"{lost / 100:,.0f} INR was not recovered.",
    ]
    band_note = _action_evidence(rows, action.value)
    if band_note:
        evidence.append(band_note)
    evidence.append(
        f"Estimated benefit = {lost / 100:,.0f} INR historical loss x {efficacy:.0%} observed "
        f"success rate for this action = {benefit / 100:,.0f} INR."
    )

    return PreventionRecommendation(
        recommendation_id=f"PRV-{abs(hash(key)) % 10**8:08d}",
        merchant_id=merchant_id,
        pattern=(
            f"{signature.label()} has degraded {len(rows)} times"
            + (f" {conditions[0]}" if conditions and conditions[0].startswith("between") else "")
            + "."
        ),
        signature_key=key,
        occurrences=len(rows),
        incident_ids=[r.incident_id for r in rows],
        conditions=conditions,
        proposed_action=action,
        proposed_parameters=parameters,
        historical_revenue_lost_paise=lost,
        estimated_benefit_paise=benefit,
        evidence=evidence,
        requires_merchant_approval=True,
        status="PROPOSED",
        created_at=rows[-1].timestamp,
    )


def _conditions(rows: list[IncidentOutcomeRecord]) -> list[str]:
    """The observable conditions these incidents shared, stated only where they really did."""
    out: list[str] = []
    signature = rows[0].failure_signature

    window = _time_cluster([r.timestamp for r in rows])
    if window is not None:
        start, end = window
        out.append(f"between {start:02d}:00 and {end:02d}:00 UTC")

    latencies = [r.failure_signature.latency_shift_ms for r in rows]
    if latencies and min(latencies) >= MIN_LATENCY_SHIFT_MS:
        out.append(f"p95 latency up at least {min(latencies):.0f}ms")

    magnitudes = [r.failure_signature.degradation_magnitude for r in rows]
    if magnitudes:
        out.append(f"success rate down at least {min(magnitudes):.0%}")

    if signature.dominant_error_code:
        out.append(f"failures dominated by {signature.dominant_error_code}")

    healthy = [
        r
        for r in rows
        if r.failure_signature.route_health
        and max(r.failure_signature.route_health.values()) >= 0.80
    ]
    if healthy:
        out.append(
            f"a healthy destination route was available in {len(healthy)} of {len(rows)} cases"
        )
    return out


def _time_cluster(timestamps: list[datetime]) -> tuple[int, int] | None:
    """The hour band the occurrences fall in, when they genuinely cluster.

    Returned only when every occurrence sits inside a `TIME_CLUSTER_HOURS` span — otherwise the
    system would report a time-of-day pattern for incidents scattered across the day, which is the
    kind of confident nonsense that makes an operator stop reading.
    """
    if len(timestamps) < 2:
        return None
    hours = sorted(ts.hour + ts.minute / 60.0 for ts in timestamps)
    if hours[-1] - hours[0] > TIME_CLUSTER_HOURS:
        return None
    return int(hours[0]), int(hours[-1]) + 1


def _proposed_action(
    rows: list[IncidentOutcomeRecord],
) -> tuple[ActionType | None, dict, float]:
    """The action to take preemptively, taken from what has actually worked on this pattern.

    Nothing is proposed when no intervention has ever succeeded here — a pattern the system has
    never fixed is a pattern it should describe to a human, not schedule an action against.
    """
    # "Helped", not "fully recovered": a control-verified partial recovery is evidence the
    # action works on this pattern, and requiring a clean sweep would leave the system unable to
    # recommend preventing a fault it has demonstrably been mitigating for weeks.
    successful = [r for r in rows if r.helped() and r.actual_action_executed != "none"]
    if not successful:
        return None, {}, 0.0

    counts: dict[str, list[IncidentOutcomeRecord]] = defaultdict(list)
    for record in successful:
        counts[record.actual_action_executed].append(record)
    action_name = max(counts, key=lambda a: len(counts[a]))

    attempted = [r for r in rows if r.actual_action_executed == action_name]
    efficacy = len(counts[action_name]) / len(attempted) if attempted else 0.0

    try:
        action = ActionType(action_name)
    except ValueError:
        return None, {}, 0.0

    parameters: dict = {}
    if action is ActionType.SHIFT_TRAFFIC:
        magnitudes = [
            r.intervention_magnitude for r in counts[action_name] if r.intervention_magnitude
        ]
        reactive = statistics.median(magnitudes) if magnitudes else 10.0
        preemptive = max(MIN_PREEMPTIVE_PCT, round(reactive * PREEMPTIVE_FRACTION))
        example = counts[action_name][-1]
        parameters = {
            "from_route": example.executed_parameters.get("from_route"),
            "to_route": example.executed_parameters.get("to_route"),
            "percentage": preemptive,
            "payment_method": example.executed_parameters.get("payment_method"),
        }
        parameters = {k: v for k, v in parameters.items() if v is not None}
    else:
        parameters = dict(counts[action_name][-1].executed_parameters)

    return action, parameters, efficacy


def _action_evidence(rows: list[IncidentOutcomeRecord], action: str) -> str:
    attempted = [r for r in rows if r.actual_action_executed == action]
    if not attempted:
        return ""
    helped = [r for r in attempted if r.helped()]
    full = [r for r in helped if r.succeeded()]
    bands = {magnitude_band(r.intervention_magnitude) for r in helped}
    band_text = f" at {', '.join(sorted(bands))}" if bands - {"any"} else ""
    return (
        f"{action} was tried {len(attempted)} times against this pattern and improved payments in "
        f"{len(helped)} of them{band_text} ({len(full)} recovered fully)."
    )
