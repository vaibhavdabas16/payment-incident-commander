"""Central configuration. Everything tunable lives here or in the policy YAML."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class DetectionConfig:
    """Thresholds for the deterministic detector (ADR-001)."""

    window_seconds: int = 120
    baseline_windows: int = 20
    min_baseline_windows: int = 6
    ewma_alpha: float = 0.25
    # A window must carry at least this many attempts to be judged at all.
    min_sample_size: int = 40
    # Robust z-score threshold on the success-rate series.
    z_threshold: float = 3.0
    # Absolute drop floor: guards against a hypersensitive z on a very stable series.
    min_absolute_drop: float = 0.04
    # Relative drop floor as a fraction of baseline.
    min_relative_drop: float = 0.05
    # CUSUM change-point sensitivity.
    cusum_k: float = 0.5
    cusum_h: float = 4.0
    # Confidence below this never opens an incident (fail-closed).
    min_confidence: float = 0.6
    # Severity banding on absolute deviation.
    severity_bands: tuple[float, float, float] = (0.05, 0.10, 0.18)
    # Segment attribution.
    min_segment_volume: int = 15
    # Joint slices need more volume before they are trustworthy.
    min_cross_segment_volume: int = 30
    min_segment_failure_share: float = 0.25
    max_segments_reported: int = 5
    # A segment must fall at least this fraction as far as the worst-hit one to be reported,
    # which filters correlated echoes (device=mobile when UPI degrades).
    echo_floor_ratio: float = 0.45

    # --- Segment-level detection: catches incidents the headline rate hides ---
    # Minimum volume in the current window before a segment can open an incident on its own.
    min_segment_detection_volume: int = 60
    # Minimum absolute drop against the segment's own baseline.
    min_segment_drop: float = 0.12
    # Two-proportion test threshold. Tighter than the usual 0.05 because many segments are tested
    # each cycle and we would otherwise open incidents on multiple-comparison artefacts.
    segment_significance_level: float = 0.001
    # A segment-only anomaly must put at least this much revenue per hour at risk to be promoted
    # to an incident (INR 50,000/hour).
    min_segment_revenue_at_risk_paise: int = 50_000 * 100
    # Second detection tier: a longer window buys statistical power for narrow segments
    # (one payment method in two states) at the cost of detection latency.
    slow_window_seconds: int = 600
    slow_baseline_seconds: int = 1800
    # The slow tier leans on the significance test rather than raw volume, so a small but badly
    # broken segment (high-value cards) is still catchable. p < 0.001 is what keeps this honest.
    min_slow_segment_volume: int = 30

    # --- Revenue-at-risk estimation over a disjoint (payment_method, amount_band) partition ---
    min_stratum_volume: int = 20
    # Lenient on purpose: a stratum only needs to be plausibly degraded to contribute value, and
    # the significance gate exists to stop one-sided noise accumulating across many strata.
    stratum_significance_level: float = 0.10

    # --- Severity banding on money, alongside the deviation bands above ---
    high_revenue_at_risk_paise: int = 5_00_000 * 100  # INR 5L/hour
    critical_revenue_at_risk_paise: int = 15_00_000 * 100  # INR 15L/hour


@dataclass
class SimulationConfig:
    seed: int = 20260824
    merchant_id: str = "merch_acme"
    # Baseline arrival rate, payments per minute.
    base_rate_per_min: int = 180
    baseline_success_rate: float = 0.92
    # Diurnal amplitude as a fraction of base rate.
    diurnal_amplitude: float = 0.25


@dataclass
class LLMConfig:
    provider: str = field(default_factory=lambda: os.getenv("PIC_LLM_PROVIDER", "auto"))
    api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    model: str = field(default_factory=lambda: os.getenv("PIC_LLM_MODEL", "gemini-2.5-flash"))
    timeout_s: float = 30.0
    max_retries: int = 2
    temperature: float = 0.1

    @property
    def enabled(self) -> bool:
        if self.provider == "deterministic":
            return False
        return bool(self.api_key)


@dataclass
class VerificationConfig:
    # How long to observe after an intervention before judging it.
    observation_seconds: int = 180
    min_post_sample: int = 40
    significance_level: float = 0.05
    # Absolute improvement required to call anything a recovery.
    min_meaningful_improvement: float = 0.03
    # Fraction of the gap back to baseline that counts as full recovery.
    full_recovery_ratio: float = 0.75
    partial_recovery_ratio: float = 0.35
    # Drop below the pre-action rate by this much and we roll back.
    regression_threshold: float = 0.02


@dataclass
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv("PIC_DATABASE_URL", f"sqlite:///{ROOT / 'pic.db'}")
    )
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    policy_file: Path = ROOT / "pic" / "policies" / "merchant_policies.yaml"
    results_dir: Path = ROOT / "evaluation" / "results"
    max_intervention_attempts: int = 2
    # Wall-clock seconds the simulator advances per real second in live mode.
    live_speedup: float = 60.0
    verbose: bool = field(default_factory=lambda: _env_bool("PIC_VERBOSE", False))


settings = Settings()
