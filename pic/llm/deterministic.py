"""Deterministic reasoner.

Implements the same interface as the Gemini reasoner using rules only. It exists for three
reasons, all of them practical:

* **Reproducible evaluation.** Benchmark numbers must not move between runs. Harness runs pin this
  reasoner unless explicitly asked for the model.
* **No-credential operation.** The repository must clone, install and demo without an API key.
* **Graceful degradation.** When the model is unreachable mid-incident, the system still diagnoses
  and acts rather than stalling on a live payment outage.

It ranks by the deterministic prior the Root Cause Agent already computed, so its answers are
sane — the model's contribution is nuance and narrative, not basic competence.
"""

from __future__ import annotations

from typing import Any

from ..schemas import ActionType, EvidenceBundle, Hypothesis
from .base import ActionChoice, HypothesisRanking, Reasoner

# Two hypotheses closer than this are treated as indistinguishable rather than force-ranked.
AMBIGUITY_MARGIN = 0.12


class DeterministicReasoner(Reasoner):
    name = "deterministic"

    def rank_hypotheses(
        self,
        bundle: EvidenceBundle,
        candidates: list[Hypothesis],
        context: dict[str, Any],
    ) -> HypothesisRanking:
        ranked = sorted(candidates, key=lambda h: h.probability, reverse=True)
        ambiguous = False
        if len(ranked) >= 2:
            ambiguous = (ranked[0].probability - ranked[1].probability) < AMBIGUITY_MARGIN
        top = ranked[0] if ranked else None
        narrative = ""
        if top:
            evidence = "; ".join(top.supporting_evidence[:3]) or "no corroborating evidence"
            narrative = (
                f"{top.cause} is the most likely explanation at {top.probability:.0%} confidence, "
                f"supported by: {evidence}."
            )
            if ambiguous:
                narrative += (
                    f" This is close to '{ranked[1].cause}' ({ranked[1].probability:.0%}); "
                    "the evidence does not clearly separate them."
                )
        _ = bundle, context
        return HypothesisRanking(
            hypotheses=ranked, ambiguous=ambiguous, narrative=narrative, reasoner=self.name
        )

    def propose_action(
        self, context: dict[str, Any], catalogue: list[dict[str, Any]]
    ) -> ActionChoice:
        """Pick the highest-ranked pre-costed candidate.

        The Decision Agent has already computed expected value for every candidate action, so the
        deterministic choice is simply the best of them. This keeps the fallback path honest: it
        makes the same trade-off the model is asked to make, without the prose.
        """
        if not catalogue:
            return ActionChoice(
                action=ActionType.NO_ACTION,
                rationale="No candidate action available for this diagnosis.",
                reasoner=self.name,
            )
        best = max(catalogue, key=lambda c: c.get("expected_value_paise", 0))
        return ActionChoice(
            action=ActionType(best["action"]),
            parameters=dict(best.get("parameters", {})),
            rationale=best.get("rationale", ""),
            risk_score=float(best.get("risk_score", 0.3)),
            confidence=float(context.get("root_cause_confidence", 0.5)),
            alternatives=[c for c in catalogue if c is not best][:3],
            reasoner=self.name,
        )

    def summarize(self, context: dict[str, Any]) -> str:
        rate = context.get("current_success_rate")
        baseline = context.get("baseline_success_rate")
        cause = context.get("root_cause", "an unidentified cause")
        risk = context.get("revenue_at_risk_per_hour_paise", 0)
        parts = []
        if rate is not None and baseline is not None:
            parts.append(f"Payment success rate fell from {baseline:.1%} to {rate:.1%}.")
        parts.append(f"Most likely cause: {cause}.")
        if risk:
            parts.append(f"Estimated revenue at risk: INR {risk / 100:,.0f} per hour.")
        return " ".join(parts)


def summarise_evidence(bundle: EvidenceBundle, limit: int = 12) -> list[str]:
    """Compact evidence rendering shared by both reasoners' prompts and narratives."""
    return [f"[{f.finding_id}] {f.statement}" for f in bundle.findings[:limit]]
