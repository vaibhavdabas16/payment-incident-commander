"""The learned playbook: what the system would tell you it now knows.

`store.py` answers questions an agent asks mid-incident — *how has this action performed under
conditions like these?* This module answers the question a person asks between incidents: *what
has this system actually learned, and would I agree with it?*

It is a read-only projection over stored records. It computes nothing about the present, it holds
no state, and it cannot influence a decision: the strategy layer queries `store.py` directly, so
nothing here sits on the path from a proposal to an execution. If this file were deleted the
agent's behaviour would be identical and only the Learning page would go dark.

Two things it is careful about.

**It reports a preference only where the evidence separates one.** A playbook entry needs a
minimum number of comparable incidents and a magnitude band that measurably beat another; below
that it says there is not enough evidence yet, which is the honest answer and the more useful one.

**It never mixes merchants.** Every aggregation is scoped, for the same reason retrieval is.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..schemas import IncidentOutcomeRecord, magnitude_band

# Below this many comparable incidents a "preference" is a coincidence with a percentage attached.
MIN_EVIDENCE = 3

# How much better one magnitude band has to be before the playbook will advise against another.
# Two bands within this are reported as not yet separated rather than force-ranked.
MIN_SEPARATION = 0.20

# ...and the band being advised against has to have actually failed more than once. A rate gap
# alone is not enough: at four attempts a single differing outcome is a 25-point difference, and
# telling a merchant to avoid an approach on the strength of one bad day is exactly the kind of
# confident nonsense that makes an operator stop reading the page.
MIN_FAILURES_TO_AVOID = 2


@dataclass
class BandOutcome:
    """How one magnitude band of one action has performed."""

    band: str
    attempts: int = 0
    helped: int = 0
    fully_recovered: int = 0
    rollbacks: int = 0
    median_recovery_s: float | None = None
    revenue_protected_paise: int = 0
    revenue_at_risk_paise: int = 0

    @property
    def helped_rate(self) -> float:
        return round(self.helped / self.attempts, 4) if self.attempts else 0.0

    @property
    def rollback_rate(self) -> float:
        return round(self.rollbacks / self.attempts, 4) if self.attempts else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "band": self.band,
            "attempts": self.attempts,
            "helped": self.helped,
            "fully_recovered": self.fully_recovered,
            "rollbacks": self.rollbacks,
            "helped_rate": self.helped_rate,
            "rollback_rate": self.rollback_rate,
            "median_recovery_s": self.median_recovery_s,
            "revenue_protected_paise": self.revenue_protected_paise,
            "revenue_at_risk_paise": self.revenue_at_risk_paise,
        }


@dataclass
class PlaybookEntry:
    """What the system has learned about one kind of failure."""

    signature_key: str
    label: str
    payment_method: str | None
    provider: str | None
    root_cause_id: str
    incidents: int = 0
    seeded_incidents: int = 0
    revenue_at_risk_paise: int = 0
    revenue_protected_paise: int = 0
    revenue_recovered_paise: int = 0
    incident_ids: list[str] = field(default_factory=list)
    bands: list[BandOutcome] = field(default_factory=list)
    preferred_action: str | None = None
    preferred_band: str | None = None
    preferred_helped_rate: float = 0.0
    preferred_median_recovery_s: float | None = None
    avoid_band: str | None = None
    avoid_reason: str = ""
    confident: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "signature_key": self.signature_key,
            "label": self.label,
            "payment_method": self.payment_method,
            "provider": self.provider,
            "root_cause_id": self.root_cause_id,
            "incidents": self.incidents,
            "seeded_incidents": self.seeded_incidents,
            "revenue_at_risk_paise": self.revenue_at_risk_paise,
            "revenue_protected_paise": self.revenue_protected_paise,
            "revenue_recovered_paise": self.revenue_recovered_paise,
            "recovery_rate": round(
                min(
                    1.0,
                    (self.revenue_protected_paise + self.revenue_recovered_paise)
                    / self.revenue_at_risk_paise,
                ),
                4,
            )
            if self.revenue_at_risk_paise
            else 0.0,
            "incident_ids": self.incident_ids,
            "bands": [b.as_dict() for b in self.bands],
            "preferred_action": self.preferred_action,
            "preferred_band": self.preferred_band,
            "preferred_helped_rate": self.preferred_helped_rate,
            "preferred_median_recovery_s": self.preferred_median_recovery_s,
            "avoid_band": self.avoid_band,
            "avoid_reason": self.avoid_reason,
            "confident": self.confident,
            "note": self.note,
        }


def build_playbook(
    records: Iterable[IncidentOutcomeRecord], *, merchant_id: str | None = None
) -> list[PlaybookEntry]:
    """One entry per failure kind, most expensive first."""
    scoped = [
        r
        for r in records
        if (merchant_id is None or r.merchant_id == merchant_id) and not r.false_positive
    ]
    groups: dict[str, list[IncidentOutcomeRecord]] = defaultdict(list)
    for record in scoped:
        groups[record.failure_signature.key()].append(record)

    entries = [_entry(key, rows) for key, rows in groups.items()]
    entries.sort(key=lambda e: e.revenue_at_risk_paise, reverse=True)
    return entries


def _entry(key: str, rows: list[IncidentOutcomeRecord]) -> PlaybookEntry:
    signature = rows[0].failure_signature
    entry = PlaybookEntry(
        signature_key=key,
        label=signature.label(),
        payment_method=signature.payment_method,
        provider=signature.psp or signature.issuer or signature.gateway,
        root_cause_id=signature.root_cause_id,
        incidents=len(rows),
        seeded_incidents=sum(1 for r in rows if r.seeded),
        revenue_at_risk_paise=sum(r.revenue_at_risk_paise for r in rows),
        revenue_protected_paise=sum(r.revenue_protected_paise for r in rows),
        revenue_recovered_paise=sum(r.revenue_recovered_paise for r in rows),
        incident_ids=[r.incident_id for r in rows][:40],
    )

    # Only interventions that changed payment behaviour carry a lesson about what works.
    acted = [r for r in rows if r.actual_action_executed not in ("none", "no_action")]
    by_band: dict[tuple[str, str], list[IncidentOutcomeRecord]] = defaultdict(list)
    for record in acted:
        by_band[(record.actual_action_executed, record.magnitude_band())].append(record)

    for (action, band), band_rows in by_band.items():
        times = [r.time_to_recovery_s for r in band_rows if r.time_to_recovery_s is not None]
        entry.bands.append(
            BandOutcome(
                band=f"{action} · {band}" if band != "any" else action,
                attempts=len(band_rows),
                helped=sum(1 for r in band_rows if r.helped()),
                fully_recovered=sum(1 for r in band_rows if r.succeeded()),
                rollbacks=sum(1 for r in band_rows if r.rollback_required),
                median_recovery_s=round(statistics.median(times), 1) if times else None,
                revenue_protected_paise=sum(r.revenue_protected_paise for r in band_rows),
                revenue_at_risk_paise=sum(r.revenue_at_risk_paise for r in band_rows),
            )
        )
    entry.bands.sort(key=lambda b: (b.helped_rate, b.attempts), reverse=True)

    _decide_preference(entry, by_band)
    return entry


def _decide_preference(
    entry: PlaybookEntry, by_band: dict[tuple[str, str], list[IncidentOutcomeRecord]]
) -> None:
    """Name a preferred intervention only where the evidence actually separates one."""
    if not entry.bands:
        entry.note = "No intervention has been executed against this failure yet."
        return

    eligible = [b for b in entry.bands if b.attempts >= MIN_EVIDENCE]
    if not eligible:
        best = entry.bands[0]
        entry.note = (
            f"Only {best.attempts} comparable incident"
            f"{'' if best.attempts == 1 else 's'} so far — too few to prefer an approach. "
            f"A playbook entry needs {MIN_EVIDENCE}."
        )
        return

    best = eligible[0]
    action, band = next(k for k, v in by_band.items() if _matches(best, k, v))
    entry.preferred_action = action
    entry.preferred_band = band
    entry.preferred_helped_rate = best.helped_rate
    entry.preferred_median_recovery_s = best.median_recovery_s
    entry.confident = True

    # Advise against a band only when it is comparably evidenced, measurably worse, *and* has
    # failed more than once. A single bad outcome is not grounds to tell a merchant to avoid
    # something.
    worse = [
        b
        for b in eligible
        if b is not best
        and best.helped_rate - b.helped_rate >= MIN_SEPARATION
        and (b.attempts - b.helped) >= MIN_FAILURES_TO_AVOID
    ]
    if worse:
        avoid = min(worse, key=lambda b: b.helped_rate)
        entry.avoid_band = avoid.band
        entry.avoid_reason = (
            f"{avoid.helped}/{avoid.attempts} improved payments against "
            f"{best.helped}/{best.attempts} for {best.band}"
            + (
                f", and {avoid.rollbacks} had to be rolled back"
                if avoid.rollbacks
                else ""
            )
            + "."
        )
    entry.note = (
        f"{best.helped} of {best.attempts} comparable incidents improved with {best.band}"
        + (f", {best.fully_recovered} fully recovered" if best.fully_recovered else "")
        + "."
    )


def _matches(outcome: BandOutcome, key: tuple[str, str], rows: list) -> bool:
    action, band = key
    label = f"{action} · {band}" if band != "any" else action
    return label == outcome.band and len(rows) == outcome.attempts


def effectiveness_by(
    records: Iterable[IncidentOutcomeRecord], dimension: str, *, merchant_id: str | None = None
) -> list[dict[str, Any]]:
    """Most effective action per payment method, or per provider.

    `dimension` is a field of `FailureSignature`. Rows where it is unset are skipped rather than
    bucketed under "unknown", which would invent a category the data does not have.
    """
    scoped = [
        r
        for r in records
        if (merchant_id is None or r.merchant_id == merchant_id)
        and not r.false_positive
        and r.actual_action_executed not in ("none", "no_action")
    ]
    groups: dict[str, list[IncidentOutcomeRecord]] = defaultdict(list)
    for record in scoped:
        value = getattr(record.failure_signature, dimension, None)
        if value:
            groups[str(value)].append(record)

    out: list[dict[str, Any]] = []
    for value, rows in groups.items():
        by_action: dict[str, list[IncidentOutcomeRecord]] = defaultdict(list)
        for record in rows:
            by_action[record.actual_action_executed].append(record)
        best_action, best_rows = max(
            by_action.items(),
            key=lambda kv: (
                sum(1 for r in kv[1] if r.helped()) / len(kv[1]),
                len(kv[1]),
            ),
        )
        helped = sum(1 for r in best_rows if r.helped())
        out.append(
            {
                dimension: value,
                "incidents": len(rows),
                "best_action": best_action,
                "best_action_band": _dominant_band(best_rows),
                "attempts": len(best_rows),
                "helped": helped,
                "helped_rate": round(helped / len(best_rows), 4),
                "revenue_protected_paise": sum(r.revenue_protected_paise for r in rows),
            }
        )
    out.sort(key=lambda r: r["revenue_protected_paise"], reverse=True)
    return out


def _dominant_band(rows: list[IncidentOutcomeRecord]) -> str:
    bands = [magnitude_band(r.intervention_magnitude) for r in rows]
    bands = [b for b in bands if b != "any"]
    if not bands:
        return "any"
    return max(set(bands), key=bands.count)


def influenced_by_history(
    records: Iterable[IncidentOutcomeRecord], *, merchant_id: str | None = None
) -> list[dict[str, Any]]:
    """Incidents that were decided with comparable history already on the record.

    Ordered oldest first, so a reader can see the point at which the system stopped deciding on
    live evidence alone. The "informed by" count is how many earlier records shared the signature
    at the time — computed here, not stored, because it is a property of the sequence rather than
    of any one incident.
    """
    scoped = sorted(
        (
            r
            for r in records
            if (merchant_id is None or r.merchant_id == merchant_id) and not r.false_positive
        ),
        key=lambda r: r.timestamp,
    )
    seen: dict[str, int] = defaultdict(int)
    out: list[dict[str, Any]] = []
    for record in scoped:
        key = record.failure_signature.key()
        prior = seen[key]
        seen[key] += 1
        if prior == 0:
            continue
        out.append(
            {
                "incident_id": record.incident_id,
                "at": record.timestamp.isoformat(),
                "label": record.failure_signature.label(),
                "informed_by": prior,
                "action": record.actual_action_executed,
                "magnitude": record.intervention_magnitude,
                "verification": record.verification_result,
                "helped": record.helped(),
                "recovery_rate": record.recovery_rate,
                "seeded": record.seeded,
            }
        )
    return out
