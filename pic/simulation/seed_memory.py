"""Deterministic seeded history, for demonstrating learning without waiting for it.

Learning is only visible once there is something to have learned from. A merchant who has run one
incident has an empty playbook, which is honest and shows nothing. Running twelve incidents live
takes long enough that nobody watching a demo will wait for it.

So this module builds a fixed set of `IncidentOutcomeRecord`s, and **every one of them is flagged
`seeded=True`**. That flag travels with the record into every query, every aggregate and every
screen that renders one, and the UI labels them. The distinction the product depends on is between
*measured* and *invented*, and a seeded record that announced itself as measured would be exactly
the fabrication this system exists to avoid.

What makes these defensible as a fixture rather than as fake data:

* They describe the same failure the shipped `SCN-UPI-PSP` scenario produces — the same payment
  method, provider, error code and root cause the detector and diagnosis actually emit for it. A
  real incident run afterwards retrieves them because the signature genuinely matches, not because
  anything was special-cased.
* Their internal arithmetic obeys the same identity the live ledger does:
  `at_risk = protected + recovered + lost`. A seeded record cannot express a financial outcome a
  real one could not.
* The pattern they encode — moderate shifts work here, large ones regress — is the thing the demo
  claims the system can learn, stated as data rather than asserted in prose.

Nothing here writes to the payment stream, the control plane or merchant policy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..schemas import FailureSignature, IncidentOutcomeRecord, Severity

# The failure these records describe. Matches what `SCN-UPI-PSP` actually produces, so a live
# incident retrieves them on merit.
SEEDED_SIGNATURE = dict(
    payment_method="upi",
    psp="psp_axis",
    route_id="route_A",
    dominant_error_code="PSP_UNAVAILABLE",
    root_cause_id="psp_degradation",
)

# The seeded population, as (magnitude, verification, rollback, at risk, protected, recovered).
# Money is integer paise. Read this table as the history the demo starts from: a moderate shift
# works here nearly every time, a large one regresses more often than not.
#
# The counts it produces are 8 of 9 for the 20% band and 2 of 5 for the 50% band, which is the
# comparison the learned playbook is supposed to be able to draw.
_MODERATE = [
    (20.0, "RECOVERED", False, 2_640_000, 2_010_000, 310_000),
    (20.0, "RECOVERED", False, 3_120_000, 2_450_000, 220_000),
    (18.0, "RECOVERED", False, 1_980_000, 1_540_000, 190_000),
    (20.0, "PARTIALLY_RECOVERED", False, 4_050_000, 2_180_000, 260_000),
    (22.0, "RECOVERED", False, 2_310_000, 1_860_000, 140_000),
    (20.0, "RECOVERED", False, 2_870_000, 2_290_000, 300_000),
    (15.0, "RECOVERED", False, 1_640_000, 1_270_000, 120_000),
    (20.0, "RECOVERED", False, 3_450_000, 2_760_000, 240_000),
    (20.0, "FAILED", True, 2_190_000, 0, 0),
]

_LARGE = [
    (50.0, "REGRESSED", True, 3_900_000, 0, 0),
    (50.0, "RECOVERED", False, 2_760_000, 2_140_000, 180_000),
    (55.0, "REGRESSED", True, 4_420_000, 0, 0),
    (50.0, "FAILED", True, 3_180_000, 0, 0),
    (48.0, "PARTIALLY_RECOVERED", False, 2_540_000, 1_120_000, 90_000),
]

# Recovery times, in seconds, cycled across the seeded records so the medians the playbook reports
# are a real median over real values rather than one repeated number.
_RECOVERY_TIMES = (238.0, 274.0, 219.0, 402.0, 251.0, 263.0, 208.0, 246.0, 588.0)
_LARGE_RECOVERY_TIMES = (612.0, 331.0, 704.0, 655.0, 489.0)

# The seeded history is dated before the demo starts so it reads as history rather than as
# something that happened during the session.
_ANCHOR = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)


def seeded_records(
    merchant_id: str = "merch_acme", anchor: datetime | None = None
) -> list[IncidentOutcomeRecord]:
    """The full seeded population, oldest first. Deterministic: same input, same records."""
    anchor = anchor or _ANCHOR
    out: list[IncidentOutcomeRecord] = []
    index = 0

    for rows, times, band in ((_MODERATE, _RECOVERY_TIMES, "m"), (_LARGE, _LARGE_RECOVERY_TIMES, "l")):
        for i, (magnitude, verification, rolled_back, at_risk, protected, recovered) in enumerate(rows):
            index += 1
            out.append(
                _record(
                    incident_id=f"SEED-{band}{i + 1:02d}",
                    merchant_id=merchant_id,
                    # Spread backwards through the preceding fortnight, newest last.
                    at=anchor - timedelta(hours=6 * (len(_MODERATE) + len(_LARGE) - index)),
                    magnitude=magnitude,
                    verification=verification,
                    rolled_back=rolled_back,
                    at_risk=at_risk,
                    protected=protected,
                    recovered=recovered,
                    recovery_s=times[i % len(times)],
                )
            )
    out.sort(key=lambda r: r.timestamp)
    return out


def _record(
    *,
    incident_id: str,
    merchant_id: str,
    at: datetime,
    magnitude: float,
    verification: str,
    rolled_back: bool,
    at_risk: int,
    protected: int,
    recovered: int,
    recovery_s: float,
) -> IncidentOutcomeRecord:
    # The same identity the live ledger enforces. A seeded record must not be able to express a
    # financial outcome a measured one could not.
    lost = max(0, at_risk - protected - recovered)
    assert protected + recovered + lost == at_risk

    signature = FailureSignature(
        merchant_id=merchant_id,
        severity=Severity.HIGH,
        degradation_magnitude=0.17,
        latency_shift_ms=1800.0,
        traffic_share=0.42,
        affected_segment_keys=["payment_method=upi&psp=psp_axis"],
        route_health={"route_A": 0.38, "route_B": 0.93, "route_C": 0.91},
        **SEEDED_SIGNATURE,
    )
    return IncidentOutcomeRecord(
        incident_id=incident_id,
        timestamp=at,
        merchant_id=merchant_id,
        failure_signature=signature,
        affected_segments=[{"payment_method": "upi", "psp": "psp_axis"}],
        detected_metrics={
            "success_rate": 0.771,
            "baseline_success_rate": 0.941,
            "deviation": -0.17,
            "dominant_error_code": "PSP_UNAVAILABLE",
        },
        selected_root_cause="Payment service provider degradation on psp=psp_axis",
        selected_root_cause_id="psp_degradation",
        root_cause_confidence=0.88,
        revenue_at_risk_paise=at_risk,
        selected_action="shift_traffic",
        actual_action_executed="shift_traffic",
        executed_parameters={
            "from_route": "route_A",
            "to_route": "route_B",
            "percentage": magnitude,
            "payment_method": "upi",
        },
        intervention_magnitude=magnitude,
        policy_result="APPROVE",
        verification_result=verification,
        verification_significant=verification in ("RECOVERED", "PARTIALLY_RECOVERED", "REGRESSED"),
        revenue_protected_paise=protected,
        revenue_recovered_paise=recovered,
        revenue_lost_paise=lost,
        recovery_rate=round((protected + recovered) / at_risk, 4) if at_risk else 0.0,
        rollback_required=rolled_back,
        rollback_result="SUCCEEDED" if rolled_back else "NOT_REQUIRED",
        time_to_recovery_s=recovery_s,
        human_intervention_required=rolled_back,
        final_resolution=verification,
        seeded=True,
    )


def seed(memory: Any, merchant_id: str = "merch_acme") -> int:
    """Load the seeded population into `memory`, skipping any already present.

    Returns how many were added, so a caller can tell "seeded now" from "already seeded" rather
    than reporting a number that is really an idempotency artefact.
    """
    added = 0
    for record in seeded_records(merchant_id=merchant_id):
        if memory.get(record.incident_id) is None:
            memory.record_incident(record)
            added += 1
    return added


def is_seeded(memory: Any) -> bool:
    return any(r.seeded for r in memory.all())
