"""Gemini-backed reasoner.

Talks to the Generative Language REST API directly with `httpx` rather than through a vendor SDK.
The call surface we need is one endpoint, and a direct call means no dependency on SDK release
churn, explicit control over the structured-output schema, and an error path we can actually reason
about when a demo is running.

Every response is constrained by a `responseSchema` and then re-validated in Python. Two things are
enforced after the model replies, because a schema alone cannot express them:

* **No invented evidence.** Cited evidence IDs are intersected with the IDs actually present in the
  bundle. Anything the model made up is dropped, and a hypothesis left with no real support is
  reported as unsupported rather than quietly kept.
* **No invented actions.** The chosen action must be a member of the catalogue it was given.
  Anything else raises and falls back to deterministic reasoning.

Any failure — network, quota, malformed JSON, schema violation — raises `ReasonerUnavailable`, and
the calling agent proceeds deterministically with the incident flagged.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from ..config import settings
from ..schemas import ActionType, EvidenceBundle, Hypothesis
from .base import ActionChoice, HypothesisRanking, Reasoner, ReasonerUnavailable
from .deterministic import AMBIGUITY_MARGIN, DeterministicReasoner, summarise_evidence

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

SYSTEM_INSTRUCTION = (
    "You are the reasoning core of an autonomous payment-operations system for an Indian "
    "payments merchant. You analyse structured evidence about payment failures.\n\n"
    "Rules you must follow:\n"
    "1. Only cite evidence IDs that appear in the evidence list you are given. Never invent an "
    "observation, a metric, or an evidence ID.\n"
    "2. Distinguish confidence from certainty. If the evidence does not separate two explanations, "
    "say so and mark the diagnosis ambiguous rather than picking one to look decisive.\n"
    "3. Actively look for evidence that contradicts your leading hypothesis and report it.\n"
    "4. A falling success rate caused by a change in traffic mix is not a fault. If more customers "
    "simply moved to a payment method that always converts worse, the correct answer is that "
    "nothing is broken.\n"
    "5. You never execute anything. You propose; a separate deterministic policy engine decides.\n"
    "6. Be concise and specific. Operators read this during an outage."
)

_HYPOTHESIS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "hypotheses": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "cause_id": {"type": "STRING"},
                    "probability": {"type": "NUMBER"},
                    "supporting_evidence": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "contradicting_evidence": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "reasoning": {"type": "STRING"},
                },
                "required": ["cause_id", "probability", "reasoning"],
            },
        },
        "ambiguous": {"type": "BOOLEAN"},
        "narrative": {"type": "STRING"},
    },
    "required": ["hypotheses", "ambiguous", "narrative"],
}

_ACTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "action": {"type": "STRING"},
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "from_route": {"type": "STRING"},
                "to_route": {"type": "STRING"},
                "percentage": {"type": "NUMBER"},
                "payment_method": {"type": "STRING"},
                "max_retries": {"type": "INTEGER"},
                "change_id": {"type": "STRING"},
                "interval_seconds": {"type": "INTEGER"},
                "subject": {"type": "STRING"},
                "body": {"type": "STRING"},
                "title": {"type": "STRING"},
                "description": {"type": "STRING"},
            },
        },
        "rationale": {"type": "STRING"},
        "risk_score": {"type": "NUMBER"},
        "confidence": {"type": "NUMBER"},
        "rejected_alternatives": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"action": {"type": "STRING"}, "why_not": {"type": "STRING"}},
                "required": ["action", "why_not"],
            },
        },
    },
    "required": ["action", "rationale", "risk_score", "confidence"],
}


class GeminiReasoner(Reasoner):
    name = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or settings.llm.model
        self.api_key = api_key or settings.llm.api_key
        if not self.api_key:
            raise ReasonerUnavailable("GEMINI_API_KEY is not set")
        self._fallback = DeterministicReasoner()
        self.last_usage: dict[str, Any] = {}

    # ------------------------------------------------------------- transport

    def _generate(self, prompt: str, schema: dict[str, Any] | None) -> str:
        cfg = settings.llm
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": cfg.temperature},
        }
        if schema is not None:
            body["generationConfig"]["responseMimeType"] = "application/json"
            body["generationConfig"]["responseSchema"] = schema

        url = f"{API_ROOT}/{self.model}:generateContent"
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

        last_error: Exception | None = None
        for attempt in range(cfg.max_retries + 1):
            try:
                with httpx.Client(timeout=cfg.timeout_s) as client:
                    response = client.post(url, json=body, headers=headers)
                if response.status_code == 200:
                    payload = response.json()
                    self.last_usage = payload.get("usageMetadata", {})
                    return _extract_text(payload)
                # 429 and 5xx are worth retrying; a 400 means the request itself is wrong.
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = ReasonerUnavailable(
                        f"gemini HTTP {response.status_code}: {response.text[:200]}"
                    )
                    time.sleep(0.6 * (2**attempt))
                    continue
                raise ReasonerUnavailable(
                    f"gemini HTTP {response.status_code}: {response.text[:300]}"
                )
            except httpx.HTTPError as exc:
                last_error = exc
                time.sleep(0.6 * (2**attempt))

        raise ReasonerUnavailable(f"gemini unreachable after retries: {last_error}")

    def _generate_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        raw = self._generate(prompt, schema)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReasonerUnavailable(f"gemini returned non-JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ReasonerUnavailable("gemini returned a non-object JSON payload")
        return parsed

    # -------------------------------------------------------------- reasoning

    def rank_hypotheses(
        self,
        bundle: EvidenceBundle,
        candidates: list[Hypothesis],
        context: dict[str, Any],
    ) -> HypothesisRanking:
        if not candidates:
            return self._fallback.rank_hypotheses(bundle, candidates, context)

        evidence_lines = summarise_evidence(bundle, limit=20)
        candidate_lines = [
            f"- {h.cause_id}: {h.cause} (prior from statistical scoring: {h.probability:.2f})"
            for h in candidates
        ]
        prompt = "\n".join(
            [
                "A payment degradation has been detected. Statistical analysis has already scored "
                "candidate root causes; your job is to weigh the evidence and re-rank them.",
                "",
                f"Overall success rate: {context.get('current_success_rate', 0):.1%} "
                f"(baseline {context.get('baseline_success_rate', 0):.1%})",
                f"Dominant failure code: {bundle.dominant_error_code or 'none'} "
                f"({bundle.dominant_error_share:.0%} of failures)",
                f"p95 latency shift: {bundle.latency_shift_ms:+.0f} ms",
                "",
                "EVIDENCE (cite only these IDs):",
                *evidence_lines,
                "",
                "CANDIDATE ROOT CAUSES (use exactly these cause_id values):",
                *candidate_lines,
                "",
                "Return probabilities that sum to approximately 1.0 across the candidates. Set "
                "`ambiguous` to true if the top two are too close for the evidence to separate.",
            ]
        )

        data = self._generate_json(prompt, _HYPOTHESIS_SCHEMA)
        by_id = {h.cause_id: h for h in candidates}
        valid_evidence = bundle.finding_ids()

        ranked: list[Hypothesis] = []
        for item in data.get("hypotheses", []):
            cause_id = item.get("cause_id")
            base = by_id.get(cause_id)
            if base is None:
                continue  # a cause outside the catalogue is discarded, not trusted
            supporting = [e for e in item.get("supporting_evidence", []) if e in valid_evidence]
            contradicting = [
                e for e in item.get("contradicting_evidence", []) if e in valid_evidence
            ]
            ranked.append(
                base.model_copy(
                    update={
                        "probability": _clamp01(float(item.get("probability", base.probability))),
                        "supporting_evidence": supporting or base.supporting_evidence,
                        "contradicting_evidence": contradicting or base.contradicting_evidence,
                        "reasoning": str(item.get("reasoning", ""))[:600],
                    }
                )
            )

        if not ranked:
            raise ReasonerUnavailable("gemini returned no recognisable hypotheses")

        for h in by_id.values():
            if all(r.cause_id != h.cause_id for r in ranked):
                ranked.append(h)

        ranked.sort(key=lambda h: h.probability, reverse=True)
        ambiguous = bool(data.get("ambiguous", False))
        if len(ranked) >= 2 and (ranked[0].probability - ranked[1].probability) < AMBIGUITY_MARGIN:
            # The model may be more decisive than the numbers support; the margin rule still binds.
            ambiguous = True

        return HypothesisRanking(
            hypotheses=ranked,
            ambiguous=ambiguous,
            narrative=str(data.get("narrative", ""))[:1200],
            reasoner=self.name,
        )

    def propose_action(
        self, context: dict[str, Any], catalogue: list[dict[str, Any]]
    ) -> ActionChoice:
        if not catalogue:
            return self._fallback.propose_action(context, catalogue)

        allowed = {c["action"] for c in catalogue}
        lines = []
        for c in catalogue:
            lines.append(
                f"- {c['action']} {json.dumps(c.get('parameters', {}))}: "
                f"expected revenue protected INR {c.get('expected_revenue_protected_per_hour_paise', 0) / 100:,.0f}/hr, "
                f"expected value INR {c.get('expected_value_paise', 0) / 100:,.0f}, "
                f"risk {c.get('risk_score', 0):.2f}, reversible={c.get('reversible', True)}. "
                f"{c.get('rationale', '')}"
            )

        prompt = "\n".join(
            [
                "Choose the single best intervention for this payment incident.",
                "",
                f"Diagnosis: {context.get('root_cause')} "
                f"(confidence {context.get('root_cause_confidence', 0):.2f})",
                f"Revenue at risk: INR {context.get('revenue_at_risk_per_hour_paise', 0) / 100:,.0f}/hour",
                f"Success rate: {context.get('current_success_rate', 0):.1%} "
                f"(baseline {context.get('baseline_success_rate', 0):.1%})",
                f"Affected segments: {context.get('affected_segments')}",
                f"Route health: {context.get('route_health')}",
                "",
                "CANDIDATE ACTIONS (choose exactly one `action` value from this list):",
                *lines,
                "",
                "Optimise expected revenue protected minus cost and risk - not success rate alone. "
                "Prefer a reversible action. Choose `no_action` if no intervention is justified, "
                "for example when the cause is a traffic-mix change rather than a fault, or when "
                "the only fix is on the issuer's side. Explain briefly why you rejected the others.",
            ]
        )

        data = self._generate_json(prompt, _ACTION_SCHEMA)
        action_raw = str(data.get("action", "")).strip()
        if action_raw not in allowed:
            raise ReasonerUnavailable(
                f"gemini chose {action_raw!r}, which is not in the candidate catalogue"
            )

        chosen = next(c for c in catalogue if c["action"] == action_raw)
        # Parameters come from the deterministic candidate; the model may only adjust a shift size,
        # and even that is re-checked by the policy gateway.
        parameters = dict(chosen.get("parameters", {}))
        model_params = data.get("parameters") or {}
        if action_raw == ActionType.SHIFT_TRAFFIC.value and "percentage" in model_params:
            try:
                parameters["percentage"] = float(model_params["percentage"])
            except (TypeError, ValueError):
                pass

        return ActionChoice(
            action=ActionType(action_raw),
            parameters=parameters,
            rationale=str(data.get("rationale", ""))[:800],
            risk_score=_clamp01(float(data.get("risk_score", chosen.get("risk_score", 0.3)))),
            confidence=_clamp01(float(data.get("confidence", 0.5))),
            alternatives=list(data.get("rejected_alternatives", []))[:4],
            reasoner=self.name,
        )

    def summarize(self, context: dict[str, Any]) -> str:
        prompt = "\n".join(
            [
                "Write two sentences for an operations dashboard describing this payment incident "
                "and what was done about it. Plain, factual, no marketing tone.",
                "",
                json.dumps(context, default=str)[:4000],
            ]
        )
        try:
            return self._generate(prompt, None).strip()[:600]
        except ReasonerUnavailable:
            return self._fallback.summarize(context)


def _extract_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        feedback = payload.get("promptFeedback", {})
        raise ReasonerUnavailable(f"gemini returned no candidates (feedback: {feedback})")
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if not text.strip():
        reason = candidates[0].get("finishReason", "unknown")
        raise ReasonerUnavailable(f"gemini returned empty text (finishReason={reason})")
    return text


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
