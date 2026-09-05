import { useState } from 'react'
import { formatDuration, formatFact, formatINR, formatPct } from './api.js'
import {
  Card, DecisionTrace, Led, NextSteps, PreventionCard, RevenueBar, SimilarIncidents, Skeleton, Tag,
  readableAction,
} from './components.jsx'

/* One incident, told as one story.
 *
 *   revenue at risk → what is breaking → why → what the agent will do → why that → what if it is
 *   wrong → it ran → did it work → how much came back → what it learned → how to prevent it
 *
 * The order is the argument. A merchant does not open this page to audit an agent pipeline; they
 * open it because money is going missing and they want to know whether anything is being done
 * about it. The agent trace is real and stays, but it is evidence for the story rather than the
 * story itself, so it sits at the bottom behind a disclosure.
 *
 * Every figure on this page comes from a typed field the agents recorded. Where the system has not
 * produced something yet the section says so; nothing is filled in to make the page look complete.
 */

export default function IncidentStory({
  incident, trace, learning, agents, metrics, busy, onApprove, onReject, onStep, onGoto,
}) {
  if (!incident) return <Card><Skeleton rows={6} /></Card>

  const closed = incident.state === 'CLOSED'
  const awaiting = incident.state === 'AWAITING_HUMAN_APPROVAL'
  const traceForThis = trace && trace.incident_id === incident.incident_id ? trace : null

  return (
    <div className="story">
      <Hero incident={incident} closed={closed} awaiting={awaiting} now={metrics?.timestamp} />
      <Recommendation
        incident={incident} trace={traceForThis} busy={busy}
        awaiting={awaiting} onApprove={onApprove} onReject={onReject}
      />
      <StrategyTable incident={incident} />
      <VerificationPanel incident={incident} />
      <RevenueOutcome incident={incident} />
      <LearnedFromThis incident={incident} onGoto={onGoto} />
      <PreventionOpportunity incident={incident} learning={learning} onGoto={onGoto} />
      {incident.escalation ? (
        <Handover incident={incident} busy={busy} onStep={onStep} />
      ) : null}
      <AgentTraceSection incident={incident} trace={traceForThis} agents={agents} />
    </div>
  )
}

/* ------------------------------------------------------------------- hero */

/** What is breaking, and what it is costing.
 *
 *  This is *this incident's* exposure, never the portfolio's. The two were previously rendered in
 *  the same visual register, which invited a reader to add them together or mistake one for the
 *  other. The label says which, every time. */
function Hero({ incident, closed, awaiting, now }) {
  const rev = incident.revenue
  const im = incident.impact
  const atRisk = rev?.measurable ? rev.revenue_at_risk_paise : null
  const rate = im?.revenue_at_risk_per_hour_paise
  const rc = incident.root_cause
  const duration = rev?.exposure_seconds ?? elapsed(incident, now)

  const state = closed
    ? { text: 'Incident closed', tone: 'ok' }
    : awaiting
      ? { text: 'Waiting for your decision', tone: 'warn' }
      : { text: 'Active incident', tone: 'crit' }

  return (
    <section className={`hero ${state.tone}`}>
      <div className="hero-top">
        <span className="hero-state">
          <Led tone={state.tone} live={!closed} />
          {state.text}
        </span>
        <span className="hero-id mono">{incident.incident_id}</span>
        <Tag tone={severityTone(incident.severity)}>{incident.severity}</Tag>
      </div>

      <h1 className="hero-title">{incident.title}</h1>
      <p className="hero-why">
        {rc
          ? `${rc.most_likely_root_cause}. ${Math.round(rc.confidence * 100)}% confidence.`
          : 'The agents are still establishing what is wrong.'}
      </p>

      <div className="hero-money">
        <div className="hero-money-main">
          <span className="hero-k">
            {closed ? "This incident's revenue at risk" : 'Revenue at risk, this incident'}
          </span>
          <b className="hero-v">
            {atRisk !== null ? formatINR(atRisk) : formatINR(rate)}
            {atRisk === null ? <i>/hour</i> : null}
          </b>
          <span className="hero-sub">
            {atRisk !== null
              ? `over ${formatDuration(duration)} · ${formatINR(rate)}/hour while it was open`
              : 'still accruing — the total is fixed when the incident closes'}
          </span>
        </div>
        {rev?.measurable ? <RevenueBar revenue={rev} /> : null}
      </div>

      <div className="hero-stats">
        <Stat k="Recovered" v={formatINR(rev?.revenue_recovered_paise)} hint="failed payments completed" />
        <Stat k="Protected" v={formatINR(rev?.revenue_protected_paise)} hint="further loss prevented" />
        <Stat k="Recovery" v={rev?.measurable ? formatPct(rev.recovery_rate, 0) : '—'} hint="of what was at risk" />
        <Stat k="Duration" v={formatDuration(duration)} hint={closed ? 'open to closed' : 'and counting'} />
        <Stat
          k="Payments rescued"
          v={incident.order_recovery ? incident.order_recovery.recovered.toLocaleString() : '—'}
          hint={incident.order_recovery ? `of ${incident.order_recovery.recoverable_payments.toLocaleString()} recoverable` : ''}
        />
      </div>
    </section>
  )
}

function Stat({ k, v, hint }) {
  return (
    <div className="hero-stat">
      <span>{k}</span>
      <b>{v ?? '—'}</b>
      {hint ? <i>{hint}</i> : null}
    </div>
  )
}

/* --------------------------------------------------------- recommendation */

/** The business decision, which is what this product is for.
 *
 *  Deliberately the largest thing on the page after the money. It answers what the agent proposes,
 *  what it expects that to be worth, how sure it is, what it is risking, what history says — and
 *  then, without being asked, what happens if it has got this wrong. An operator being asked to
 *  authorise a change to a live payment system is entitled to the downside before they click. */
function Recommendation({ incident, trace, busy, awaiting, onApprove, onReject }) {
  const p = incident.proposal
  const pd = incident.policy_decision
  const plan = incident.recovery_plan
  if (!p) {
    return (
      <Card title="Recommended recovery">
        <p className="muted">The agent has not finished choosing a recovery yet.</p>
      </Card>
    )
  }

  const chosen = pickStrategy(plan, p)
  const support = chosen?.historical_support
  const stats = support?.stats
  const exposure = trace?.stages?.find((s) => s.key === 'exposure')
  const executed = incident.action_result
  const clamped = pd && JSON.stringify(pd.requested_parameters) !== JSON.stringify(pd.granted_parameters)

  return (
    <section className="rec-panel">
      <header className="rec-head">
        <span className="rec-eyebrow">Recommended recovery</span>
        {executed ? <Tag tone="ok">executed</Tag> : awaiting ? <Tag tone="warn">needs your approval</Tag> : null}
        <span className="rec-guardrail">AI proposes · policy decides · the system verifies</span>
      </header>

      <h2 className="rec-action">{actionSentence(p, pd)}</h2>

      <div className="rec-figures">
        <div>
          <span>Expected revenue recovery</span>
          <b>{formatINR(p.expected_revenue_protected_per_hour_paise)}<i>/hour</i></b>
        </div>
        <div>
          <span>Confidence in the cause</span>
          <b>{formatPct(p.confidence, 0)}</b>
        </div>
        <div>
          <span>Risk</span>
          <b className={riskClass(p.risk_score)}>{riskWord(p.risk_score)}</b>
          <i>score {p.risk_score}</i>
        </div>
        <div>
          <span>Historical success</span>
          <b>
            {stats && support.matched_incidents
              ? `${stats.helped} / ${stats.attempts}`
              : '—'}
          </b>
          <i>{stats && support.matched_incidents ? 'comparable incidents improved' : 'no comparable history'}</i>
        </div>
      </div>

      <div className="rec-split">
        <div className="rec-col">
          <h3>Why this recovery</h3>
          <ul className="reasons">
            {whyBullets(incident, chosen, stats, support).map((b, i) => <li key={i}>{b}</li>)}
          </ul>
          {plan?.similar_incidents?.length ? (
            <details className="more">
              <summary>
                {plan.similar_incident_count > plan.similar_incidents.length
                  ? `The ${plan.similar_incidents.length} most similar of ${plan.similar_incident_count} comparable incidents`
                  : `The ${plan.similar_incidents.length} comparable incidents behind that`}
              </summary>
              <SimilarIncidents rows={plan.similar_incidents} compact />
            </details>
          ) : null}
        </div>

        <div className="rec-col guard">
          <h3>What happens if this is wrong</h3>
          <p className="rec-guard-lead">
            Autonomy within merchant-defined guardrails. If the recovery does not work, the system
            rolls it back and escalates with the evidence.
          </p>
          {exposure?.reached ? (
            <ul className="reasons">
              <li>{exposure.headline}</li>
              {(exposure.detail || []).map((d, i) => <li key={i}>{d}</li>)}
            </ul>
          ) : (
            <p className="muted">Nothing has been authorised, so nothing is exposed yet.</p>
          )}
        </div>
      </div>

      {pd ? (
        <div className={`rec-policy ${pd.approved ? 'ok' : 'warn'}`}>
          <Tag tone={pd.approved ? 'ok' : pd.requires_human ? 'warn' : 'crit'}>
            {pd.outcome.replace(/_/g, ' ').toLowerCase()}
          </Tag>
          <span>{pd.reason}</span>
          {clamped ? (
            <em>
              Merchant policy reduced it: requested {paramSummary(pd.requested_parameters)}, granted{' '}
              {paramSummary(pd.granted_parameters)}.
            </em>
          ) : null}
        </div>
      ) : null}

      {awaiting ? (
        <div className="rec-actions">
          <button className="btn primary" disabled={busy} onClick={onApprove}>Approve and execute</button>
          <button className="btn danger" disabled={busy} onClick={onReject}>Reject</button>
        </div>
      ) : null}
    </section>
  )
}

/** The bullets under "why this action". Each is read off a recorded field, never composed from a
 *  template with blanks — a reason a merchant cannot check is a reason they will not act on. */
function whyBullets(incident, chosen, stats, support) {
  const out = []
  const rc = incident.root_cause
  const im = incident.impact
  const pd = incident.policy_decision

  if (rc) out.push(`${rc.most_likely_root_cause}, at ${Math.round(rc.confidence * 100)}% confidence.`)
  if (chosen?.reasoning) out.push(chosen.reasoning)
  // The option was priced at one size and policy may have granted another. The headline shows what
  // will actually run; the sentence above describes the option as priced, and without this the two
  // read as a contradiction rather than as a clamp.
  const granted = pd?.granted_parameters?.percentage
  if (granted != null && chosen?.magnitude != null && Math.abs(granted - chosen.magnitude) > 1e-6) {
    out.push(
      `That option was priced at ${chosen.magnitude}%. Merchant policy granted ${granted}%, which is ` +
      `what runs — the expected value above is for the option as priced.`,
    )
  }
  if (im) {
    out.push(
      `${formatINR(im.revenue_at_risk_per_hour_paise)} an hour is at risk, from about ` +
      `${im.transactions_at_risk_per_hour.toLocaleString()} extra failed payments affecting ` +
      `${im.affected_customers_estimate.toLocaleString()} customers an hour.`,
    )
  }
  if (stats && support?.matched_incidents) {
    out.push(
      `${stats.helped} of ${stats.attempts} comparable incidents improved with this action` +
      (support.magnitude_matched ? ' at this size' : '') +
      (stats.median_recovery_s ? `, median recovery ${Math.round(stats.median_recovery_s / 60 * 10) / 10} min` : '') +
      '.',
    )
  }
  if (pd?.approved) {
    out.push(`Merchant policy permits it: ${pd.reason}.`)
  }
  return out
}

/* --------------------------------------------------------- strategy table */

/** Every option the agent priced, not just the one it picked.
 *
 *  Showing only the chosen action asks for trust. Showing the alternatives it was chosen over,
 *  with what each was worth and what history says about it, offers a reason instead. */
function StrategyTable({ incident }) {
  const plan = incident.recovery_plan
  const rows = plan?.strategies || []
  if (!rows.length) return null
  const chosen = pickStrategy(plan, incident.proposal)
  const sorted = [...rows].sort((a, b) => b.expected_value_paise - a.expected_value_paise)

  return (
    <Card
      title="Every recovery it considered"
      sub={`${rows.length} priced · selected highlighted`}
    >
      <div className="strat">
        <div className="strat-head">
          <span>Strategy</span>
          <span>Expected value</span>
          <span>Protects/hour</span>
          <span>Risk</span>
          <span>Historical success</span>
        </div>
        {sorted.map((s) => {
          const st = s.historical_support?.stats
          const n = s.historical_support?.matched_incidents || 0
          const selected = chosen && s.strategy_id === chosen.strategy_id
          return (
            <div className={`strat-row ${selected ? 'on' : ''}`} key={s.strategy_id}>
              <span className="strat-name">
                {selected ? <Tag tone="ok">selected</Tag> : null}
                <b>{readableAction(s.action)}{s.magnitude != null ? ` ${s.magnitude}%` : ''}</b>
                {s.target && s.target !== 'none' ? <i className="src">{s.target}</i> : null}
              </span>
              <span className="strat-n">{formatINR(s.expected_value_paise)}</span>
              <span className="strat-n dim">{formatINR(s.expected_revenue_protected_per_hour_paise)}</span>
              <span className={`strat-n ${riskClass(s.risk_score)}`}>{riskWord(s.risk_score)}</span>
              <span className="strat-n dim">
                {n ? `${st.helped}/${st.attempts} · ${formatPct(st.helped_rate, 0)}` : 'no history'}
              </span>
            </div>
          )
        })}
      </div>
      <p className="wf-note" style={{ marginTop: 12 }}>
        The objective is to <b>maximise recovered revenue within the merchant's risk limits</b> —
        not to maximise success rate. Expected value is revenue recovered weighted by the
        probability the recovery works, less what it risks if it backfires. History adjusts that
        probability within a hard bound; it cannot create an option, change a measured figure, or
        reach the policy gateway.
      </p>
    </Card>
  )
}

/* ------------------------------------------------------------ verification */

/** The experiment, which is the strongest thing this system does.
 *
 *  A badge saying "verified" is worth nothing. What is worth something is the two numbers side by
 *  side: the traffic that was moved, and the traffic that was deliberately left alone to live
 *  through the same hour. That comparison is why the agent can tell "my action hurt" from "the
 *  incident got worse", and it deserves the space. */
function VerificationPanel({ incident }) {
  const v = incident.verification
  if (!v) {
    if (!incident.action_result) return null
    return (
      <Card title="Recovery verification">
        <p className="muted">
          Waiting on post-intervention traffic. Recovery is verified before it is counted.
        </p>
      </Card>
    )
  }

  const verdict = VERDICTS[v.status] || VERDICTS.INCONCLUSIVE
  const diff =
    v.treated_success_rate != null && v.control_success_rate != null
      ? v.treated_success_rate - v.control_success_rate
      : v.improvement

  return (
    <section className={`verify ${verdict.tone}`}>
      <header className="verify-head">
        <span className="verify-eyebrow">Was the revenue actually recovered?</span>
        <Tag tone={verdict.tone}>{verdict.label}</Tag>
      </header>

      {v.control_used ? (
        <>
          <div className="verify-ab">
            <div className="ab control">
              <span className="ab-k">Control · left alone</span>
              <b className="ab-v">{formatPct(v.control_success_rate)}</b>
              <span className="ab-n">{v.control_sample.toLocaleString()} payments</span>
            </div>
            <div className="ab-delta">
              <b className={diff >= 0 ? 'up' : 'down'}>
                {diff >= 0 ? '+' : ''}{(diff * 100).toFixed(1)}
              </b>
              <span>percentage points</span>
              <i>p = {v.p_value}</i>
            </div>
            <div className="ab treatment">
              <span className="ab-k">Treatment · moved</span>
              <b className="ab-v">{formatPct(v.treated_success_rate)}</b>
              <span className="ab-n">{v.treated_sample.toLocaleString()} payments</span>
            </div>
          </div>
          <p className="verify-note">
            <b>We do not claim recovery just because payment success improved.</b> The control group
            is traffic the agent deliberately did not move, living through the same ramp and the
            same hour. Comparing against it is what stops the incident recovering on its own from
            being mistaken for the recovery working — recovery is verified before it is counted.
          </p>
        </>
      ) : (
        <div className="verify-ab single">
          <div className="ab">
            <span className="ab-k">Before</span>
            <b className="ab-v">{formatPct(v.before_success_rate)}</b>
            <span className="ab-n">{v.before_sample.toLocaleString()} payments</span>
          </div>
          <div className="ab-delta">
            <b className={v.improvement >= 0 ? 'up' : 'down'}>
              {v.improvement >= 0 ? '+' : ''}{(v.improvement * 100).toFixed(1)}
            </b>
            <span>percentage points</span>
            <i>p = {v.p_value}</i>
          </div>
          <div className="ab">
            <span className="ab-k">After</span>
            <b className="ab-v">{formatPct(v.after_success_rate)}</b>
            <span className="ab-n">{v.after_sample.toLocaleString()} payments</span>
          </div>
        </div>
      )}

      <div className="verify-verdict">
        <b>{verdict.headline}</b>
        <p>{v.explanation}</p>
        {v.estimated_revenue_protected_per_hour_paise ? (
          <div className="verify-money">
            <span>Revenue recovered</span>
            <b>{formatINR(v.estimated_revenue_protected_per_hour_paise)}<i>/hour</i></b>
          </div>
        ) : null}
        {v.side_effects?.length ? (
          <ul className="reasons warn">
            {v.side_effects.map((s, i) => <li key={i}>{s}</li>)}
          </ul>
        ) : null}
      </div>
    </section>
  )
}

const VERDICTS = {
  RECOVERED: { label: 'recovered', tone: 'ok', headline: 'Recovery verified against a control group.' },
  PARTIALLY_RECOVERED: { label: 'partially recovered', tone: 'warn', headline: 'Real improvement, but not the whole gap — the incident stayed open.' },
  FAILED: { label: 'failed', tone: 'crit', headline: 'The intervention did not work, so it was undone.' },
  REGRESSED: { label: 'regressed', tone: 'crit', headline: 'The intervention made things worse, so it was rolled back.' },
  INCONCLUSIVE: { label: 'inconclusive', tone: 'mute', headline: 'Not enough traffic yet to judge — the incident stayed open rather than guessing.' },
}

/* ------------------------------------------------------------- the money */

function RevenueOutcome({ incident }) {
  const rev = incident.revenue
  const orders = incident.order_recovery
  if (!rev?.measurable) return null

  return (
    <Card title="Revenue actually recovered" sub="every figure is arithmetic over recorded measurements">
      <RevenueBar revenue={rev} />
      <div className="facts" style={{ marginTop: 16 }}>
        <div className="fact"><span>At risk</span><b>{formatINR(rev.revenue_at_risk_paise)}</b></div>
        <div className="fact"><span>Protected</span><b>{formatINR(rev.revenue_protected_paise)}</b></div>
        <div className="fact"><span>Recovered</span><b>{formatINR(rev.revenue_recovered_paise)}</b></div>
        <div className="fact"><span>Lost</span><b>{formatINR(rev.revenue_lost_paise)}</b></div>
        <div className="fact"><span>Recovery rate</span><b>{formatPct(rev.recovery_rate, 0)}</b></div>
      </div>
      {orders?.note ? (
        <p className="wf-note" style={{ marginTop: 14 }}>
          <b>Failed payments: </b>{orders.note}
        </p>
      ) : null}
      <details className="more">
        <summary>The arithmetic, line by line</summary>
        <ol className="calc">{rev.calculation.map((l, i) => <li key={i}>{l}</li>)}</ol>
      </details>
    </Card>
  )
}

/* ------------------------------------------------------------- learning */

function LearnedFromThis({ incident, onGoto }) {
  const l = incident.learning
  const plan = incident.recovery_plan
  if (!l) return null
  const size = l.intervention_magnitude ? ` at ${l.intervention_magnitude}%` : ''
  const support = pickStrategy(plan, incident.proposal)?.historical_support
  const stats = support?.stats

  return (
    <Card title="What the agent learned" sub="historical evidence for the next recovery">
      <div className="learn">
        <div className="learn-row">
          <span className="learn-k">Recorded</span>
          <span>
            <b>{readableAction(l.actual_action_executed)}{size}</b> under this failure pattern →{' '}
            <b>{(l.verification_result || '').replace(/_/g, ' ').toLowerCase()}</b>
            {l.rollback_required ? ', and had to be rolled back' : ''}.
          </span>
        </div>
        <div className="learn-row">
          <span className="learn-k">Against signature</span>
          <code>{signatureLabel(l.failure_signature)}</code>
        </div>
        {stats && support.matched_incidents ? (
          <div className="learn-row">
            <span className="learn-k">Now stands at</span>
            <span>
              {stats.helped} of {stats.attempts} comparable incidents improved with this action
              {stats.rollbacks ? `, ${stats.rollbacks} rolled back` : ''}.
            </span>
          </div>
        ) : null}
        <p className="learn-forward">
          This improves future recovery recommendations: it moves the efficacy prior behind every
          option of this kind, inside a hard bound. It cannot authorise anything — the policy
          gateway decides what may run, and history is evidence rather than authority.
        </p>
        {onGoto ? (
          <button className="btn sm" onClick={() => onGoto('learning')}>See the learned playbook</button>
        ) : null}
      </div>
    </Card>
  )
}

/* ------------------------------------------------------------ prevention */

/** The last question: how do we stop having this incident at all. */
function PreventionOpportunity({ incident, learning, onGoto }) {
  const key = incident.learning?.failure_signature
    ? signatureKeyOf(incident.learning.failure_signature)
    : null
  const all = learning?.prevention || []
  const match = key ? all.find((r) => r.signature_key === key) : null
  const rec = match || (incident.state === 'CLOSED' ? all[0] : null)
  if (!rec) return null

  return (
    <Card title="Prevention opportunity" sub="advisory — merchant policy is unchanged">
      <PreventionCard recommendation={rec} busy onDecide={() => {}} readOnly />
      {onGoto ? (
        <button className="btn sm" style={{ marginTop: 12 }} onClick={() => onGoto('learning')}>
          Review it on the Learning page
        </button>
      ) : null}
    </Card>
  )
}

/* -------------------------------------------------------------- handover */

function Handover({ incident, busy, onStep }) {
  const e = incident.escalation
  return (
    <Card title="Handed to a human" right={<Tag tone="warn">{e.urgency}</Tag>}>
      <p className="handover-why">{e.because || e.reason}</p>
      <p className="handover-rec">{e.recommended_human_action}</p>
      {onStep ? <NextSteps incident={incident} busy={busy} onStep={onStep} /> : null}
    </Card>
  )
}

/* ----------------------------------------------------------- agent trace */

/** The pipeline, as supporting evidence.
 *
 *  Collapsed by default and last on the page. It is not less true than the story above it, but a
 *  merchant reads it only when they have decided to check something — and putting a stage
 *  checklist first made the product look like an observability tool rather than an operator. */
function AgentTraceSection({ incident, trace, agents }) {
  const [open, setOpen] = useState(false)
  const stages = trace?.stages || []
  const done = stages.filter((s) => s.reached).length

  return (
    <section className="tracewrap">
      <button className="tracewrap-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className="tracewrap-title">Agent trace — the technical proof</span>
        <span className="tracewrap-sum">
          {stages.length ? `${done} of ${stages.length} stages reached` : 'loading'}
        </span>
        <span className="tracewrap-chev">{open ? '▾' : '▸'}</span>
      </button>

      {!open && stages.length ? (
        <ol className="checklist">
          {stages.map((s) => (
            <li key={s.key} className={s.reached ? 'done' : ''}>
              <span className="check">{s.reached ? '✓' : '·'}</span>
              {s.title}
            </li>
          ))}
        </ol>
      ) : null}

      {open ? (
        <div className="tracewrap-body">
          <p className="wf-note">
            Every stage can be opened to see the evidence underneath it — the findings and the tool
            that produced each, the hypotheses with what argues against them, the incidents this was
            weighed against, every option that was priced, and every policy rule the gateway
            evaluated.
          </p>
          <DecisionTrace trace={trace} loading />
          <details className="more">
            <summary>{`Raw step sequence — ${(incident.steps || []).length} agent runs, repeats and all`}</summary>
            <AgentRuns incident={incident} agents={agents} />
          </details>
        </div>
      ) : null}
    </section>
  )
}

function AgentRuns({ incident, agents }) {
  const steps = incident.steps || []
  return (
    <div className="rows">
      {steps.map((s, i) => (
        <div className="row wide" key={s.step_id || i}>
          <span className="row-id">{s.state.replace(/_/g, ' ').toLowerCase()}</span>
          <Tag tone={s.ok ? 'ok' : 'crit'}>{s.ok ? 'ok' : 'failed'}</Tag>
          <span className="row-main">
            <b>{s.agent.replace(/_/g, ' ')}</b>
            <span className="row-sub">{s.summary || s.error}</span>
          </span>
          <span className="row-num dim">{s.tool_calls?.length || 0} tools</span>
          <span className="row-num dim">{Math.round(s.latency_ms)} ms</span>
        </div>
      ))}
      {!steps.length ? <p className="muted">{(agents || []).length} agents, none run yet.</p> : null}
    </div>
  )
}

/* --------------------------------------------------------------- helpers */

/** The strategy the proposal corresponds to, matched on action and size.
 *
 *  Matched rather than stored: `ActionProposal` carries the action the reasoner chose, and the
 *  plan carries the priced options, and the join between them is the action plus its magnitude. */
export function pickStrategy(plan, proposal) {
  if (!plan?.strategies?.length || !proposal) return null
  const pct = proposal.parameters?.percentage
  const same = plan.strategies.filter((s) => s.action === proposal.action)
  if (!same.length) return null
  if (pct == null) return same[0]
  return (
    same.find((s) => s.magnitude != null && Math.abs(s.magnitude - Number(pct)) < 1e-6) || same[0]
  )
}

function actionSentence(p, pd) {
  const params = (pd?.granted_parameters && Object.keys(pd.granted_parameters).length
    ? pd.granted_parameters
    : p.parameters) || {}
  if (p.action === 'shift_traffic') {
    const scope = params.payment_method ? ` of ${params.payment_method}` : ''
    return `Shift ${params.percentage}%${scope} traffic from ${params.from_route} to ${params.to_route}`
  }
  if (p.action === 'no_action') return 'Take no action'
  return `${readableAction(p.action)}${paramSummary(params) ? ` · ${paramSummary(params)}` : ''}`
}

function paramSummary(params) {
  return Object.entries(params || {})
    .filter(([, v]) => v !== null && v !== undefined && String(v).length <= 32)
    .slice(0, 3)
    .map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`)
    .join(', ')
}

function riskWord(score) {
  if (score == null) return '—'
  if (score <= 0.15) return 'Minimal'
  if (score <= 0.3) return 'Low'
  if (score <= 0.55) return 'Medium'
  return 'High'
}

function riskClass(score) {
  if (score == null) return ''
  if (score <= 0.3) return 'risk-low'
  if (score <= 0.55) return 'risk-med'
  return 'risk-high'
}

const severityTone = (s) =>
  ({ CRITICAL: 'crit', HIGH: 'elevated', MEDIUM: 'warn', LOW: 'mute' })[s] || 'mute'

/** How long the incident has been open, on the *simulated* clock.
 *
 *  `opened_at` is simulated time. Diffing it against the browser's wall clock reported a
 *  minutes-old incident as 258 hours, because the simulated world starts on a fixed date. `now` is
 *  the simulated timestamp the metrics endpoint reports; without it there is no honest answer for
 *  an open incident, so the duration is withheld rather than guessed. */
function elapsed(incident, now) {
  const start = new Date(incident.opened_at).getTime()
  const end = incident.closed_at
    ? new Date(incident.closed_at).getTime()
    : now
      ? new Date(now).getTime()
      : null
  if (end === null) return null
  return Math.max(0, (end - start) / 1000)
}

export function signatureLabel(signature) {
  if (!signature) return 'unclassified'
  const parts = [
    signature.payment_method,
    signature.psp || signature.issuer || signature.gateway,
    signature.dominant_error_code,
  ].filter(Boolean)
  return parts.length ? parts.join(' · ') : signature.root_cause_id || 'unclassified'
}

/** Mirrors `FailureSignature.key()`, so an incident's learning record can be matched to the
 *  prevention recommendation mined from the same signature. */
function signatureKeyOf(s) {
  return [
    s.merchant_id || '',
    s.root_cause_id || '',
    s.payment_method || '-',
    s.psp || '-',
    s.dominant_error_code || '-',
  ].join('|')
}

export { formatFact }
