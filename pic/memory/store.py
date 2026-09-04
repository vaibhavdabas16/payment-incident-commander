"""Incident memory — the system's record of what it has already tried.

Closed incidents are stored as `IncidentOutcomeRecord`, a typed structure covering the whole
lifecycle: the failure signature, the hypotheses, the option set, what policy granted, what was
executed at what magnitude, the treatment and control measurements, the money, whether it had to
be rolled back, and how it ended. Retrieval is deterministic weighted matching over structured
dimensions — no embeddings, no vector database (ADR-006). At this corpus size an exact agreement
on `(root_cause, error_code, psp)` is both a better match and an explainable one: the system can
say *"this resembles INC-0031: same PSP, same error code, and a 20% shift fixed it"* rather than
*"cosine similarity 0.83"*.

Three properties this file is responsible for:

* **Nothing here is written by a model.** Every field of every record is copied from a typed agent
  output or computed arithmetically. The reasoner may be shown these records; it can never author
  one, which is what makes "the agent must not invent historical incidents" structural rather than
  a hope (ADR-010).

* **Memory informs, it never decides.** Retrieved priors nudge hypothesis scores by at most ±0.15
  (`MAX_MEMORY_ADJUSTMENT` in the Root Cause Agent) and efficacy priors by at most
  ±`MAX_HISTORY_ADJUSTMENT` here. An agent that trusts its history over its instruments will
  confidently misdiagnose the first incident that looks familiar but isn't.

* **Merchants are isolated.** Every query filters by merchant. One merchant's outage must never
  become evidence about another merchant's routing, and the default is to scope rather than to
  remember to.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from typing import Any, Iterable

from ..database import IncidentMemoryRow, dumps, session_scope
from ..schemas import (
    ActionOutcomeStats,
    FailureSignature,
    IncidentOutcomeRecord,
    IncidentRecord,
    Severity,
    VerificationStatus,
    magnitude_band,
)

# --------------------------------------------------------------------------
# Similarity
# --------------------------------------------------------------------------

# Feature weights for the legacy flat-feature match used by the Root Cause Agent's priors. Error
# code and provider identity carry the most signal because they are the things that actually
# repeat across incidents; geography and device repeat by coincidence.
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

# Structured signature matching, used for recovery-strategy retrieval. Root cause is weighted
# highest because two incidents with the same symptoms and different causes call for opposite
# actions — which is exactly the mistake history is most likely to encourage.
SIGNATURE_WEIGHTS = {
    # Weighted highest, and by a clear margin. Two incidents with identical symptoms and different
    # causes call for opposite actions, and treating them as comparable is the specific mistake
    # history is most likely to encourage: "the last time UPI on psp_axis looked like this we
    # rerouted and it worked" is exactly the wrong lesson when this time nothing is broken. Set so
    # that disagreeing on both the cause and the error code puts a pair below the retrieval floor
    # on its own, while disagreeing on the cause alone still leaves related faults comparable -
    # a PSP outage and a provider-wide one are different diagnoses about the same kind of problem,
    # and the failure of a reroute in one is real evidence about a reroute in the other.
    "root_cause_id": 0.32,
    "dominant_error_code": 0.18,
    "psp": 0.14,
    "payment_method": 0.12,
    "issuer": 0.08,
    "gateway": 0.05,
    "route_id": 0.05,
    "geography": 0.02,
}

SIGNATURE_NUMERIC_WEIGHTS = {
    "degradation_magnitude": 0.06,
    "latency_shift_ms": 0.03,
    "traffic_share": 0.03,
}

SEVERITY_WEIGHT = 0.05
SEGMENT_OVERLAP_WEIGHT = 0.05
# Whether a healthy destination existed at all. Two PSP outages are not comparable evidence about
# rerouting if one of them had nowhere healthy to reroute to.
ROUTE_OPTION_WEIGHT = 0.04
HEALTHY_ROUTE_FLOOR = 0.80

# Retrieval floor for "this is the same kind of incident". Deliberately stricter than
# `MIN_SIMILARITY`: a loose match is acceptable as a hypothesis nudge and unacceptable as the
# reason to size an intervention.
MIN_SIGNATURE_SIMILARITY = 0.55

_SEVERITY_ORDER = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]


def _severity_closeness(a: Severity, b: Severity) -> float:
    try:
        gap = abs(_SEVERITY_ORDER.index(a) - _SEVERITY_ORDER.index(b))
    except ValueError:
        return 0.0
    return max(0.0, 1.0 - gap / 3.0)


def _numeric_closeness(left: float, right: float) -> float:
    scale = max(abs(float(left)), abs(float(right)), 1e-6)
    return 1.0 - min(1.0, abs(float(left) - float(right)) / scale)


def similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Weighted agreement in [0, 1] over flat features, normalised by comparable weight.

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
        score += weight * _numeric_closeness(float(left), float(right))

    if total_weight <= 0:
        return 0.0
    return score / total_weight


def signature_similarity(a: FailureSignature, b: FailureSignature) -> float:
    """Weighted agreement between two failure signatures, in [0, 1].

    Same normalisation rule as `similarity`: a dimension neither side recorded is skipped rather
    than counted as disagreement, so an older record is not penalised for a field that did not
    exist when it was written.
    """
    total_weight = 0.0
    score = 0.0

    for key, weight in SIGNATURE_WEIGHTS.items():
        left, right = getattr(a, key, None), getattr(b, key, None)
        if left in (None, "") or right in (None, ""):
            continue
        total_weight += weight
        if str(left) == str(right):
            score += weight

    for key, weight in SIGNATURE_NUMERIC_WEIGHTS.items():
        left, right = getattr(a, key, None), getattr(b, key, None)
        if left is None or right is None:
            continue
        total_weight += weight
        score += weight * _numeric_closeness(float(left), float(right))

    total_weight += SEVERITY_WEIGHT
    score += SEVERITY_WEIGHT * _severity_closeness(a.severity, b.severity)

    left_keys, right_keys = set(a.affected_segment_keys), set(b.affected_segment_keys)
    if left_keys and right_keys:
        total_weight += SEGMENT_OVERLAP_WEIGHT
        overlap = len(left_keys & right_keys) / len(left_keys | right_keys)
        score += SEGMENT_OVERLAP_WEIGHT * overlap

    if a.route_health and b.route_health:
        total_weight += ROUTE_OPTION_WEIGHT
        left_ok = max(a.route_health.values()) >= HEALTHY_ROUTE_FLOOR
        right_ok = max(b.route_health.values()) >= HEALTHY_ROUTE_FLOOR
        if left_ok == right_ok:
            score += ROUTE_OPTION_WEIGHT

    if total_weight <= 0:
        return 0.0
    return score / total_weight


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


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


def build_signature(
    incident: IncidentRecord, route_health: dict[str, float] | None = None
) -> FailureSignature:
    """Derive the structured failure signature from an incident's own recorded outputs.

    Every field is lifted from a typed agent output. Nothing is inferred from prose and nothing is
    supplied by a model, which is what lets retrieval be checked against the incident it came from.
    """
    features = extract_incident_features(incident)
    anomaly = incident.anomaly
    evidence = incident.evidence
    traffic_share = 0.0
    segment_keys: list[str] = []
    if anomaly:
        segment_keys = [s.segment.key() for s in anomaly.affected_segments]
        traffic_share = round(sum(s.traffic_share for s in anomaly.affected_segments[:3]), 4)

    return FailureSignature(
        merchant_id=incident.merchant_id,
        payment_method=features.get("payment_method"),
        psp=features.get("psp"),
        issuer=features.get("issuer"),
        gateway=features.get("gateway"),
        route_id=features.get("route_id"),
        geography=features.get("geography"),
        dominant_error_code=evidence.dominant_error_code if evidence else None,
        root_cause_id=incident.root_cause.cause_id if incident.root_cause else "",
        severity=incident.severity,
        degradation_magnitude=round(abs(anomaly.deviation), 4) if anomaly else 0.0,
        latency_shift_ms=round(evidence.latency_shift_ms, 1) if evidence else 0.0,
        traffic_share=traffic_share,
        affected_segment_keys=segment_keys[:6],
        route_health=dict(route_health or {}),
    )


def features_from_signature(signature: FailureSignature) -> dict[str, Any]:
    """The flat feature view of a signature, for the legacy `similar()` retrieval path."""
    out: dict[str, Any] = {
        "dominant_error_code": signature.dominant_error_code,
        "psp": signature.psp,
        "issuer": signature.issuer,
        "payment_method": signature.payment_method,
        "route_id": signature.route_id,
        "geography": signature.geography,
        "deviation": -signature.degradation_magnitude,
        "latency_shift_ms": signature.latency_shift_ms,
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SimilarIncident:
    """A retrieved record with the score that retrieved it."""

    similarity: float
    record: IncidentOutcomeRecord

    def as_dict(self) -> dict[str, Any]:
        r = self.record
        return {
            "incident_id": r.incident_id,
            "similarity": round(self.similarity, 3),
            "matched_on": r.failure_signature.label(),
            "root_cause_id": r.selected_root_cause_id,
            "root_cause": r.selected_root_cause,
            "action_taken": r.actual_action_executed,
            "magnitude": r.intervention_magnitude,
            "magnitude_band": r.magnitude_band(),
            "verification_result": r.verification_result,
            "outcome": r.final_resolution,
            "succeeded": r.succeeded(),
            "rollback_required": r.rollback_required,
            "revenue_protected_paise": r.revenue_protected_paise,
            "revenue_recovered_paise": r.revenue_recovered_paise,
            "revenue_lost_paise": r.revenue_lost_paise,
            "recovery_rate": r.recovery_rate,
            "time_to_recovery_s": r.time_to_recovery_s,
            "human_intervention_required": r.human_intervention_required,
            "false_positive": r.false_positive,
            "at": r.timestamp.isoformat(),
        }


def _legacy_match(record: IncidentOutcomeRecord, score: float) -> dict[str, Any]:
    """The shape the Investigation and Root Cause agents have always consumed."""
    return {
        "incident_id": record.incident_id,
        "similarity": round(score, 3),
        "root_cause_id": record.selected_root_cause_id,
        "root_cause": record.selected_root_cause,
        "action_taken": record.actual_action_executed,
        "outcome": record.final_resolution,
        "recovery_time_s": record.time_to_recovery_s,
        "revenue_protected_per_hour_paise": record.revenue_protected_paise,
        "false_positive": record.false_positive,
        "human_override": record.human_intervention_required,
        "summary": record.selected_root_cause,
        "matched_on": features_from_signature(record.failure_signature),
    }


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------

# Non-remedial or inert actions never carry useful efficacy statistics: they were never trying to
# move the success rate, so counting them as failed interventions would poison every prior.
NON_STATISTICAL_ACTIONS = {"none", "no_action", "create_incident_ticket", "set_monitoring_frequency"}


class IncidentMemory:
    """In-process memory of completed incidents, mirrored to SQLite so it survives a restart."""

    def __init__(self, persist: bool = True) -> None:
        self._records: list[IncidentOutcomeRecord] = []
        self._by_id: dict[str, IncidentOutcomeRecord] = {}
        self.persist = persist

    # ----------------------------------------------------------------- read

    def all(self) -> list[IncidentOutcomeRecord]:
        return list(self._records)

    def for_merchant(self, merchant_id: str | None) -> list[IncidentOutcomeRecord]:
        """Every record belonging to one merchant. The default scoping for every query."""
        if not merchant_id:
            return list(self._records)
        return [r for r in self._records if r.merchant_id == merchant_id]

    def get(self, incident_id: str) -> IncidentOutcomeRecord | None:
        return self._by_id.get(incident_id)

    def __len__(self) -> int:
        return len(self._records)

    def similar(self, features: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
        """Legacy flat-feature retrieval, used for root-cause priors.

        Kept alongside `find_similar_incidents` rather than folded into it: this one answers
        "what was wrong last time it looked like this", which needs a looser bar than "how big a
        traffic shift should I make", and conflating the two thresholds would either flood the
        priors with weak matches or starve the strategy layer of evidence.
        """
        query = {k: v for k, v in (features or {}).items() if v is not None}
        if not query:
            return []
        scored = []
        for record in self._records:
            score = similarity(query, features_from_signature(record.failure_signature))
            if score >= MIN_SIMILARITY:
                scored.append((score, record))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [_legacy_match(record, score) for score, record in scored[:limit]]

    def find_similar_incidents(
        self,
        signature: FailureSignature,
        *,
        merchant_id: str | None = None,
        limit: int = 8,
        min_similarity: float = MIN_SIGNATURE_SIMILARITY,
        exclude: str | None = None,
    ) -> list[SimilarIncident]:
        """Past incidents of the same kind, most similar first.

        Scoped to one merchant by default — `merchant_id` falls back to the signature's own, so
        cross-merchant leakage requires passing an explicit empty string rather than forgetting.
        """
        scope = merchant_id if merchant_id is not None else signature.merchant_id
        out: list[SimilarIncident] = []
        for record in self.for_merchant(scope):
            if exclude and record.incident_id == exclude:
                continue
            score = signature_similarity(signature, record.failure_signature)
            if score >= min_similarity:
                out.append(SimilarIncident(similarity=score, record=record))
        out.sort(key=lambda m: (m.similarity, m.record.timestamp), reverse=True)
        return out[:limit]

    def get_action_outcomes_for_condition(
        self,
        signature: FailureSignature,
        *,
        merchant_id: str | None = None,
        min_similarity: float = MIN_SIGNATURE_SIMILARITY,
        by_magnitude: bool = True,
    ) -> list[ActionOutcomeStats]:
        """How each action has performed under conditions like these, one row per action×band.

        This is the query that makes learning change behaviour: it is what distinguishes "a 20%
        shift worked twice" from "a 50% shift failed", which are the same action and opposite
        evidence.
        """
        matches = self.find_similar_incidents(
            signature, merchant_id=merchant_id, limit=200, min_similarity=min_similarity
        )
        return _aggregate_outcomes([m.record for m in matches], by_magnitude=by_magnitude)

    def get_success_rate_for_action(
        self,
        action: str,
        *,
        signature: FailureSignature | None = None,
        merchant_id: str | None = None,
        magnitude: float | None = None,
        min_similarity: float = MIN_SIGNATURE_SIMILARITY,
    ) -> ActionOutcomeStats:
        """Observed success rate for one action, optionally at one magnitude, under a condition.

        Returns an empty `ActionOutcomeStats` (attempts=0) rather than a made-up rate when there
        is no history: a caller must be able to tell "never tried" from "tried and failed".
        """
        if signature is not None:
            records = [
                m.record
                for m in self.find_similar_incidents(
                    signature, merchant_id=merchant_id, limit=200, min_similarity=min_similarity
                )
            ]
        else:
            records = self.for_merchant(merchant_id)

        band = magnitude_band(magnitude) if magnitude is not None else None
        rows = _aggregate_outcomes(records, by_magnitude=band is not None)
        for row in rows:
            if row.action != action:
                continue
            if band is None or row.magnitude_band == band:
                return row
        return ActionOutcomeStats(action=action, magnitude_band=band or "any")

    def get_historical_recovery_outcomes(
        self,
        *,
        merchant_id: str | None = None,
        signature: FailureSignature | None = None,
        min_similarity: float = MIN_SIGNATURE_SIMILARITY,
    ) -> dict[str, Any]:
        """Aggregate financial and reliability outcomes over remembered incidents."""
        if signature is not None:
            records = [
                m.record
                for m in self.find_similar_incidents(
                    signature, merchant_id=merchant_id, limit=500, min_similarity=min_similarity
                )
            ]
        else:
            records = self.for_merchant(merchant_id)

        if not records:
            return {
                "incidents": 0,
                "revenue_at_risk_paise": 0,
                "revenue_protected_paise": 0,
                "revenue_recovered_paise": 0,
                "revenue_lost_paise": 0,
                "recovery_rate": 0.0,
                "actions": [],
            }

        at_risk = sum(r.revenue_at_risk_paise for r in records)
        protected = sum(r.revenue_protected_paise for r in records)
        recovered = sum(r.revenue_recovered_paise for r in records)
        lost = sum(r.revenue_lost_paise for r in records)
        durations = [r.time_to_recovery_s for r in records if r.time_to_recovery_s is not None]
        return {
            "incidents": len(records),
            "revenue_at_risk_paise": at_risk,
            "revenue_protected_paise": protected,
            "revenue_recovered_paise": recovered,
            "revenue_lost_paise": lost,
            # Recomputed from the totals rather than averaged over per-incident rates, so a tiny
            # incident with a perfect rate cannot outweigh a large one that went badly.
            "recovery_rate": round(min(1.0, (protected + recovered) / at_risk), 4)
            if at_risk > 0
            else 0.0,
            "median_time_to_recovery_s": round(statistics.median(durations), 1)
            if durations
            else None,
            "rollbacks": sum(1 for r in records if r.rollback_required),
            "human_interventions": sum(1 for r in records if r.human_intervention_required),
            "actions": [s.model_dump(mode="json") for s in _aggregate_outcomes(records)],
        }

    # ---------------------------------------------------------------- write

    def record_incident(
        self, record: IncidentOutcomeRecord | IncidentRecord
    ) -> IncidentOutcomeRecord:
        """Store a completed incident, including the ones that went badly.

        Failed interventions, rollbacks and false positives are recorded deliberately: an agent
        that only remembers its successes will keep repeating the mistake that produced them.
        Accepts a live `IncidentRecord` for convenience, but the supervisor builds the structured
        record itself so learning is computed once, in one place.
        """
        if isinstance(record, IncidentRecord):
            from .build import build_outcome_record

            record = build_outcome_record(record)

        existing = self._by_id.get(record.incident_id)
        if existing is not None:
            self._records[self._records.index(existing)] = record
        else:
            self._records.append(record)
        self._by_id[record.incident_id] = record
        if self.persist:
            self._persist(record)
        return record

    # Retained so existing callers (and the supervisor's older path) keep working unchanged.
    record = record_incident

    def update_incident(self, incident_id: str, **fields: Any) -> IncidentOutcomeRecord | None:
        """Amend a stored record — a rollback confirmed late, revenue recomputed at close.

        Returns None for an unknown id rather than inventing a record to hold the update.
        """
        current = self._by_id.get(incident_id)
        if current is None:
            return None
        updated = current.model_copy(update=fields)
        self._records[self._records.index(current)] = updated
        self._by_id[incident_id] = updated
        if self.persist:
            self._persist(updated)
        return updated

    def clear(self) -> None:
        """Forget everything. Used when a demo world is reset to a clean baseline."""
        self._records.clear()
        self._by_id.clear()

    def _persist(self, record: IncidentOutcomeRecord) -> None:
        try:
            with session_scope() as session:
                session.merge(
                    IncidentMemoryRow(
                        incident_id=record.incident_id,
                        created_at=record.timestamp,
                        merchant_id=record.merchant_id,
                        signature_key=record.failure_signature.key(),
                        root_cause_id=record.selected_root_cause_id,
                        action_taken=record.actual_action_executed,
                        outcome=record.final_resolution,
                        verification_result=record.verification_result,
                        revenue_protected_paise=record.revenue_protected_paise,
                        revenue_recovered_paise=record.revenue_recovered_paise,
                        revenue_lost_paise=record.revenue_lost_paise,
                        rollback_required=record.rollback_required,
                        false_positive=record.false_positive,
                        record=dumps(record.model_dump(mode="json")),
                    )
                )
        except Exception:
            # Memory is an optimisation, never a dependency. A database problem must not take down
            # incident response.
            self.persist = False

    def load(self) -> int:
        """Reload persisted memory, so what the system learned survives a restart."""
        try:
            with session_scope() as session:
                rows = session.query(IncidentMemoryRow).all()
        except Exception:
            return 0
        loaded: list[IncidentOutcomeRecord] = []
        for row in rows:
            try:
                loaded.append(IncidentOutcomeRecord.model_validate(json.loads(row.record or "{}")))
            except Exception:
                # One unreadable row must not discard the rest of what was learned.
                continue
        self._records = loaded
        self._by_id = {r.incident_id: r for r in loaded}
        return len(self._records)


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def _aggregate_outcomes(
    records: Iterable[IncidentOutcomeRecord], by_magnitude: bool = True
) -> list[ActionOutcomeStats]:
    """Group outcomes by executed action (and magnitude band), counting what actually happened."""
    buckets: dict[tuple[str, str], list[IncidentOutcomeRecord]] = {}
    for record in records:
        action = record.actual_action_executed or "none"
        if action in NON_STATISTICAL_ACTIONS:
            continue
        band = record.magnitude_band() if by_magnitude else "any"
        buckets.setdefault((action, band), []).append(record)

    out: list[ActionOutcomeStats] = []
    for (action, band), rows in buckets.items():
        successes = sum(1 for r in rows if r.succeeded())
        helped = sum(1 for r in rows if r.helped())
        partials = helped - successes
        rollbacks = sum(1 for r in rows if r.rollback_required)
        failures = len(rows) - helped
        times = [r.time_to_recovery_s for r in rows if r.time_to_recovery_s is not None]
        protected = [r.revenue_protected_paise for r in rows if r.revenue_protected_paise > 0]
        _ = partials
        out.append(
            ActionOutcomeStats(
                action=action,
                magnitude_band=band,
                attempts=len(rows),
                successes=successes,
                partials=partials,
                failures=max(0, failures),
                rollbacks=rollbacks,
                helped=helped,
                success_rate=round(successes / len(rows), 4) if rows else 0.0,
                helped_rate=round(helped / len(rows), 4) if rows else 0.0,
                median_recovery_time_s=round(statistics.median(times), 1) if times else None,
                median_revenue_protected_paise=int(statistics.median(protected))
                if protected
                else 0,
                incident_ids=[r.incident_id for r in rows][:20],
            )
        )
    out.sort(key=lambda s: (s.attempts, s.success_rate), reverse=True)
    return out


def outcome_from_verification(status: VerificationStatus | None) -> str:
    return status.value if status else "UNKNOWN"
