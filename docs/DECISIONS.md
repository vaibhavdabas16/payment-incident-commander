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
