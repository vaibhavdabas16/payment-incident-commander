# Demo guide

Two ways to run it. The terminal demo is the reliable one for a judging slot; the dashboard is the
one that looks good on a projector.

---

## Option A — terminal demo (30 seconds, no build step)

```bash
pip install -r requirements.txt
python -m pic.demo
```

Three scenarios run back to back against live simulated traffic. Nothing is scripted: the same
supervisor, agents, tools and policy gateway that the API and the benchmark use.

Use `--pace 0` to run it instantly, or `--pace 1.2` to let it breathe while you narrate.

---

## Option B — live dashboard

```bash
pip install -r requirements.txt
cd web && npm install && npm run build && cd ..
uvicorn pic.api.main:app --port 8000
# open http://127.0.0.1:8000
```

The backend warms up 45 simulated minutes of healthy traffic (about 20 seconds), then runs the
simulation at 60× real time. Click any scenario tile to inject an incident and watch the lifecycle
advance stage by stage over the WebSocket.

For hot-reloading frontend work, run `npm run dev` in `web/` and use `http://localhost:5173` — Vite
proxies `/api` and `/ws` to port 8000.

---

## The narrative

### Scenario 1 — the agent fixes it

*A single UPI PSP starts declining collect requests. UPI is the merchant's dominant method, so the
headline success rate falls hard. A healthy alternative route exists.*

Watch for:

1. **Detection** fires on a robust z-score plus a materiality floor plus a CUSUM change point —
   three independent tests that must agree. No model is involved.
2. **Investigation** runs a battery of read tools and produces findings with IDs. Every later claim
   cites those IDs, and the benchmark verifies that each cited ID exists.
3. **Impact** shows its arithmetic. Attempt rate × shortfall × per-stratum order value. You can
   check the multiplication on screen.
4. **Root cause** names the PSP with a confidence, and lists what argues *against* its own leading
   hypothesis.
5. **Decision** proposes moving 15% of UPI traffic, priced: expected value, revenue protected, risk.
   Note it scopes the shift to UPI rather than moving the merchant's healthy card volume.
6. **Policy gateway** approves — plain Python, no model in the path.
7. **Verification** is the part worth pausing on. It reports something like *treated 95% vs control
   29%*: the traffic that moved against the traffic left behind, in the same minutes. That is a
   concurrent control group, not a before/after comparison.

The incident usually ends **PARTIALLY_RECOVERED** after a second intervention, because a 15% shift
cannot close the whole gap and the system says so rather than claiming a win.

### Scenario 2 — the fix does not work

*The same symptoms, but every UPI provider is degrading together. Random variation still makes one
look worst, so the evidence points at a reroute that cannot possibly help.*

This is the scenario that matters. Watch for:

1. Confidence lands around **0.44**, below the merchant's 0.70 autonomous floor, so the **policy
   gateway holds the action for a human** — the "agent recommends action, human approval required"
   state. A human is notified and a ticket is filed; the incident does not sit silently.
2. The demo simulates an operator approving it.
3. **Verification fails against the control**: treated ≈ 45%, control ≈ 42%, p ≈ 0.29. The
   improvement is not distinguishable from noise.
4. The agent **rolls back its own change** — the audit trail shows the inverse action, and the final
   route weights are exactly what they were before.
5. It **escalates** rather than trying the same thing again.

If a judge asks one question, it will be about this scenario. The answer is that the control group
is what makes the failure detectable: a before/after comparison during a worsening outage cannot
distinguish "my fix did nothing" from "the incident got worse".

### Scenario 3 — nothing is broken

*A campaign drives a surge of wallet and netbanking traffic that has always converted worse.*

The headline success rate falls. **No incident opens.** Rerouting healthy traffic here would add
risk and protect no revenue, and the system's willingness to do nothing is a feature — it is the
false-positive control in the benchmark.

---

## Things worth pointing at

**The clamp.** Trigger a scenario and look at the policy stage. When a proposal exceeds a merchant
limit the gateway reduces it rather than refusing it, and records both:

```json
{"requested": {"percentage": 30}, "granted": {"percentage": 20},
 "outcome": "APPROVE_WITH_CLAMP", "bound_by": ["bound:max_traffic_shift_pct"]}
```

**The echo test.** In the investigation findings you will sometimes see a line like *"Excluding
payment_method=upi traffic, this segment returns to −5.9%, so it reflects that fault rather than an
independent one."* That is the system distinguishing a genuine second fault from a statistical
shadow of the first. Without it, one PSP outage reads as "multiple concurrent degradations" and
produces no actionable diagnosis.

**Incident correlation.** Leave the dashboard running through a long scenario. One fault stays one
incident, with a correlated-detection count, rather than opening a new one every monitoring cycle.

**The benchmark panel** at the bottom of the dashboard reads `evaluation/results/latest.json`. If it
says no results, run `python -m pic.evaluation.harness`. The dashboard never renders a number the
harness did not produce.

---

## If something goes wrong

**Port 8000 in use** — another instance is already running. On Windows:
`Get-NetTCPConnection -LocalPort 8000 -State Listen | Stop-Process -Id { $_.OwningProcess } -Force`

**Dashboard shows "no evaluation results"** — run the harness once; it takes a few minutes for three
seeds, or use `--seeds 7` for one.

**No incident appears after triggering** — detection needs enough volume in a window. Give it 30–60
seconds of wall clock at 60× speed. `SCN-TRAFFIC-MIX` is *supposed* to produce nothing.

**A Gemini key is set and calls are failing** — the system falls back to the deterministic reasoner
automatically and keeps running; the event stream shows `reasoner_degraded`. Benchmarks pin the
deterministic reasoner regardless, so results are unaffected.
