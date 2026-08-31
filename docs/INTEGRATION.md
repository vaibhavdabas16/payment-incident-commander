# Running this on your own payments

The live demo runs a simulator: it generates its own traffic and applies its own fixes, which is
what makes it a closed loop you can watch end to end in ninety seconds. A real deployment replaces
both ends and **nothing in between** — the detector, the eight agents, the policy gateway and the
evaluation harness are the same code either way.

| | Demo | Your deployment |
|---|---|---|
| Where events come from | Simulator generates them | You `POST` them to `/api/v1/events` |
| The clock | Simulated, 60× | Real |
| Where actions go | An in-memory routing table | A signed webhook to your endpoint |
| Approvals | A button in the dashboard | `POST /api/v1/incidents/{id}/approve` |
| Reasoning | Gemini when a key is set | Deterministic by default — see [Why deterministic](#why-deterministic-by-default) |

Everything below is implemented and tested (`tests/test_integration_api.py`). What is *not* here
is in [Limits](#limits-worth-knowing-before-you-rely-on-this) — read that section before you point
production traffic at this.

---

## 1. Configure a merchant

Credentials come from the environment, never from a file in this repository:

```bash
export PIC_MERCHANTS='[{
  "merchant_id": "acme",
  "api_key": "pic_live_9f2c8ae4d1b74e6fa03c5d18",
  "policy_file": "/etc/pic/acme_policy.yaml"
}]'
```

Or, for platforms that mount secrets as files, `PIC_MERCHANTS_FILE=/run/secrets/merchants.json`
containing the same JSON.

| Field | Required | What it does |
|---|---|---|
| `merchant_id` | yes | Stamped on every event and incident |
| `api_key` | yes | Bearer token for `/api/v1/*`. Minimum 24 characters — shorter is refused, not warned about |
| `policy_file` | strongly recommended | Your limits and your route names. Without it, **every traffic-shifting action is denied** |
| `action_endpoint` | no | Where approved actions are sent. Omit it to run read-only |
| `action_secret` | with `action_endpoint` | HMAC secret for signing. An endpoint without a secret is refused at startup |
| `reasoner` | no | `deterministic` (default) or `auto` to use Gemini when a key is set |
| `monitoring_interval_s` | no | How often your traffic is swept. Default 60 |

Start the service normally. It logs `[live] serving 1 merchant(s): acme`, and `/api/v1/*` returns
`503` until at least one merchant is configured.

---

## 2. Send your payments

```bash
curl -X POST https://your-host/api/v1/events \
  -H "Authorization: Bearer pic_live_9f2c8ae4d1b74e6fa03c5d18" \
  -H "Content-Type: application/json" \
  -d '{"events":[{
        "payment_id":  "pay_29QL8xKm",
        "timestamp":   "2026-08-31T18:04:11Z",
        "amount_paise": 249900,
        "payment_method": "upi",
        "status":      "captured",
        "psp":         "psp_axis",
        "gateway":     "gw_primary",
        "issuer":      "hdfc",
        "route_id":    "route_upi_primary",
        "error_code":  null
      }]}'
```

**Five fields are required**: `payment_id`, `timestamp`, `amount_paise`, `payment_method`,
`status`. Everything else sharpens diagnosis when present and is `unknown` when absent — a
dimension that is always `unknown` simply never becomes a hypothesis, so you can start with five
fields and add the rest later without changing anything here.

- **Money is integer paise.** Never a float. A rupee amount that survives a round trip through
  binary floating point is not the amount you charged.
- **`status`** accepts `success`/`succeeded`/`captured`/`authorized`/`authorised`/`paid` and
  `failed`/`failure`/`error`/`declined`/`rejected`. Anything else is **rejected, not guessed**:
  mapping an unknown status to failed would invent an outage, and to success would hide one.
- **Timestamps** may be ISO-8601 with or without a zone; naive is read as UTC. More than 5 minutes
  in the future is refused as sender clock skew, and older than 6 hours is refused because it
  cannot inform a live baseline.
- **Retrying is safe and correct.** Duplicates are dropped by `payment_id`, because a duplicated
  failure is a degradation that never happened.
- Up to **1,000 events per call**. Send them as they settle, or batch on a short timer.

The response accounts for every event you sent:

```json
{"accepted": 98, "duplicates": 1, "rejected": [
  {"index": 44, "payment_id": "pay_x", "error": "unknown status 'pending_capture'; send one of [...]"}
], "oldest": "...", "newest": "..."}
```

One malformed row never costs the rest of the batch.

### Check it is working

```bash
curl -H "Authorization: Bearer $KEY" https://your-host/api/v1/status
```

Tells you how many events arrived, how many were rejected, whether the system may act, and —
importantly — whether your policy is loaded:

```json
"policy": {
  "source": "bundled default",
  "eligible_routes": ["route_A", "route_B", "route_C"],
  "warning": "using the bundled example policy, whose approved routes are the demo's. Traffic-shifting actions will be denied until policy_file names your own routes."
}
```

**Discover that during setup, not from a denied action at 2am.**

---

## 3. Give it your policy

Copy [`pic/policies/merchant_policies.yaml`](../pic/policies/merchant_policies.yaml) and edit it.
The gateway is plain Python reading this YAML — **no model is involved in deciding whether an
action is allowed**, which is the reason the system can be trusted with a write at all.

The two things you must change:

```yaml
routing:
  eligible_routes:            # YOUR route ids. A route absent here is never a destination.
    - route_upi_primary
    - route_upi_backup
  min_destination_success_rate: 0.80   # never move traffic onto something already unhealthy

bounds:
  max_traffic_shift_pct: 30   # the most that may move in one action
```

Also worth setting deliberately: `thresholds.min_confidence_for_autonomous_action` (below it, the
agent must ask a human), and `rate_limits` (how often it may act at all).

---

## 4. Receive actions

Without `action_endpoint` the system is **read-only**: it detects, investigates, prices and
diagnoses, and every attempt to act fails closed and is handed to a human with a reason. That is
the sensible way to start, and it is the default.

When you are ready, point it at an endpoint. Each approved action arrives as:

```http
POST /your/endpoint
Content-Type: application/json
X-PIC-Action: shift_traffic
X-PIC-Signature: t=1788170813,v1=6f3b...c14

{"action":"shift_traffic","parameters":{"from_route":"route_upi_primary",
 "to_route":"route_upi_backup","percentage":15.0,"payment_method":"upi"}}
```

**Verify the signature before doing anything.** The timestamp is inside the signed string, so an
old valid request cannot be re-stamped and replayed:

```python
import hashlib, hmac, time

def verify(secret: str, header: str, body: bytes, tolerance_s: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts, provided = int(parts["t"]), parts["v1"]
    if abs(int(time.time()) - ts) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)
```

Actions you may receive: `shift_traffic`, `disable_method`, `enable_method`, `configure_retry`,
`rollback_config_change`, `restore_config_change`.

**Your response is the source of truth.**

- **2xx** — applied. If you return `{"weights_after": {...}}`, that becomes the recorded state.
- **Anything else, or a timeout** — treated as *not applied*. The agent records a failed
  intervention and hands the incident to a human.

It never assumes success, and that is not politeness — the entire verification story rests on the
system knowing what it actually did. An action recorded as done but never applied would be measured
against traffic it never touched.

---

## 5. Approve, and watch it check itself

When the policy gateway holds an action for a human, the incident sits in
`AWAITING_HUMAN_APPROVAL`:

```bash
curl -H "Authorization: Bearer $KEY" https://your-host/api/v1/incidents
curl -X POST -H "Authorization: Bearer $KEY" \
  -d '{"approver":"ops@acme.com"}' \
  https://your-host/api/v1/incidents/INC-0001/approve
```

Approving does **not** skip verification. The agent applies the change, stands back for
`observation_seconds` of real time while your payments keep arriving, and measures the result
against a concurrent control group — traffic deliberately left on the old route — so your incident
recovering on its own cannot be mistaken for the fix working. If the numbers do not improve it
sends the **exact inverse action** and hands over.

`POST .../reject` declines instead, and the incident is escalated with that on the record.

Every handover states its case in your numbers, not a category:

> Shift traffic did not help: 71.2% on treated traffic against 70.8% on a control group left
> alone (p=0.61). The change has been reverted. The diagnosis may be wrong, or the fault may sit
> outside what routing can reach.

---

## Why deterministic by default

A live merchant gets the deterministic reasoner unless they ask for `"reasoner": "auto"`.

A payment system's decisions must not be gated on a third-party API being reachable. When the model
is slow the decision agent hits its 30-second timeout and the incident escalates unactioned — even
though a perfectly good proposal was available locally the whole time. This is not hypothetical:
it is what happened while building this integration, and it cost 190 seconds per incident before
the timeout caught it.

It is also the path the benchmark measures — 90.9% root-cause accuracy, 99.5% detection precision —
so the deterministic default is the configuration those numbers actually describe.

---

## Limits worth knowing before you rely on this

Stated plainly, because finding these out during an incident is worse than reading them now.

- **Single process, in-memory.** Events live in RAM with a six-hour retention window, and incident
  history does not survive a restart. Two replicas would each see half your traffic and neither
  would have a correct baseline. Run one instance, or shard by merchant.
- **Baselines are earned, not assumed.** Detection compares against history, so the first ~30
  minutes after you start sending are spent learning what normal looks like. A merchant who
  integrates *during* an outage will have that outage learned as normal — which is true of every
  monitoring system, and worth planning around.
- **A long degradation drags the rolling baseline toward it.** Detection is designed to fire early;
  an outage that has run for an hour before you start sending events may not register as one.
- **No backfill.** Events older than six hours are refused, so you cannot warm a baseline from
  history. It has to accumulate live.
- **Rate limiting and quotas are not implemented.** The API key authenticates; it does not throttle.
  Put this behind your own gateway.
- **One approval at a time.** An incident holds for a single human decision; there is no queue,
  escalation path or on-call rotation integration.
- **The demo API (`/api/*`) is unauthenticated** and shares the process. It serves the simulated
  dashboard, not your data — no route there can read a merchant's events — but if you deploy this
  for real, do not expose it.
