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

### Revenue recovery
The metric the merchant actually cares about, and the one most easily made to lie.

- `revenue_at_risk_paise`, `revenue_protected_paise`, `revenue_recovered_paise`,
  `revenue_lost_paise` — totals across every priced incident, in integer paise.
- `recovery_rate` — `(protected + recovered) / at_risk`, recomputed from the totals. Never an
  average of per-incident percentages: the arithmetic mean of percentages is not a percentage of
  anything, and averaging would let a trivial incident that recovered perfectly cancel out a large
  one that did not.
- `revenue_identity_rate` — **must be 1.0**. The share of incidents where
  `at_risk == protected + recovered + lost` exactly. Anything less means a rupee is being counted
  twice or dropped, and every figure above it is fiction. See ADR-012.
- `orders_recovered` / `orders_recoverable` / `orders_failed` — the second recovery layer, with a
  denominator, so "7,100 recovered" is reported as a share of what was recoverable rather than on
  its own.

### Learning
Whether remembering changes the decision, measured as an experiment rather than asserted.

`measure_learning` runs the same failure four times against **one shared memory** — three
like-for-like incidents and one where the obvious fix cannot work, so the record accumulates a
failure as well as successes. Each repetition is a fresh world with its own simulator, detector and
traffic, so the only thing carried between them is what was deliberately written to memory. If the
prior moves, nothing else can account for it.

- `history_available_rate`, `mean_comparable_incidents`, and
  `comparable_incidents_first_run -> comparable_incidents_last_run`: whether history is actually
  retrievable by the time it is needed.
- `mean_efficacy_adjustment` and `max_efficacy_adjustment`: how far history moved the prior. Zero
  here would mean memory is being retrieved and then ignored, which is the failure mode this metric
  exists to catch. The maximum is bounded by `MAX_HISTORY_ADJUSTMENT` (0.20) by construction.
- `policy_consulted_rate` (**must be 1.0**) and `unauthorised_executions` (**must be 0**), recorded
  per repetition. The claim being tested is not merely "history changes the decision" but "history
  changes the decision without touching anything that keeps the decision safe", and only the second
  half is worth trusting the first for.
- `prevention_recommendations`, `prevention_all_require_approval`, `prevention_none_applied`: the
  patterns mined from the run, and the fact that none of them changed anything.

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

**Confidence is only roughly calibrated.** It used to be *inverted* - the agent was on average
more certain when it was wrong than when it was right, which is the worst possible shape for a
number that gates autonomous action. That came from nothing tying the strength of a claim to the
share of the failures it explained: one run asserted a payment-method fault at 77% confidence on a
segment carrying 24% of the failures.

Confidence is now capped by explanatory coverage - a segment-level cause cannot be stated more
confidently than the failures its segment carries, plus a small allowance for the same fault
appearing in several segments. This is a consistency rule, not a calibration: it never looks at
whether the diagnosis was right, only at whether the evidence supports the strength of the claim,
and it runs after ranking so it cannot reorder hypotheses. Mean confidence when correct now exceeds
mean confidence when wrong, and the Brier score improved, but the margin is small and the score is
still mediocre. A calibrator fitted over more scenarios would do better; at this corpus size it
would mostly encode the simulator's quirks.

**Reverting a config rollback now works.** It previously could not: `rollback_change` recorded
intent and the simulator kept degrading traffic regardless, so the one action that actually
addresses a config regression was guaranteed to look useless. Verification read no improvement and
the agent dutifully reverted its own correct fix - every failed rollback in the benchmark was one
of these, which made the metric a statement about the simulator rather than about the agent. The
control plane now honours the rollback: a degradation caused by a recorded change stops when that
change is reverted, and the action carries a real inverse so it is reversible like any other
write. `SCN-ANDROID` now recovers on all three seeds, rollback success is 1.0 of 7, and
unnecessary escalation fell to zero.

**Historical note on the same metric.** The harness now models an operator granting the approval the gateway asks for, so runs
continue past the gate instead of stopping there. That raised verification coverage from 3 runs to
18 and rollbacks from 1 to 11 - and the honest rollback success rate is 0.45, not the 1.0 that a
single sample used to report.

The split is exact. All five reverts of a `shift_traffic` or `create_incident_ticket` succeeded.
All six failures are `rollback_change`: the simulator has no notion of un-deploying an SDK, so the
action records intent and is flagged `partial_effect`, and there is nothing for the revert to
undo. No traffic-shift revert has ever failed.

`approval_required_rate` is reported alongside, because needing a human before acting is a safety
result in its own right and continuing past the gate must not hide it.

**Revenue estimates are noisy on short windows, and were biased until measured.** They were
systematically low: 21 of 24 runs under-estimated, median signed error -41.8%. Valuation sums over
a disjoint `(payment_method x amount_band)` partition and required each cell to hold 20 payments
before contributing; on a two-minute window most cells hold fewer, and instrumenting the estimator
showed the discarded cells carried more of the true loss than the ones that survived - three times
as much on some runs.

Lowering the floor would have been wrong, because a thin cell's deviation is extremely noisy and
two scenarios already over-estimated. The sub-threshold cells are pooled into one residual group
instead, which is sound because the partition is disjoint, and the pooled group faces the same
significance bar so a healthy window still contributes nothing. Median signed error is now -1.0%
with errors falling either side evenly, and the share within 25% of truth more than doubled.

What is left is sampling error on lognormal amounts over a short window: the median *absolute*
error is still around 39%, and it tightens as an incident develops. The Impact Agent flags an
estimate as provisional below 400 payments.

**Reproducibility.** `Agent.execute` charges each step's measured wall time to the simulated clock,
which is right for the live dashboard and wrong for a benchmark - it makes the result depend on how
busy the machine is. The harness pins a fixed simulated cost per step instead. Detection sampling
was verified byte-identical across separate processes under different `PYTHONHASHSEED` values, so
hash ordering does not affect results either. One historical run reported a false-positive count of
6 where every run before and since reports 5; that has not been explained.

**Learning is now measured, but on a very small corpus.** This used to read "incident memory is
live in the app but unmeasured by this benchmark", and that was the honest position at the time:
the harness built a fresh Engine per scenario so memory started empty every run, and `record()`
fired only on closure while most runs parked at the approval gate.

Both causes are fixed. `measure_learning` carries one memory across repetitions deliberately, and
models an operator granting up to three approvals so the incident reaches a terminal state and
writes what it learned — an experiment about learning cannot be run on incidents that never finish.

What it now shows is real but small: comparable history goes from none on the first repetition to
one or two by the fourth, and the efficacy prior moves by a few hundredths. That is the shrinkage
estimator behaving exactly as designed — four comparable incidents is roughly where history starts
to weigh as much as the prior — but it means the *magnitude* of the effect here is a statement
about a four-incident corpus, not about the mechanism at scale. The bounded-adjustment property is
proven directly in `tests/test_learning_and_recovery.py` with sixty synthetic records, where the
adjustment reaches and stops at the cap.

Two further limits worth stating. Only the *last* executed action is recorded per incident, so an
incident that shifted 20% and then 15% after a partial recovery is remembered as a 15% shift — the
record is per incident, not per attempt. And the retrieval floor is a threshold, so an incident
that sits just below it contributes nothing at all rather than contributing weakly; a graded
weighting by similarity would be better and is not implemented.

**Revenue recovery numbers depend on the recovery-probability model.** The share of already-failed
payments that come back is measured from the retry outcomes in the merchant's own traffic where
there are at least twenty of them for that error code, and falls back to a documented per-class
prior otherwise (`pic/recovery/orders.py`). In the simulator most error codes fall back. The
population figures — how many payments failed, how many are recoverable, how many the customer
completed themselves — are exact counts over the event store; the *conversion* of an attempt into a
recovery is the modelled part, and it is the part to be sceptical of.

**`SCN-MULTI` is now diagnosed correctly, by looking again after acting.** Its second fault - an
issuer on cards, alongside a UPI provider outage - needs roughly six minutes of traffic to become
significant, while the first decision has to be made after two. Waiting for it before deciding was
tried and was clearly wrong: it delayed every incident past its approval and broke the revert path.
The diagnosis is instead revised *after* the action has been taken and verified, pooling evidence
from onset, so the mitigation timeline is untouched and only the diagnosis handed to the human
improves. If the wider look does not establish a second fault, the original diagnosis stands.

Two supporting corrections were needed. `multi_factor` scored a flat prior, so the strongest
partial explanation always outranked the account covering everything; it is now scored from the
hypotheses it subsumes. And the echo test filed the second fault as an echo of the first: it
re-measures a candidate's whole dimension, which for an issuer spans every payment method and
dilutes a card-only fault into insignificance. A segment that names a shared dimension with a
different value cannot contain the same payment as the primary, so it cannot be an echo of it -
that is now decided directly rather than by a test that cannot see it.

**Payment events are a slotted dataclass, not a model.** They never cross a trust boundary - the
simulator is the only thing that constructs one, nothing serialises them, and every consumer reads
them by attribute - and as a pydantic model each carried a `__dict__` plus a
`__pydantic_fields_set__` holding all twenty-one field names, about 3.2KB an event. As a slotted
dataclass it is roughly 560 bytes, and a warmed engine holding two simulated hours fell from about
250MB to 19MB. The one invariant the type enforced is still checked on construction.

**Nine scenarios is a small corpus.** The accuracy figures have wide confidence intervals. Three
seeds reduce variance from traffic sampling but not from scenario design.

**The simulator is not production traffic.** It reproduces the structure that matters — heavy-tailed
amounts, diurnal arrival rates, correlated segments, ramped degradations, retries, a control plane
that responds to actions — but a real merchant's traffic will surface failure modes it does not
contain.

**`SCN-UPI-PSP-BADFALLBACK` is now diagnosed correctly on two of three seeds.** All UPI providers
degrade together, and the whole-method hypothesis used to be unreachable because it demanded a
concentration ratio the largest payment method cannot reach even when it is wholly at fault. With
that bar aligned to the module's own `strong_dimensions` test, the method-level cause wins on two
seeds. On the third an issuer lands at a concentration of exactly 1.25 against a `< 1.25`
threshold, is judged an independent second fault, and the provider-level explanation wins instead.
That constant was left alone: moving it to 1.26 would fix the seed and demonstrate nothing.
