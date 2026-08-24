"""Statistical primitives for detection and verification.

Deliberately small and dependency-light so every number the system reports can be traced to a
few lines of readable maths (ADR-001, ADR-009).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

NORMAL_SF_COEFF = 0.2316419


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def median(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def stdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def mad(xs: Sequence[float]) -> float:
    """Median absolute deviation, scaled to be a consistent estimator of sigma for normal data."""
    if not xs:
        return 0.0
    med = median(xs)
    return 1.4826 * median([abs(x - med) for x in xs])


def ewma(xs: Sequence[float], alpha: float) -> float:
    """Exponentially weighted mean — recent windows matter more than an hour-old baseline."""
    if not xs:
        return 0.0
    acc = xs[0]
    for x in xs[1:]:
        acc = alpha * x + (1 - alpha) * acc
    return acc


def ewma_series(xs: Sequence[float], alpha: float) -> list[float]:
    if not xs:
        return []
    out = [xs[0]]
    for x in xs[1:]:
        out.append(alpha * x + (1 - alpha) * out[-1])
    return out


def robust_z(value: float, history: Sequence[float]) -> float:
    """Z-score using median/MAD so a previous incident in the baseline cannot mask a new one.

    Falls back to the standard deviation when MAD collapses to zero (a perfectly flat series),
    and to a small floor after that, since a flat baseline would otherwise divide by zero and
    report infinite significance on a one-transaction wobble.
    """
    if len(history) < 2:
        return 0.0
    centre = median(history)
    scale = mad(history)
    if scale <= 1e-9:
        scale = stdev(history)
    if scale <= 1e-9:
        scale = 0.005  # ~0.5pp of success-rate noise; prevents divide-by-zero blowups
    return (value - centre) / scale


def normal_sf(z: float) -> float:
    """Upper-tail probability of the standard normal (Zelen & Severo approximation)."""
    if z < 0:
        return 1.0 - normal_sf(-z)
    t = 1.0 / (1.0 + NORMAL_SF_COEFF * z)
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    return max(0.0, min(1.0, poly * math.exp(-z * z / 2.0) / math.sqrt(2 * math.pi)))


@dataclass
class CusumResult:
    change_point_detected: bool
    statistic: float
    index: int | None


def cusum(series: Sequence[float], k: float = 0.5, h: float = 4.0) -> CusumResult:
    """One-sided lower CUSUM: detects a sustained downward shift in the mean.

    `k` is the slack in units of sigma (shifts smaller than this are ignored) and `h` the decision
    threshold. Standardising by MAD makes the test scale-free across merchants.
    """
    if len(series) < 4:
        return CusumResult(False, 0.0, None)
    centre = median(series)
    scale = mad(series) or stdev(series) or 0.005
    acc = 0.0
    worst = 0.0
    at: int | None = None
    for i, x in enumerate(series):
        standardised = (x - centre) / scale
        acc = min(0.0, acc + standardised + k)
        if abs(acc) > worst:
            worst = abs(acc)
            at = i
    return CusumResult(worst >= h, worst, at)


@dataclass
class ProportionTest:
    """Two-proportion z-test — 'did the intervention work' as a hypothesis test."""

    z: float
    p_value: float
    significant: bool
    diff: float


def two_proportion_ztest(
    successes_a: int, total_a: int, successes_b: int, total_b: int, alpha: float = 0.05
) -> ProportionTest:
    """One-sided test of H0: p_b <= p_a against H1: p_b > p_a (B is the post-action window)."""
    if total_a == 0 or total_b == 0:
        return ProportionTest(0.0, 1.0, False, 0.0)
    p_a = successes_a / total_a
    p_b = successes_b / total_b
    pooled = (successes_a + successes_b) / (total_a + total_b)
    se = math.sqrt(pooled * (1 - pooled) * (1 / total_a + 1 / total_b))
    if se <= 1e-12:
        return ProportionTest(0.0, 1.0, False, p_b - p_a)
    z = (p_b - p_a) / se
    p_value = normal_sf(z)
    return ProportionTest(z=z, p_value=p_value, significant=p_value < alpha, diff=p_b - p_a)


def confidence_from_evidence(
    z_score: float, sample_size: int, absolute_drop: float, change_point: bool
) -> float:
    """Map detection evidence onto a calibrated 0..1 confidence.

    Three independent things should raise confidence: how far outside normal variation the value
    sits, how much data supports it, and how large the effect is in business terms. A confirming
    change point adds a small bonus. Kept as an explicit readable formula rather than a fitted
    model so its calibration can be inspected and argued with.
    """
    z_component = min(1.0, abs(z_score) / 6.0)
    # Volume confidence saturates around a few hundred attempts.
    volume_component = min(1.0, math.log10(max(1, sample_size)) / math.log10(500))
    effect_component = min(1.0, absolute_drop / 0.20)
    score = 0.45 * z_component + 0.25 * volume_component + 0.30 * effect_component
    if change_point:
        score = min(1.0, score + 0.05)
    return round(max(0.0, min(0.99, score)), 3)
