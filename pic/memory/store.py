"""Incident memory.

Closed incidents are stored with a structured feature vector and retrieved by deterministic
weighted matching — no embeddings, no vector database (ADR-006). At this corpus size an exact
agreement on `(error_code, psp, issuer)` is both a better match and an explainable one: the system
can say *"this resembles INC-0031: same PSP, same error code"* rather than *"cosine similarity
0.83"*.

Memory informs but never decides. Retrieved priors adjust hypothesis scores by at most ±0.15
(`MAX_MEMORY_ADJUSTMENT` in the Root Cause Agent), so a run of similar past incidents can tilt a
close call without ever overriding what the live evidence says. An agent that trusts its history
over its instruments will confidently misdiagnose the first incident that looks familiar but isn't.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..database import IncidentMemoryRow, dumps, session_scope
from ..schemas import IncidentRecord, VerificationStatus, utcnow

# Feature weights. Error code and provider identity carry the most signal because they are the
# things that actually repeat across incidents; geography and device repeat by coincidence.
FEATURE_WEIGHTS = {
    "dominant_error_code": 0.30,
    "psp": 0.18,
    "issuer": 0.15,
    "payment_method": 0.12,
    "route_id": 0.08,
    "app_version": 0.07,
    "os": 0.04,
    "geography": 0.03,
    "amount_band": 0.03,
}

# Numeric features compared by closeness rather than equality.
NUMERIC_WEIGHTS = {
    "deviation": 0.06,
    "latency_shift_ms": 0.04,
}

MIN_SIMILARITY = 0.35


@dataclass
class MemoryEntry:
    incident_id: str
    created_at: datetime
    summary: str
    features: dict[str, Any]
    root_cause_id: str
    root_cause: str
    action_taken: str
    action_parameters: dict[str, Any] = field(default_factory=dict)
    outcome: str = ""
    recovery_time_s: float | None = None
    revenue_protected_per_hour_paise: int = 0
    human_override: bool = False
    false_positive: bool = False

    def as_match(self, similarity: float) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "similarity": round(similarity, 3),
            "root_cause_id": self.root_cause_id,
            "root_cause": self.root_cause,
            "action_taken": self.action_taken,
            "outcome": self.outcome,
            "recovery_time_s": self.recovery_time_s,
            "revenue_protected_per_hour_paise": self.revenue_protected_per_hour_paise,
            "false_positive": self.false_positive,
            "human_override": self.human_override,
            "summary": self.summary,
            "matched_on": self.features,
        }


def extract_incident_features(incident: IncidentRecord) -> dict[str, Any]:
    """Reduce an incident to the handful of attributes that make two incidents 'the same kind'."""
    features: dict[str, Any] = {}
    evidence = incident.evidence
    anomaly = incident.anomaly

    if evidence:
        features["dominant_error_code"] = evidence.dominant_error_code
        features["latency_shift_ms"] = round(evidence.latency_shift_ms)
    if anomaly:
        features["deviation"] = round(anomaly.deviation, 3)
        # Flatten the affected segments into single-valued dimensions so matching is direct.
        for segment in anomaly.affected_segments[:4]:
            for key, value in segment.segment.dimensions.items():
                features.setdefault(key, value)
    return {k: v for k, v in features.items() if v is not None}


def similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Weighted agreement in [0, 1], normalised by the weight actually comparable.

    Normalising by comparable weight rather than total weight matters: an incident recorded before
    `amount_band` existed should not be penalised for a feature neither side has.
    """
    total_weight = 0.0
    score = 0.0

    for key, weight in FEATURE_WEIGHTS.items():
        left, right = a.get(key), b.get(key)
        if left is None or right is None:
            continue
        total_weight += weight
        if str(left) == str(right):
            score += weight

    for key, weight in NUMERIC_WEIGHTS.items():
        left, right = a.get(key), b.get(key)
        if left is None or right is None:
            continue
        total_weight += weight
        scale = max(abs(float(left)), abs(float(right)), 1e-6)
        closeness = 1.0 - min(1.0, abs(float(left) - float(right)) / scale)
        score += weight * closeness

    if total_weight <= 0:
        return 0.0
    return score / total_weight


class IncidentMemory:
    """In-process memory mirrored to SQLite so it survives across runs."""

    def __init__(self, persist: bool = True) -> None:
        self._entries: list[MemoryEntry] = []
        self.persist = persist

    # ----------------------------------------------------------------- read

    def similar(self, features: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
        query = {k: v for k, v in (features or {}).items() if v is not None}
        if not query:
            return []
        scored = []
        for entry in self._entries:
            score = similarity(query, entry.features)
            if score >= MIN_SIMILARITY:
                scored.append((score, entry))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [entry.as_match(score) for score, entry in scored[:limit]]

    def all(self) -> list[MemoryEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    # ---------------------------------------------------------------- write

    def record(self, incident: IncidentRecord) -> MemoryEntry:
        """Store a closed incident, including the ones that went badly.

        Failed interventions and false positives are recorded deliberately: an agent that only
        remembers its successes will keep repeating the mistake that produced them.
        """
        verification = incident.verification
        outcome = incident.outcome or (verification.status.value if verification else "UNKNOWN")
        recovery_time = None
        if incident.closed_at:
            recovery_time = (incident.closed_at - incident.opened_at).total_seconds()

        false_positive = (
            incident.root_cause is not None
            and incident.root_cause.cause_id == "traffic_mix_shift"
        ) or outcome == "FALSE_POSITIVE"

        entry = MemoryEntry(
            incident_id=incident.incident_id,
            created_at=incident.closed_at or utcnow(),
            summary=incident.title or (incident.root_cause.narrative if incident.root_cause else ""),
            features=extract_incident_features(incident),
            root_cause_id=incident.root_cause.cause_id if incident.root_cause else "",
            root_cause=incident.root_cause.most_likely_root_cause if incident.root_cause else "",
            action_taken=incident.action_result.action.value if incident.action_result else "none",
            action_parameters=incident.action_result.parameters if incident.action_result else {},
            outcome=outcome,
            recovery_time_s=recovery_time,
            revenue_protected_per_hour_paise=incident.revenue_protected_per_hour_paise,
            human_override=incident.state.value == "AWAITING_HUMAN_APPROVAL",
            false_positive=false_positive,
        )
        self._entries.append(entry)
        if self.persist:
            self._persist(entry)
        return entry

    def _persist(self, entry: MemoryEntry) -> None:
        try:
            with session_scope() as session:
                session.merge(
                    IncidentMemoryRow(
                        incident_id=entry.incident_id,
                        created_at=entry.created_at,
                        summary=entry.summary,
                        features=dumps(entry.features),
                        root_cause_id=entry.root_cause_id,
                        root_cause=entry.root_cause,
                        action_taken=entry.action_taken,
                        action_parameters=dumps(entry.action_parameters),
                        outcome=entry.outcome,
                        recovery_time_s=entry.recovery_time_s,
                        revenue_protected_per_hour_paise=entry.revenue_protected_per_hour_paise,
                        human_override=entry.human_override,
                        false_positive=entry.false_positive,
                    )
                )
        except Exception:
            # Memory is an optimisation, never a dependency. A database problem must not take down
            # incident response.
            self.persist = False

    def load(self) -> int:
        """Reload persisted memory, so priors survive a restart."""
        try:
            with session_scope() as session:
                rows = session.query(IncidentMemoryRow).all()
        except Exception:
            return 0
        self._entries = [
            MemoryEntry(
                incident_id=row.incident_id,
                created_at=row.created_at,
                summary=row.summary,
                features=json.loads(row.features or "{}"),
                root_cause_id=row.root_cause_id,
                root_cause=row.root_cause,
                action_taken=row.action_taken,
                action_parameters=json.loads(row.action_parameters or "{}"),
                outcome=row.outcome,
                recovery_time_s=row.recovery_time_s,
                revenue_protected_per_hour_paise=row.revenue_protected_per_hour_paise,
                human_override=row.human_override,
                false_positive=row.false_positive,
            )
            for row in rows
        ]
        return len(self._entries)


def outcome_from_verification(status: VerificationStatus | None) -> str:
    return status.value if status else "UNKNOWN"
