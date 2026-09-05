# Five-minute demo script

Read this top to bottom while screen recording. Bold text is what you do. Everything in quotes is
what you say. Anything in `[square brackets]` is a number you read off your own screen — it changes
every run, so don't rehearse it.

---

## Setup

Two terminals.

```bash
cd web && npm run build && cd ..
PIC_LLM_PROVIDER=deterministic uvicorn pic.api.main:app --port 8000
```

Open the dashboard at **exactly this URL**:

```
http://127.0.0.1:8000/?session=demo
```

The `session=demo` part matters. Every browser gets its own private world, and the next command
has to talk to the one you are looking at.

Second terminal:

```bash
curl -X POST "http://127.0.0.1:8000/api/control/speed?speedup=4&session=demo"
```

Detection needs about two minutes of real traffic before it will open an incident. At 4x that is
thirty seconds, which is what the script is written around.

Then, before you hit record:

1. Let the page sit twenty seconds so the charts have traffic in them.
2. **Overview → Load demo history.**
3. Go to **Simulate**. Have *UPI PSP route degradation* ready to click.
4. Re-taking? Press **Reset** on Simulate first.

---

## Where everything is

**The incident page is one long scroll.** You never navigate inside it — you only scroll down. The
sections always come in this order, top to bottom:

| # | Section | How you recognise it |
|---|---|---|
| 1 | **Hero** | Big title, incident ID top right, one huge rupee figure |
| 2 | **Recommended recovery** | The recommendation in a sentence, four stats under it |
| 3 | **Every recovery it considered** | A table, selected row highlighted |
| 4 | **Recovery verification** | Two coloured blocks side by side |
| 5 | **Revenue actually recovered** | Five figures in a row |
| 6 | **What the agent learned** | |
| 7 | **Prevention opportunity** | Only if the pattern has recurred |
| 8 | **Handed to a human** | Only if it escalated |
| 9 | **Agent trace** | The long technical log. Skip it on camera |

### The exact numbers the script asks you to read

| Script says | Where it is | Exact label on screen |
|---|---|---|
| `[revenue at risk]` | Hero, the big number | **"Revenue at risk, this incident"** |
| `[confidence]` | Recommended recovery, 2nd stat | **"Confidence in the cause"** |
| `[the percentage]` | Recommended recovery, the sentence in large type | e.g. "Shift 15% of UPI traffic…" |
| `[history line]` | Recommended recovery, 4th stat | **"Historical success"** |
| `[control %]` | Recovery verification, **left** block | **"Control"** — the big % |
| `[treatment %]` | Recovery verification, **right** block | **"Treated"** — the big % |
| `[at risk / protected / recovered]` | Revenue actually recovered | Labelled **At risk · Protected · Recovered · Lost · Recovery rate** |

Two things worth knowing so you don't stumble:

- The **hero** also carries small stats (Recovered, Protected, Recovery, Duration, Payments
  rescued). Those are the same figures as section 5. Read them from section 5 — it's the clearer
  shot.
- Between the two verification blocks is the gap in **percentage points** and the **p-value**.
  That's the "at that p-value" line if you want it, but you can skip it.

### Sidebar navigation

Only three sidebar items appear in this script:

- **Simulate** — to start the scenario (and **Reset** between takes)
- **Incidents** — where the incident opens; click the row to open the page above
- **Learning** — the playbook, at **"Learned recovery playbook"**, then scroll to
  **"Prevention opportunities"**
- **How it works** — the closing benchmark numbers

---

## The script

### 0:00 · Landing page, top

"This is Payment Incident Commander. It's an autonomous payment recovery and revenue protection
agent.

When payments start failing, most tools tell you that something is wrong. The hard part is
everything after. Which of five providers is at fault. How much money is walking out of the door.
What to do about it that won't make things worse. And whether the thing you did actually worked.

So the goal here isn't to manage the incident. It's to recover the revenue."

### 0:25 · **Scroll down to the loop band**

"Revenue at risk. Detect. Diagnose. Recover. Verify. Revenue protected. Learn. Prevent.

Money at both ends, on purpose. Let me break something and show you."

### 0:35 · **Simulate → click "UPI PSP route degradation" → go to Overview**

"I've just degraded a UPI provider. Nothing here is scripted. The same detector, the same agents
and the same policy gateway run whether I'm watching or the benchmark is.

Detection needs three independent statistical tests to agree before it will open an incident, so it
won't fire on one noisy window. It takes a few seconds of real traffic."

*(Keep talking while you wait.)*

"And that's deliberate. A payments team that gets paged for every wobble stops reading the pages."

### 1:05 · **Incident appears — click into it**

"There it is. And the first thing the page tells me isn't a stack trace. It's `[read the revenue at
risk]` an hour of merchant revenue at risk, priced from this merchant's own traffic, by payment
method and order value band.

Underneath, the cause. UPI on PSP Axis, at `[read the confidence]` confidence, with the evidence it
was drawn from."

### 1:25 · **Scroll to "Recommended recovery"**

"Now the part that makes this a product rather than a dashboard.

It recommends shifting `[read the percentage]` of UPI traffic off the degraded route. It tells me
what it expects that to be worth, how confident it is, what it's risking, and what happened the
last time it tried this. `[read the history line]` comparable incidents improved.

That's not a language model remembering something. It's a count over structured records of what
actually happened."

### 1:50 · **Point at the exposure panel on the right**

"And unprompted, it tells me what happens if it's wrong. Only that share of traffic moves. The rest
stays exactly where it is. Nothing is judged for three minutes. If it doesn't help, it's undone
automatically."

### 2:05 · **Scroll to the strategy table**

"It also shows every recovery it didn't pick and what each one was worth. The objective isn't to
maximise success rate. It's to maximise recovered revenue inside the merchant's risk limits."

### 2:20 · **Scroll back up, point at the policy line, click Approve**

"Every action passes a deterministic policy gateway. Plain Python, evaluating the merchant's own
config. No model anywhere in that path.

Here it's clamped the shift to what merchant policy allows, and it's holding it for a human because
confidence is below their autonomous floor.

AI proposes. Policy decides. The system verifies. I'll approve it."

> If it already acted on its own, say instead: "Policy decided this one was well inside the
> merchant's limits, so it didn't need me. That's the same gateway making the opposite call."

### 2:45 · **Scroll to verification** — *your strongest thirty seconds, slow down here*

"This is the part I'd point at first.

We do not claim recovery just because payment success went up. When the agent moved that traffic,
it deliberately left the rest of it alone. So there's a control group living through the same
outage, the same hour, the same customers.

Control, `[read control %]`. Treated, `[read treatment %]`. That gap is the intervention. Not the
incident recovering on its own.

And that distinction matters, because those two situations call for opposite responses. Without a
control group, a fix applied during a worsening outage looks like it caused the damage, and the
system rolls back something that was working."

### 3:20 · **Scroll to the revenue ledger**

"So the ledger closes. `[at risk]` at risk. `[protected]` protected. `[recovered]` recovered from
payments that had already failed. And the rest, lost.

Those figures are disjoint by construction. They sum to the exposure exactly. The benchmark asserts
that on every single incident, because a system reporting a hundred and thirty percent recovery
rate hasn't had a good day. It has a bug."

### 3:45 · **Scroll to "What the agent learned", then go to the Learning page**

"When the incident closes, the whole thing becomes one structured record.

On the Learning page that becomes a playbook. For this failure signature it now prefers a moderate
traffic shift. And it says what to avoid: the larger shift helped in only two of five comparable
incidents, and three of those had to be rolled back.

To be straight with you, fourteen of these records are seeded demo history, and the interface
labels them as seeded. But they describe the same failure this scenario produces, so the live
incident retrieved them because the signature genuinely matched. Nothing was special-cased."

### 4:15 · **Scroll to Prevention**

"And the last step is prevention. This pattern has recurred often enough to be worth stopping
rather than recovering, so it proposes preventing it. That's a recommendation, not an action.
Merchant policy only ever changes when a person edits the policy file."

### 4:30 · **Go to "How it works"**

"One last thing, and it's the one I'd want a reviewer to check. There's a scenario where every UPI
provider is failing at once, so the obvious reroute cannot possibly work. The agent tries it,
measures no improvement against the control, reverts its own change, and escalates with the
evidence.

Across the benchmark: zero policy violations. Zero unauthorised executions. Every rollback
succeeded. Detection precision ninety-nine point five percent. Root cause accuracy ninety-one
percent. Median time to mitigate, a hundred and thirty-six seconds, against a modelled human
baseline of twenty-seven minutes.

All of that is generated by a reproducible harness. None of it is typed into a slide."

### 4:55 · Close

"Detect. Recover. Verify. Learn.

Recover the revenue, not just the incident. Thank you."

---

## If a take goes sideways

Runs differ because traffic is sampled. None of these is worth re-recording.

| What you see | What to say |
|---|---|
| Detection is slow | "It needs three tests to agree. It won't open an incident on one noisy window." |
| It approves itself | "Policy decided it was inside the merchant's limits. Same gateway, opposite call." |
| Verification says **partially recovered** | "Real improvement, but not the whole gap, so it stays open rather than claiming a win." |
| Verification says **inconclusive** | "Not enough traffic to judge yet, so it waits rather than guessing." |
| Prevention panel is empty | Skip it. A pattern needs three comparable incidents. |
| Order recovery says not authorised | "Merchant policy withheld that method. It says so instead of doing it quietly." |

## The thirty-second version

If you get cut short:

"Payments start failing. This prices the revenue at risk, picks the safest recovery, executes it
inside the merchant's policy, then proves it worked by comparing against traffic it deliberately
left alone. If it didn't work, it rolls itself back. Every incident becomes a record that makes the
next recovery better. Zero policy violations across the benchmark, and the AI never executes
anything itself. It proposes. Policy decides."
