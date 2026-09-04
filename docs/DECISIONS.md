# Architecture Decision Records

Decisions that a senior engineer reading this repo would otherwise have to reverse-engineer.
Each records the choice, the reasoning, and what we gave up.

---

### ADR-001 — Detection is deterministic; the LLM never detects

**Decision.** Anomaly detection uses EWMA baselines, robust (MAD-based) z-scores, a Wilson lower
bound on proportions, and a CUSUM change-point test. No model call participates.

**Why.** Detection is the one component whose quality must be *measured* (precision, recall,
latency) across thousands of windows. An LLM verdict per window is non-reproducible, expensive at
that cardinality, and cannot be calibrated. It also fails badly on the exact thing that matters:
distinguishing a 3% dip on 40 transactions (noise) from a 12% dip on 900 (real).

**Trade-off.** Purely statistical detection misses semantic anomalies that a model might catch
(e.g. "this error message is new"). We compensate by feeding the *dominant error code shift* in as
an explicit statistical feature rather than reaching for a model.

---

### ADR-002 — The policy gateway contains no model, and is the only path to execution

**Decision.** `pic/policies/gateway.py` is plain Python. The Action Agent physically cannot execute
a write tool without a `PolicyDecision` object carrying `approved=True`, and the supervisor refuses
the `EXECUTING` transition otherwise.

**Why.** This is the safety claim of the whole product. If an LLM could be prompted into
authorising a traffic shift, every other guardrail is decoration. Keeping the gateway deterministic
makes "zero policy violations" a testable property rather than a hope.

**Trade-off.** The gateway cannot handle situations its rules do not anticipate. That is the point:
unanticipated situations route to `REQUIRE_APPROVAL`, not to model discretion.

---

### ADR-003 — Gemini for reasoning, behind a provider interface, with a deterministic fallback

**Decision.** `pic/llm/base.py` defines `Reasoner`. `GeminiReasoner` calls the Gemini API;
`DeterministicReasoner` implements the same interface with rule-based scoring. Selection is by
`PIC_LLM_PROVIDER` env var, defaulting to Gemini when `GEMINI_API_KEY` is present and falling back
otherwise. Evaluation runs pin the provider explicitly.

**Why.** The user supplied a Gemini key, so the reasoning is genuinely model-driven. But a
hackathon demo that dies on a network hiccup — or an evaluation harness whose numbers change run to
run — is worse than useless. The fallback also lets `pytest` run offline in CI with no key.

**Trade-off.** Two implementations of the reasoning surface to maintain. Contained by keeping the
interface to three methods (`rank_hypotheses`, `propose_action`, `summarize`) and having both
return the same validated Pydantic types.

**Consequence for honesty.** Benchmark tables in the README state which reasoner produced them.

---

### ADR-004 — SQLite by default, Postgres supported, Docker optional

**Decision.** SQLAlchemy models with a SQLite file as default (`PIC_DATABASE_URL` overrides).
`docker-compose.yml` with Postgres is committed but is not on the critical path.

**Why.** The spec asked for Postgres + Docker, but Docker is not installed on the target machine and
the harder requirement is "runs locally with a single clear setup process." A judge cloning this
repo gets a working demo from `pip install -e . && python -m pic.demo` with no daemon. Nothing in
the code depends on a Postgres-only feature.

**Trade-off.** SQLite write concurrency is poor. Irrelevant here: the simulator writes in batches
from one process.

---

### ADR-005 — Explicit finite state machine, not an autonomous agent loop

**Decision.** The supervisor is a hand-written FSM with a transition table. No agent decides what
happens next; agents only produce outputs for their own state.

**Why.** Free-running loops are unauditable and untestable, and they re-plan under uncertainty in
ways that are unacceptable when the side effects are financial. An FSM gives a fixed audit spine, a
provable "no execution without approval" invariant, and a UI that can render lifecycle progress
honestly because the stages are real.

**Trade-off.** Less adaptive than a planner. Acceptable: incident response is a well-understood
workflow, not an open-ended task.

---

### ADR-006 — No vector database

**Decision.** Incident memory retrieval is deterministic weighted feature matching.

**Why.** The corpus is tens of incidents, not millions. Embedding similarity here would be slower,
opaque, and dependent on another service, while producing worse matches than exact agreement on
`(error_code, psp, issuer)`. The spec explicitly said not to add one without real value.

**Trade-off.** No fuzzy semantic recall over free-text postmortems. Not needed — our incident
records are structured.

---

### ADR-007 — Ground truth is physically unreachable from agent tools

**Decision.** Scenario labels live in a `ground_truth` table that the `ToolRegistry` refuses to
expose, with a test asserting no registered tool touches it.

**Why.** The most common way a hackathon evaluation becomes a lie is an agent that can see the
answer key. Making the isolation structural (and tested) is what makes the diagnosis-accuracy
number meaningful.

---

### ADR-008 — Money is integer paise end to end

**Decision.** All monetary values are `int` paise. Formatting to `₹3.7L` happens only in the
presentation layer.

**Why.** Float rupees accumulate error across aggregation and make revenue-at-risk comparisons
non-reproducible. Standard payments practice.

---

### ADR-009 — Verification is a statistical test, not a comparison of two numbers

**Decision.** `VerificationAgent` runs a two-proportion z-test between the pre- and post-action
windows and requires both a significant p-value and a minimum absolute improvement.

**Why.** Success rate rising 74% → 78% on thin volume is frequently noise. Declaring victory on
noise is exactly the failure mode that would make an autonomous system dangerous — it would close
incidents that are still live.

**Trade-off.** Needs sufficient post-window volume; with too few transactions the agent returns
`INCONCLUSIVE` and holds the incident open rather than guessing.

---

### ADR-010 — Memory is written and queried only by code, and its influence is hard-capped

**Context.** The system now decides partly on the basis of what happened last time. That creates a
new and serious failure mode: an agent that can *recall* a past incident can *invent* one, and a
fabricated precedent is far more dangerous than a fabricated observation. "Nine of eleven similar
incidents recovered with a 20% reroute" is exactly the kind of sentence a language model produces
fluently and cannot be held to.

**Decision.**

1. The only writer of memory is `pic/memory/build.py::build_outcome_record`, which copies fields
   from typed agent outputs. No model is on that path.
2. Retrieval is a deterministic weighted match over a structured `FailureSignature`. No model is on
   that path either.
3. Historical evidence is computed *before* the reasoner is called and handed to it as structured
   data. The model may weigh and explain it; it cannot add to it.
4. What history is permitted to do is move one number: the efficacy prior of a candidate action,
   through a shrinkage estimator, clamped to ±`MAX_HISTORY_ADJUSTMENT` (0.20). It cannot change a
   measured rupee figure, create an action, widen a policy limit, or reach the gateway.

**Consequences.** A hundred consecutive historical successes at a magnitude the merchant does not
permit still produce a clamped action, because the clamp is downstream of everything history
touches. The cap also means the first incident that looks familiar but is not cannot be
confidently mishandled: memory tilts a close call, it does not decide.

The alternative — letting retrieved outcomes set `p_success` directly — was rejected because it
makes the system's behaviour a function of its most recent luck. Two lucky outcomes would move an
efficacy prior from 0.85 to 1.0 and the agent would stop pricing the downside at all.

Asserted in `tests/test_learning_and_recovery.py`, section *Safety under learning*.

---

### ADR-011 — Prevention recommends; it never applies

**Context.** Once the system can see that PSP Axis degrades at the same time every evening and has
cost a known number of rupees, the obvious next step is to act on it preemptively. That is also the
point at which an agent would be modifying its own standing authority.

**Decision.** Pattern mining emits a `PreventionRecommendation`, which is a document: the pattern,
its conditions, the incidents behind it, the money it has cost, the action that has actually worked
against it, and an estimated benefit derived from both. It has no path into the pipeline. Merchant
policy lives in `pic/policies/merchant_policies.yaml` and is changed by a person editing that file.

`POST /api/prevention/{id}/accept` records who accepted it and returns `policy_changed: false`. The
dashboard card says the same thing where somebody deciding will read it.

**Consequences.** The most valuable thing the system learns is the thing it is least able to act on
alone, and that is the correct trade. A merchant's standing grant of authority is the one artefact
in the system that must only ever move deliberately, in a file a human owns and can diff.

We also decline to recommend prevention for a pattern the system has never actually fixed, or for
one whose incidents were false positives. A scheduled reroute against a traffic-mix change is not
prevention, it is harm on a timer.

---

### ADR-012 — The three revenue figures are disjoint by construction

**Context.** Revenue recovery is the headline business metric, so the way it can most easily be
wrong is by counting the same rupee twice — once as loss prevented and once as revenue recovered.
A system reporting a 130% recovery rate has not had a good day; it has a bug.

**Decision.** `revenue_protected` is forward-looking (loss the intervention prevented, for the time
it was actually in force), `revenue_recovered` is backward-looking (payments that had already failed
and were then completed), and `revenue_lost` is the remainder. Each per-hour rate is applied only to
the seconds it was measured over. The identity

```
revenue_at_risk = revenue_protected + revenue_recovered + revenue_lost
```

holds by construction, is enforced by a cap-and-rescale in `pic/revenue.py`, and is asserted per
incident in the benchmark as `revenue_identity_rate` (which must be 1.0).

**Consequences.** An incident that was never priced reports `measurable: false` and zeroes rather
than an estimate. Portfolio rates are recomputed from totals rather than averaged, because the
arithmetic mean of percentages is not a percentage of anything.

---

### ADR-013 — Recovered orders are counted, not injected into the payment stream

**Context.** The second recovery layer re-presents payments that already failed. The obvious
implementation is to write the resulting attempts into the event store as new payment events.

**Decision.** It does not. A recovery campaign's outcomes are recorded against the campaign, in the
control plane, and never appear as rows in the payment stream.

**Consequences.** This is a deliberate limit on the simulation, and the reason is verification: the
Verification Agent measures the success rate of traffic on the treated and control routes, and
injecting a batch of retried payments — all of them from the failed population, none of them
subject to the routing split — would contaminate exactly the measurement the whole safety story
rests on. A recovered order is reported as a recovered order and as rupees in the ledger, which is
what it is.

The honest cost: recovered payments do not appear in the merchant's headline success-rate chart. In
a real deployment they would arrive through the normal payment path and be measured like any other
payment; here the boundary is drawn where it keeps the experiment clean.
