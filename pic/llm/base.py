"""Reasoner interface.

Three narrow methods, deliberately. The LLM is used where judgement under ambiguity is genuinely
required and nowhere else (ADR-001): it ranks hypotheses that deterministic code has already scored
and costed, chooses an action from a closed catalogue, and writes the operator-facing narrative.

It never computes money, never authorises anything, and never invents an observation — every
hypothesis and action it returns is validated against the evidence and the catalogue before the
supervisor will accept it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..schemas import ActionType, EvidenceBundle, Hypothesis


class ReasonerUnavailable(RuntimeError):
    """Raised when a model-backed reasoner cannot produce a valid answer.

    Always caught by the calling agent, which falls back to deterministic reasoning and flags the
    incident `llm_unavailable`. A model outage degrades explanation quality; it never stops the
    system from responding to a live incident.
    """


@dataclass
class HypothesisRanking:
    hypotheses: list[Hypothesis]
    ambiguous: bool = False
    narrative: str = ""
    reasoner: str = "deterministic"


@dataclass
class ActionChoice:
    action: ActionType
    parameters: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    risk_score: float = 0.3
    confidence: float = 0.5
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    reasoner: str = "deterministic"


class Reasoner(ABC):
    name: str = "base"

    @abstractmethod
    def rank_hypotheses(
        self,
        bundle: EvidenceBundle,
        candidates: list[Hypothesis],
        context: dict[str, Any],
    ) -> HypothesisRanking:
        """Re-rank pre-scored hypotheses using the evidence, and say when it cannot separate them."""

    @abstractmethod
    def propose_action(
        self,
        context: dict[str, Any],
        catalogue: list[dict[str, Any]],
    ) -> ActionChoice:
        """Choose one action from `catalogue`, with parameters."""

    @abstractmethod
    def summarize(self, context: dict[str, Any]) -> str:
        """One short operator-facing paragraph explaining the incident."""


def build_reasoner(provider: str | None = None) -> Reasoner:
    """Select a reasoner.

    `auto` (the default) uses Gemini when a key is configured and falls back to deterministic
    reasoning otherwise, so the repository runs and evaluates with no credentials at all.
    """
    from ..config import settings
    from .deterministic import DeterministicReasoner

    provider = provider or settings.llm.provider

    if provider == "deterministic":
        return DeterministicReasoner()

    if provider in ("auto", "gemini"):
        if not settings.llm.api_key:
            if provider == "gemini":
                raise ReasonerUnavailable("GEMINI_API_KEY is not set")
            return DeterministicReasoner()
        from .gemini import GeminiReasoner

        return GeminiReasoner()

    raise ValueError(f"unknown LLM provider {provider!r}")
