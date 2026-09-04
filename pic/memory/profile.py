"""Merchant operating profile.

Two merchants with the same outage should not get the same recommendation. A jewellery merchant
doing a hundred high-value card payments an hour and a food-delivery merchant doing fifty thousand
low-value UPI payments have genuinely different answers to "is a 20% reroute worth the risk", and
a system that gives them the same answer is wrong for at least one of them.

The profile is **derived, never authored**. Every field comes from one of two places:

* the merchant's own policy file — what they permit, how much they allow to move at once, what
  needs a human, which routes are eligible;
* their own incident history — which destinations have actually worked, what magnitude of shift
  has succeeded here, how often a person has had to step in.

There are no hand-written per-merchant preferences anywhere in this file, because a preference
somebody typed into the source is not a merchant profile, it is a hard-coded opinion.

Like everything else in the learning subsystem, the profile is advisory: it shapes which options
are *generated* and how they are described. The policy gateway still decides what may run.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from ..schemas import FailureSignature, IncidentOutcomeRecord

# Below this many remembered incidents the historical half of the profile is not used: three
# outcomes is not a preference, it is a coincidence.
MIN_HISTORY_FOR_PREFERENCE = 3

# Risk appetite is read off the merchant's own ceilings rather than declared. These are the
# boundaries of the bands, expressed in the same units the policy file uses.
CONSERVATIVE_SHIFT_CEILING_PCT = 15.0
AGGRESSIVE_SHIFT_FLOOR_PCT = 30.0
CONSERVATIVE_RISK_CEILING = 0.45
AGGRESSIVE_RISK_FLOOR = 0.70


@dataclass
class MerchantProfile:
    """How this merchant, specifically, wants payment incidents handled."""

    merchant_id: str
    # --- from policy ---
    risk_tolerance: str = "balanced"  # conservative | balanced | aggressive
    max_traffic_shift_pct: float = 20.0
    eligible_routes: list[str] = field(default_factory=list)
    autonomous_actions: list[str] = field(default_factory=list)
    approval_required_for: list[str] = field(default_factory=list)
    # --- from history ---
    incidents_seen: int = 0
    preferred_shift_pct: float | None = None
    preferred_destinations: list[str] = field(default_factory=list)
    avoid_destinations: list[str] = field(default_factory=list)
    payment_method_priorities: list[str] = field(default_factory=list)
    human_intervention_rate: float = 0.0
    historical_recovery_rate: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "risk_tolerance": self.risk_tolerance,
            "max_traffic_shift_pct": self.max_traffic_shift_pct,
            "eligible_routes": self.eligible_routes,
            "autonomous_actions": self.autonomous_actions,
            "approval_required_for": self.approval_required_for,
            "incidents_seen": self.incidents_seen,
            "preferred_shift_pct": self.preferred_shift_pct,
            "preferred_destinations": self.preferred_destinations,
            "avoid_destinations": self.avoid_destinations,
            "payment_method_priorities": self.payment_method_priorities,
            "human_intervention_rate": self.human_intervention_rate,
            "historical_recovery_rate": self.historical_recovery_rate,
            "notes": self.notes,
        }

    def preferred_magnitudes(self, default: float) -> list[float]:
        """The shift sizes worth pricing for this merchant, smallest first.

        Always includes the default and the merchant's ceiling, so the option set spans the range
        they permit rather than only the size that happened to work last time — history should
        argue for a magnitude in front of a human, not quietly remove the alternatives.
        """
        candidates = {round(default, 1), round(self.max_traffic_shift_pct, 1)}
        if self.preferred_shift_pct:
            candidates.add(round(self.preferred_shift_pct, 1))
        # A conservative merchant gets a smaller option priced too; an aggressive one a larger.
        if self.risk_tolerance == "conservative":
            candidates.add(round(max(5.0, default * 0.5), 1))
        elif self.risk_tolerance == "aggressive":
            candidates.add(round(min(self.max_traffic_shift_pct, default * 2), 1))
        return sorted(m for m in candidates if m > 0)


def build_profile(
    merchant_id: str,
    policy: dict[str, Any] | None = None,
    records: list[IncidentOutcomeRecord] | None = None,
) -> MerchantProfile:
    """Derive the profile from merchant policy and this merchant's own remembered outcomes."""
    policy = policy or {}
    records = [r for r in (records or []) if r.merchant_id == merchant_id]

    bounds = policy.get("bounds", {}) or {}
    thresholds = policy.get("thresholds", {}) or {}
    routing = policy.get("routing", {}) or {}
    allowed = list(policy.get("allowed_actions", []) or [])
    autonomous = list(policy.get("autonomous_actions", []) or [])

    max_shift = float(bounds.get("max_traffic_shift_pct", 20) or 20)
    max_risk = float(thresholds.get("max_autonomous_risk_score", 0.6) or 0.6)

    profile = MerchantProfile(
        merchant_id=merchant_id,
        risk_tolerance=_risk_tolerance(max_shift, max_risk),
        max_traffic_shift_pct=max_shift,
        eligible_routes=list(routing.get("eligible_routes", []) or []),
        autonomous_actions=autonomous,
        approval_required_for=sorted(set(allowed) - set(autonomous)),
        incidents_seen=len(records),
    )

    if not records:
        profile.notes.append(
            "No incident history for this merchant yet, so only their policy shapes the options."
        )
        return profile

    successful_shifts = [
        r.intervention_magnitude
        for r in records
        if r.actual_action_executed == "shift_traffic"
        and r.intervention_magnitude
        and r.helped()
    ]
    failed_shifts = [
        r.intervention_magnitude
        for r in records
        if r.actual_action_executed == "shift_traffic"
        and r.intervention_magnitude
        and not r.helped()
    ]
    if len(successful_shifts) >= MIN_HISTORY_FOR_PREFERENCE:
        profile.preferred_shift_pct = round(statistics.median(successful_shifts), 1)
        profile.notes.append(
            f"{len(successful_shifts)} successful traffic shifts here have a median size of "
            f"{profile.preferred_shift_pct:g}%."
        )
    elif successful_shifts:
        profile.notes.append(
            f"Only {len(successful_shifts)} successful shift(s) recorded — too few to prefer a "
            "size on history alone."
        )
    if failed_shifts and len(failed_shifts) >= MIN_HISTORY_FOR_PREFERENCE:
        profile.notes.append(
            f"{len(failed_shifts)} shifts here did not work, median size "
            f"{statistics.median(failed_shifts):g}%."
        )

    # Destinations that have actually held up under redirected load, and ones that have not.
    good: dict[str, int] = {}
    bad: dict[str, int] = {}
    for record in records:
        destination = record.executed_parameters.get("to_route")
        if not destination:
            continue
        bucket = good if record.helped() else bad
        bucket[destination] = bucket.get(destination, 0) + 1
    profile.preferred_destinations = sorted(good, key=lambda r: good[r], reverse=True)
    profile.avoid_destinations = sorted(
        (r for r in bad if bad[r] > good.get(r, 0)), key=lambda r: bad[r], reverse=True
    )

    # Which payment methods this merchant loses the most money on. Not a declared preference —
    # the ranking is by rupees actually at risk in their own incidents.
    by_method: dict[str, int] = {}
    for record in records:
        method = record.failure_signature.payment_method
        if method:
            by_method[method] = by_method.get(method, 0) + record.revenue_at_risk_paise
    profile.payment_method_priorities = sorted(by_method, key=lambda m: by_method[m], reverse=True)

    profile.human_intervention_rate = round(
        sum(1 for r in records if r.human_intervention_required) / len(records), 4
    )
    at_risk = sum(r.revenue_at_risk_paise for r in records)
    good_money = sum(r.revenue_protected_paise + r.revenue_recovered_paise for r in records)
    profile.historical_recovery_rate = (
        round(min(1.0, good_money / at_risk), 4) if at_risk > 0 else 0.0
    )
    return profile


def _risk_tolerance(max_shift_pct: float, max_risk_score: float) -> str:
    """Read risk appetite off the merchant's own ceilings.

    A merchant who will not let more than 15% of traffic move and refuses anything above a 0.45
    risk score has told us they are conservative, in the only language the system should be
    listening to — their policy.
    """
    if max_shift_pct <= CONSERVATIVE_SHIFT_CEILING_PCT or max_risk_score <= CONSERVATIVE_RISK_CEILING:
        return "conservative"
    if max_shift_pct >= AGGRESSIVE_SHIFT_FLOOR_PCT and max_risk_score >= AGGRESSIVE_RISK_FLOOR:
        return "aggressive"
    return "balanced"


def relevant_history(
    records: list[IncidentOutcomeRecord], signature: FailureSignature
) -> list[IncidentOutcomeRecord]:
    """This merchant's records only. A convenience so callers cannot forget the scoping."""
    return [r for r in records if r.merchant_id == signature.merchant_id]
