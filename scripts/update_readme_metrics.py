"""Regenerate the README benchmark table from `evaluation/results/latest.json`.

    python scripts/update_readme_metrics.py

The README's numbers are written by this script and never by hand, so they cannot drift from what
the harness actually produced. The block between the two markers is replaced wholesale; edit the
harness, not the README.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
RESULTS = ROOT / "evaluation" / "results" / "latest.json"

START = "<!-- BENCHMARK:START -->"
END = "<!-- BENCHMARK:END -->"


def inr(paise: float | int | None) -> str:
    if not paise:
        return "—"
    rupees = paise / 100
    if abs(rupees) >= 1e7:
        return f"₹{rupees / 1e7:.2f} Cr"
    if abs(rupees) >= 1e5:
        return f"₹{rupees / 1e5:.1f}L"
    return f"₹{rupees:,.0f}"


def pct(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def num(value, suffix: str = "") -> str:
    return "—" if value is None else f"{value}{suffix}"


def build(report: dict) -> str:
    d = report["detection"]
    g = report["diagnosis"]
    b = report["business"]
    r = report["reliability"]
    e = report["end_to_end"]

    healthy = d["false_positives"] + d["true_negatives"]
    lines = [
        START,
        "",
        f"Produced by `python -m pic.evaluation.harness` on the **{report['reasoner']}** reasoner, "
        f"seeds `{', '.join(str(s) for s in report['seeds'])}`, "
        f"{len(report['runs'])} scenario runs, {d['windows_evaluated']} detection windows.",
        "",
        "| Detection | |",
        "|---|---|",
        f"| Precision | **{d['precision']:.3f}** |",
        f"| Recall | {d['recall']:.3f} |",
        f"| F1 | {d['f1']:.3f} |",
        f"| False positives | **{d['false_positives']} of {healthy}** healthy windows |",
        f"| Scenarios detected | {d['scenarios_detected']}/{d['scenarios_total']} |",
        f"| Median detection latency | {num(d['median_detection_latency_s'], 's')} |",
        "",
        "| Diagnosis | |",
        "|---|---|",
        f"| Top-1 root-cause accuracy | {num(g['top1_accuracy'])} |",
        f"| Evidence grounding | **{num(g['evidence_grounding_rate'])}** |",
        f"| Brier score (calibration, lower is better) | {num(g['brier_score'])} |",
        f"| Flagged ambiguous | {num(g['ambiguous_rate'])} |",
        "",
        "| Safety | |",
        "|---|---|",
        f"| Policy violations | **{r['policy_violations']}** |",
        f"| Unauthorised executions | **{r['unauthorised_executions']}** |",
        f"| Tool-call success rate | {num(r['tool_success_rate'])} |",
        f"| Appropriate action rate | {num(r['appropriate_action_rate'])} |",
        f"| Rollback success rate | {num(r['rollback_success_rate'])} "
        f"({r['rollbacks_attempted']} attempted) |",
        f"| Escalation rate (unnecessary) | {num(r['escalation_rate'])} "
        f"({num(r['unnecessary_escalation_rate'])}) |",
        "",
        "| Business impact | |",
        "|---|---|",
        f"| Median absolute revenue-estimate error | {num(b['median_abs_revenue_error_pct'], '%')} |",
        f"| Estimates within 25% / 50% | {num(b['within_25pct'])} / {num(b['within_50pct'])} |",
        f"| Median time to mitigate (from onset) | {num(b['median_time_to_mitigate_s'], 's')} |",
        "",
        "**Versus a human baseline** — a parameterised model of an on-call payments engineer, not a "
        "measurement. Its assumptions are stated in [docs/EVALUATION.md](docs/EVALUATION.md) and are "
        "deliberately generous to the human.",
        "",
        "| | This system | Human model | |",
        "|---|---|---|---|",
        f"| Detection | {num(e['ai_median_detection_s'], 's')} | "
        f"{num(e['baseline_detection_s'], 's')} | {num(e['detection_speedup_x'], '×')} |",
        f"| Mitigation | {num(e['ai_median_mitigation_s'], 's')} | "
        f"{num(e['baseline_mitigation_s'], 's')} | {num(e['mitigation_speedup_x'], '×')} |",
        "",
        END,
    ]
    return "\n".join(lines)


def main() -> int:
    if not RESULTS.exists():
        print(f"No results at {RESULTS}. Run: python -m pic.evaluation.harness", file=sys.stderr)
        return 1
    report = json.loads(RESULTS.read_text(encoding="utf-8"))
    readme = README.read_text(encoding="utf-8")

    if START not in readme or END not in readme:
        print("README is missing the BENCHMARK markers.", file=sys.stderr)
        return 1

    head, _, rest = readme.partition(START)
    _, _, tail = rest.partition(END)
    README.write_text(head + build(report) + tail, encoding="utf-8")
    print(f"README benchmark table updated from {RESULTS.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
