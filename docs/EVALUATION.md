# Evaluation methodology

Everything in this document is reproducible:

```bash
python -m pic.evaluation.harness            # deterministic reasoner, seeds 7 / 20260824 / 991
python -m pic.evaluation.harness --reasoner gemini
```

Results are written to `evaluation/results/latest.json` and rendered by the dashboard. **No number
in this repository is written by hand.** If a figure appears in the README, it came from that JSON.

---

## 1. What makes these numbers trustworthy

Three properties, each of which took deliberate engineering rather than good intentions.

### Ground truth comes from the generative process

The simulator degrades payments by multiplying a *known* nominal success probability. So for every
event it knows both `p_nominal` and `p_effective`, and the true expected revenue loss is exactly

```
Σ  amount_paise × (p_nominal − p_effective)
```

accumulated into per-minute buckets. The agent's estimate is scored against that, not against a
second estimate.

Per-minute bucketing matters. Degradations ramp in and out, so a single total divided by elapsed
time understates the loss rate at full severity. The harness compares the agent's figure against the
true loss over **the same window the agent observed**.

### The agent cannot see the answer key

Scenario labels live in a `ground_truth` table that no tool touches, and
`tests/test_safety_invariants.py::test_no_tool_can_reach_ground_truth` scans the tool modules to
enforce it. The `ToolContext` deliberately does not hold a reference to the simulator, because a
tool that could reach the simulator could read the active scenario.

Without this isolation, diagnosis accuracy would be theatre.

### Detection is scored per window, not per incident

Precision and recall need a denominator. The harness sweeps the detector across every monitoring
window of every run and labels each one by whether a scenario was genuinely active:

| | scenario active | no fault |
|---|---|---|
| **alarm raised** | true positive | **false positive** |
| **no alarm** | false negative | true negative |

Two details:

- The sweep runs **separately from the agent pipeline** and does **not** stop at the first alarm.
  An earlier version broke out on detection, leaving every later degraded window unsampled and
  reporting a recall of 0.078 for a detector that in fact fires on most of them.
- A window counts as "truly degraded" only once the injected scenario reaches **half intensity**.
  Below that the degradation is genuinely hard to see, and counting those windows as misses would
  penalise the detector for the ramp rather than for its sensitivity.

Clean runs (`CLEAN_MINUTES` of healthy traffic per seed) contribute only true negatives and false
positives, so the false-positive rate is measured on traffic where the correct answer is always
"do nothing".

---

## 2. What is measured

### Detection
`precision`, `recall`, `f1`, `false_positive_rate`, `scenarios_detected`, and detection latency
(seconds from scenario onset to the incident opening).

### Diagnosis
- `top1_accuracy` — the diagnosed `cause_id` against the scenario's true `root_cause_id`.
- `evidence_grounding_rate` — the fraction of runs in which **every** cited evidence ID exists in
  the bundle the tools actually produced. This is the measurable form of "never invent evidence".
- `brier_score` — calibration of the top-1 confidence. Lower is better; 0.25 is what you get by
  always saying 50%.
- `ambiguous_rate` — how often the system declines to separate competing hypotheses.

### Business impact
`median_abs_revenue_error_pct` against simulator ground truth, plus the share of estimates within
25% and 50%. Also time to mitigate, measured **from scenario onset**, not from detection — the
merchant loses money from the moment the degradation starts, and measuring from detection would
hide a slow detector.

### Agent reliability
`tool_success_rate`, `appropriate_action_rate`, `escalation_rate`, `rollback_success_rate`, and two
that must always read zero:

- `policy_violations` — an executed action whose parameters exceeded what the gateway granted.
- `unauthorised_executions` — an execution with no approving policy decision behind it.

A non-zero value in either is a failed build, not a tuning opportunity.

### Agent time is charged to the clock
Agent steps advance simulated time by their real wall-clock latency. Without this the whole pipeline
appears to complete instantaneously and time-to-mitigate reads 0.0s — flattering, and false. Real
reasoning costs real seconds (noticeably more with a model in the loop) and the merchant is losing
revenue throughout.

---

## 3. The human baseline is a model, not a measurement

The end-to-end comparison uses `ManualBaseline`, a parameterised model of a competent on-call
payments engineer:

| Phase | Seconds | Assumption |
|---|---|---|
| Detection | 540 | A threshold alert fires and is acknowledged. |
| Triage | 420 | Dashboard work: which method, which provider, which region. |
| Investigation | 300 | Confirming a cause well enough to justify changing routing. |
| Action | 360 | Getting the change approved and applied. |
| **Total** | **1620** | 27 minutes to mitigation. |

**These are assumptions and are labelled as such everywhere they appear**, including in the JSON
output and on the dashboard. They are deliberately generous to the human: an alert that fires
promptly, an engineer already at a laptop, no time lost working out who owns the problem, and no
pause to write an incident channel update.

They were **not** tuned against the agent's results. If you think 27 minutes is wrong, change
`ManualBaseline` and re-run — the comparison is a parameter of the harness, not a claim baked into
a slide.

What the comparison legitimately shows is the difference in *shape*: the agent's mitigation time is
dominated by detection latency and observation windows, both of which are configuration, while the
human's is dominated by investigation, which is not.

---

## 4. Known weaknesses

Reported rather than tuned away.

**Confidence calibration is the weakest metric.** Mean confidence when the diagnosis is correct is
close to mean confidence when it is wrong, and the Brier score reflects that. The scores come from a
readable additive rule, not a fitted model, and nothing has been calibrated against outcomes. A
fitted calibrator over more scenarios would improve this; at this corpus size it would mostly encode
the simulator's quirks.

**Revenue estimates are noisy on short windows.** Order amounts are lognormal with a long tail, so a
two-minute estimate carries real sampling error. The Impact Agent measures from incident onset and
flags an estimate as provisional below 400 payments, but the median absolute error is still tens of
percent early in an incident and tightens as the incident develops.

**Nine scenarios is a small corpus.** The accuracy figures have wide confidence intervals. Three
seeds reduce variance from traffic sampling but not from scenario design.

**The simulator is not production traffic.** It reproduces the structure that matters — heavy-tailed
amounts, diurnal arrival rates, correlated segments, ramped degradations, retries, a control plane
that responds to actions — but a real merchant's traffic will surface failure modes it does not
contain.

**`SCN-UPI-PSP-BADFALLBACK` is scored as a diagnosis failure and that is intentional.** All UPI
providers degrade together; the evidence still points at the worst-looking one, so the system
diagnoses a PSP fault, acts, and is wrong. Counting that as correct because the *response* was
well-handled would be marking our own homework. The response is scored separately, under
`appropriate_action_rate` and `rollback_success_rate`.
