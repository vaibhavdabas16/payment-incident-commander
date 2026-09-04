import { Fragment } from 'react'
import { formatClock, formatFact, formatINR, formatPct } from './api.js'

/* Shared primitives. Everything here renders data the agents actually recorded — there is no
 * placeholder content, and anything the system has not produced yet renders as an empty or
 * loading state rather than as an invented value. */

/* ------------------------------------------------------------------ atoms */

export function Led({ tone = '', live = false }) {
  return <span className={`led ${tone} ${live ? 'live' : ''}`} />
}

export function Tag({ tone = 'mute', children }) {
  return <span className={`tag ${tone}`}>{children}</span>
}

export const severityTone = (severity) =>
  ({ CRITICAL: 'crit', HIGH: 'elevated', MEDIUM: 'warn', LOW: 'mute' })[severity] || 'mute'

export const statusTone = (status) =>
  ({ healthy: 'ok', watch: 'warn', degraded: 'elevated', critical: 'crit' })[status] || 'mute'

export function Card({ title, sub, right, children, flush = false }) {
  return (
    <section className="card">
      {title ? (
        <div className="card-head">
          <h3>{title}</h3>
          {right || (sub ? <span className="sub">{sub}</span> : null)}
        </div>
      ) : null}
      <div className={`card-body ${flush ? 'flush' : ''}`}>{children}</div>
    </section>
  )
}

/* ------------------------------------------------------------------ states */

export function Empty({ title, body, facts }) {
  return (
    <div className="empty">
      <div className="empty-mark">✓</div>
      <h3>{title}</h3>
      <p>{body}</p>
      {facts?.length ? (
        <div className="empty-facts">
          {facts.map((f) => (
            <span className="empty-fact" key={f.label}>
              <Led tone={f.tone || 'ok'} live={f.live} />
              {f.label}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  )
}

export function Skeleton({ rows = 3 }) {
  return (
    <div aria-hidden="true">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skel skel-line" style={{ width: `${92 - i * 14}%` }} />
      ))}
    </div>
  )
}

export function ErrorBox({ children }) {
  return (
    <div className="err" role="alert">
      <b>Connection problem</b>
      {children}
    </div>
  )
}

/* ------------------------------------------------------------- health bars */

export function HealthBars({ rows }) {
  if (!rows?.length) return <Skeleton rows={4} />
  return (
    <div className="health">
      {rows.map((r) => (
        <div className="health-row" key={r.segment}>
          <span className="health-name">
            <Led tone={statusTone(r.status)} />
            {r.segment}
          </span>
          <span className="health-track">
            <span
              className={`health-fill ${r.status === 'healthy' ? 'ok' : r.status}`}
              style={{ width: `${Math.max(3, Math.min(100, r.success_rate * 100))}%` }}
            />
          </span>
          <span className="health-v">{formatPct(r.success_rate)}</span>
          <span className="health-n">{r.transactions.toLocaleString()} txn</span>
        </div>
      ))}
    </div>
  )
}

/* ------------------------------------------------------------- agent trace */

/** What each agent did on this incident, in the order the supervisor ran them. Built from the
 *  recorded steps, so an agent that has not run yet shows as waiting rather than as fabricated
 *  progress. */
export function AgentTrace({ incident, agents }) {
  const steps = incident?.steps || []
  const byAgent = new Map()
  steps.forEach((s) => byAgent.set(s.agent, s))
  const order = (agents?.length ? agents : []).map((a) => a.name)
  const names = order.length ? order : Array.from(byAgent.keys())

  return (
    <div className="trace">
      {names.map((name) => {
        const step = byAgent.get(name)
        const spec = agents?.find((a) => a.name === name)
        const status = !step ? 'wait' : step.ok ? 'done' : 'failed'
        return (
          <div className={`trace-row ${status}`} key={name}>
            <div className="trace-rail">
              <span className="trace-dot" />
              <span className="trace-line" />
            </div>
            <div>
              <div className="trace-name">
                {name.replace(/_/g, ' ')}
                {spec?.writes ? <Tag tone="elevated">write · gated</Tag> : null}
                {status === 'done' ? <Tag tone="ok">done</Tag> : null}
                {status === 'failed' ? <Tag tone="crit">failed</Tag> : null}
                {status === 'wait' ? <Tag tone="mute">waiting</Tag> : null}
              </div>
              <div className="trace-task">{step?.summary || spec?.summary || '—'}</div>
              {step ? (
                <div className="trace-meta">
                  {step.tool_calls?.length ? `${step.tool_calls.length} tool calls · ` : ''}
                  {step.latency_ms != null ? `${Math.round(step.latency_ms)} ms` : ''}
                </div>
              ) : null}
            </div>
          </div>
        )
      })}
    </div>
  )
}

/* ---------------------------------------------------------------- timeline */

export function Timeline({ incident }) {
  const steps = incident?.steps || []
  if (!steps.length) return <Skeleton rows={4} />
  return (
    <div className="tl">
      {steps.map((s, i) => (
        <div className={`tl-item ${s.ok ? 'done' : 'failed'}`} key={s.step_id || i}>
          <div className="tl-time">{formatClock(s.started_at)}</div>
          <div className="tl-title">
            {s.agent.replace(/_/g, ' ')}
            <Tag tone={s.ok ? 'ok' : 'crit'}>{s.state.replace(/_/g, ' ').toLowerCase()}</Tag>
          </div>
          <div className="tl-body">{s.summary}</div>
          {s.tool_calls?.length ? <ToolCalls calls={s.tool_calls} /> : null}
        </div>
      ))}
    </div>
  )
}

/** Distinct tools with a count, rather than the raw call list: a residual test runs once per
 *  candidate segment, so the unabridged list is the same name a dozen times and reads as noise. */
function ToolCalls({ calls }) {
  const counts = new Map()
  calls.forEach((c) => counts.set(c.tool, (counts.get(c.tool) || 0) + 1))
  const shown = Array.from(counts.entries()).slice(0, 8)
  const hidden = counts.size - shown.length
  return (
    <div className="tl-meta">
      {shown.map(([tool, n]) => (n > 1 ? `${tool} ×${n}` : tool)).join(' · ')}
      {hidden > 0 ? ` · +${hidden} more` : ''}
    </div>
  )
}

/* -------------------------------------------------------------- confidence */

export function Hypotheses({ rootCause }) {
  const list = rootCause?.hypotheses || []
  if (!list.length) return <Skeleton rows={3} />
  return (
    <div className="conf">
      {list.slice(0, 5).map((h, i) => (
        <div className={`conf-row ${i === 0 ? 'top' : ''}`} key={h.cause_id}>
          <span className="conf-label">{h.cause}</span>
          <span className="conf-track">
            <span className="conf-fill" style={{ width: `${Math.round((h.probability || 0) * 100)}%` }} />
          </span>
          <span className="conf-v">{formatPct(h.probability, 0)}</span>
        </div>
      ))}
    </div>
  )
}

/* ------------------------------------------------------------------ trace */

/** The whole incident, stage by stage, in the order the supervisor ran it.
 *
 *  Rendered from `/api/incidents/{id}/trace` rather than assembled here. The trace is defined once,
 *  in Python, next to the objects it describes; a second definition in JavaScript would drift, and
 *  the half that drifted would be the half a person is actually reading.
 *
 *  Every stage is either reached or not, carries the facts the agents recorded, and can be opened
 *  to see the evidence underneath. Nothing is summarised into a sentence with no working behind it.
 */
export function DecisionTrace({ trace, loading = false }) {
  if (!trace) return loading ? <Skeleton rows={7} /> : null
  const stages = trace.stages || []
  const firstPending = stages.findIndex((x) => !x.reached)
  const running = trace.state !== 'CLOSED' ? firstPending : -1

  return (
    <ol className="wf">
      {stages.map((stage, i) => {
        const state = !stage.reached
          ? i === running ? 'running' : 'todo'
          : stage.tone === 'warn' ? 'blocked'
          : stage.tone === 'alert' ? 'failed'
          : 'done'
        const facts = (stage.facts || []).filter((f) => f.value !== null && f.value !== undefined && f.value !== '')
        return (
          <li className={`wf-step ${state}`} key={stage.key}>
            <span className="wf-dot">
              {state === 'done' ? '✓' : state === 'failed' ? '!' : state === 'blocked' ? '!' : i + 1}
            </span>
            <div className="wf-main">
              <div className="wf-title">
                {stage.title}
                {state === 'running' ? <Tag tone="agent">working</Tag> : null}
                {state === 'todo' ? <Tag tone="mute">not reached</Tag> : null}
              </div>
              <div className="wf-lead">{stage.headline}</div>
              {facts.length ? (
                <div className="facts">
                  {facts.map((f) => (
                    <div className="fact" key={f.label}>
                      <span>{f.label}</span>
                      <b>{formatFact(f.value, f.unit)}</b>
                    </div>
                  ))}
                </div>
              ) : null}
              {stage.evidence?.length ? (
                <div className="wf-meta">{stage.evidence.slice(0, 10).join(' · ')}
                  {stage.evidence.length > 10 ? ` · +${stage.evidence.length - 10} more` : ''}
                </div>
              ) : null}
              <StageDetail stage={stage} />
            </div>
          </li>
        )
      })}
    </ol>
  )
}

/** The working behind one stage, in the shape that stage's evidence actually has. */
function StageDetail({ stage }) {
  const detail = stage.detail
  if (!detail || (Array.isArray(detail) && !detail.length)) return null
  const label = DETAIL_LABEL[stage.key] || 'Show the working'
  return (
    <details className="more">
      <summary>{label}</summary>
      <StageBody stage={stage} detail={detail} />
    </details>
  )
}

const DETAIL_LABEL = {
  evidence: 'Every finding, and the tool that produced it',
  hypothesis: 'Every hypothesis considered, for and against',
  history: 'The comparable incidents this was decided against',
  strategies: 'Every option that was priced',
  decision: 'The options not chosen',
  policy: 'Every rule the gateway evaluated',
  revenue: 'The arithmetic, line by line',
  learning: 'The record written to memory',
}

function StageBody({ stage, detail }) {
  if (stage.key === 'evidence') {
    return (
      <ul className="wf-list">
        {detail.map((f) => (
          <li key={f.id}><code>{f.id}</code>{f.statement}<i className="src">{f.tool}</i></li>
        ))}
      </ul>
    )
  }
  if (stage.key === 'hypothesis') {
    return (
      <div className="conf">
        {detail.map((h, i) => (
          <div className={`conf-row ${i === 0 ? 'top' : ''}`} key={h.cause_id}>
            <span className="conf-label">
              {h.cause}
              {h.memory_adjustment ? (
                <i className="src">memory {h.memory_adjustment > 0 ? '+' : ''}{h.memory_adjustment}</i>
              ) : null}
            </span>
            <span className="conf-track">
              <span className="conf-fill" style={{ width: `${Math.round((h.probability || 0) * 100)}%` }} />
            </span>
            <span className="conf-v">{formatPct(h.probability, 0)}</span>
          </div>
        ))}
      </div>
    )
  }
  if (stage.key === 'history') return <SimilarIncidents rows={detail} />
  if (stage.key === 'strategies') return <Strategies rows={detail} />
  if (stage.key === 'policy') {
    return (
      <ul className="wf-list">
        {detail.map((r) => (
          <li key={r.rule}><code>{r.outcome.toLowerCase().replace(/_/g, ' ')}</code>{r.reason}<i className="src">{r.rule}</i></li>
        ))}
      </ul>
    )
  }
  if (Array.isArray(detail) && detail.every((x) => typeof x === 'string')) {
    return <ol className="calc">{detail.map((line, i) => <li key={i}>{line}</li>)}</ol>
  }
  return <KeyValues data={detail} />
}

/** Past incidents, with what was tried and what it got. This is the historical evidence itself —
 *  a claim about history should be one click from the incidents that make it true. */
export function SimilarIncidents({ rows, compact = false }) {
  if (!rows?.length) return <p className="wf-note">No comparable incidents in memory.</p>
  return (
    <div className="rows">
      {rows.map((r) => (
        <div className="row" key={r.incident_id}>
          <span className="row-id">{r.incident_id}</span>
          <Tag tone={r.succeeded ? 'ok' : r.rollback_required ? 'crit' : 'warn'}>
            {(r.verification_result || r.outcome || '').replace(/_/g, ' ').toLowerCase()}
          </Tag>
          <span className="row-main">
            {readableAction(r.action_taken)}
            {r.magnitude ? ` ${r.magnitude}%` : ''}
            {compact ? '' : ` · ${r.matched_on}`}
          </span>
          <span className="row-num">{formatINR(r.revenue_protected_paise + r.revenue_recovered_paise)}</span>
          <span className="row-num dim">{Math.round((r.similarity || 0) * 100)}% match</span>
        </div>
      ))}
    </div>
  )
}

/** Every option the system priced, best first, with the history behind each one. */
export function Strategies({ rows }) {
  if (!rows?.length) return null
  return (
    <div className="rows">
      {rows.map((r) => {
        const h = r.historical_support
        return (
          <div className="row wide" key={r.strategy_id}>
            <span className="row-id">{r.strategy_id}</span>
            <span className="row-main">
              <b>{readableAction(r.action)}{r.magnitude != null ? ` ${r.magnitude}%` : ''}</b>
              {r.target && r.target !== 'none' ? <i className="src">{r.target}</i> : null}
              <span className="row-sub">{r.reasoning}</span>
              {h && h.matched_incidents ? (
                <span className="row-sub hist">
                  History: {h.stats.helped}/{h.stats.attempts} comparable incidents improved
                  {h.stats.rollbacks ? `, ${h.stats.rollbacks} rolled back` : ''}
                  {' · '}prior {h.efficacy_adjustment > 0 ? '+' : ''}{h.efficacy_adjustment}
                </span>
              ) : null}
            </span>
            <span className="row-num">{formatINR(r.expected_value_paise)}</span>
            <span className="row-num dim">risk {r.risk}</span>
          </div>
        )
      })}
    </div>
  )
}

function KeyValues({ data }) {
  const entries = Array.isArray(data)
    ? data.map((v, i) => [String(i + 1), v])
    : Object.entries(data || {})
  return (
    <dl className="kv">
      {entries.slice(0, 24).map(([k, v]) => (
        <Fragment key={k}>
          <dt>{k.replace(/_/g, ' ')}</dt>
          <dd>{typeof v === 'object' && v !== null ? JSON.stringify(v).slice(0, 160) : formatFact(v)}</dd>
        </Fragment>
      ))}
    </dl>
  )
}

/* ------------------------------------------------------------- money */

/** The money, which is the story. Four figures that add up, and say so. */
export function RevenueBar({ revenue, compact = false }) {
  if (!revenue || !revenue.revenue_at_risk_paise) return null
  const risk = revenue.revenue_at_risk_paise
  const pct = (v) => `${Math.max(0, Math.min(100, (v / risk) * 100))}%`
  return (
    <div className={`money ${compact ? 'compact' : ''}`}>
      <div className="money-bar" role="img"
           aria-label={`${formatINR(revenue.revenue_protected_paise)} protected, ${formatINR(revenue.revenue_recovered_paise)} recovered, ${formatINR(revenue.revenue_lost_paise)} lost`}>
        <span className="seg protected" style={{ width: pct(revenue.revenue_protected_paise) }} />
        <span className="seg recovered" style={{ width: pct(revenue.revenue_recovered_paise) }} />
        <span className="seg lost" style={{ width: pct(revenue.revenue_lost_paise) }} />
      </div>
      <div className="money-legend">
        <span><i className="dot protected" />Protected <b>{formatINR(revenue.revenue_protected_paise)}</b></span>
        <span><i className="dot recovered" />Recovered <b>{formatINR(revenue.revenue_recovered_paise)}</b></span>
        <span><i className="dot lost" />Lost <b>{formatINR(revenue.revenue_lost_paise)}</b></span>
      </div>
    </div>
  )
}

/* -------------------------------------------------------------- learning */

/** Everything the system has learned, newest first. */
export function MemoryTable({ records }) {
  if (!records?.length) {
    return (
      <Empty
        title="Nothing learned yet"
        body="Every incident that closes is priced, reduced to a structured record and stored here — including the ones that went badly. Run an incident from Simulate."
      />
    )
  }
  return (
    <div className="rows">
      {records.map((r) => (
        <div className="row wide" key={r.incident_id}>
          <span className="row-id">{r.incident_id}</span>
          <Tag tone={r.succeeded ? 'ok' : r.rollback_required ? 'crit' : 'warn'}>
            {(r.verification || '').replace(/_/g, ' ').toLowerCase()}
          </Tag>
          <span className="row-main">
            <b>{readableAction(r.action)}{r.magnitude != null ? ` ${r.magnitude}%` : ''}</b>
            <span className="row-sub">{r.signature} · {r.root_cause}</span>
          </span>
          <span className="row-num">{formatPct(r.recovery_rate, 0)}</span>
          <span className="row-num dim">{formatINR(r.revenue_at_risk_paise)} at risk</span>
        </div>
      ))}
    </div>
  )
}

/** How each action has actually performed, which is the thing future decisions are weighed on. */
export function ActionOutcomes({ rows }) {
  if (!rows?.length) return <p className="wf-note">No action has been tried enough times to have a record yet.</p>
  return (
    <div className="rows">
      {rows.map((r) => (
        // Its own three-column grid rather than the five-column `.row`: with three children in a
        // five-column layout the band label landed in the count's column and overprinted it.
        <div className="row outcome" key={`${r.action}-${r.magnitude_band}`}>
          <span className="row-main">
            <b>{readableAction(r.action)}</b>
            {r.magnitude_band !== 'any' ? <i className="src">{r.magnitude_band}</i> : null}
          </span>
          <span className="row-num">{r.helped}/{r.attempts} helped</span>
          <span className="row-num dim">
            {r.successes} fully recovered{r.rollbacks ? ` · ${r.rollbacks} rolled back` : ''}
          </span>
        </div>
      ))}
    </div>
  )
}

/** A recurring failure and the preventive change it argues for.
 *
 *  The authority line is not decoration. Approving one of these records a request; it does not
 *  change the merchant's policy, and the card says so where somebody deciding will read it. */
export function PreventionCard({ recommendation, busy, onDecide, readOnly = false }) {
  const r = recommendation
  const decided = r.status !== 'PROPOSED'
  return (
    <div className={`rec ${decided ? 'decided' : ''}`}>
      <div className="rec-top">
        <Tag tone={r.status === 'ACKNOWLEDGED' ? 'ok' : r.status === 'DISMISSED' ? 'mute' : 'warn'}>
          {r.status.toLowerCase()}
        </Tag>
        <span className="rec-p">{r.occurrences} occurrences · {r.recommendation_id}</span>
      </div>
      <h4>{r.pattern}</h4>
      <ul className="wf-list">
        {r.conditions.map((c) => <li key={c}>when {c}</li>)}
      </ul>
      <div className="facts">
        <div className="fact"><span>Proposed</span><b>{readableAction(r.proposed_action)} {paramText(r.proposed_parameters)}</b></div>
        <div className="fact"><span>Historical loss</span><b>{formatINR(r.historical_revenue_lost_paise)}</b></div>
        <div className="fact"><span>Estimated benefit</span><b>{formatINR(r.estimated_benefit_paise)}</b></div>
      </div>
      <details className="more">
        <summary>The evidence behind this</summary>
        <ul className="wf-list">{r.evidence.map((e, i) => <li key={i}>{e}</li>)}</ul>
      </details>
      <p className="rec-authority">
        Advisory. Applying this means a person editing the merchant policy file — approving here
        records the request and changes nothing.
      </p>
      {readOnly ? null : decided ? (
        <p className="rec-p">{r.status.toLowerCase()} by {r.acknowledged_by}</p>
      ) : (
        <div className="rec-actions">
          <button className="btn primary" disabled={busy} onClick={() => onDecide(r, 'accept')}>
            Record as accepted
          </button>
          <button className="btn" disabled={busy} onClick={() => onDecide(r, 'dismiss')}>Dismiss</button>
        </div>
      )}
    </div>
  )
}

/** The unedited step sequence, repeats and all.
 *
 *  The workflow above is one row per stage, which is what makes it readable. This is what actually
 *  happened, in order: a second proposal after a failed verification appears here as a second
 *  decision, and nowhere else.
 */
export function ActivityLog({ incident }) {
  const steps = incident?.steps || []
  if (!steps.length) return null
  return (
    <details className="more">
      <summary>{`Full activity log \u2014 ${steps.length} steps in the order they ran`}</summary>
      <Timeline incident={incident} />
    </details>
  )
}

/** The moves available on a handed-over incident.
 *
 *  An escalation used to end with advice and a closed incident, which reads as finished when the
 *  work has barely started. These are the same options the API exposes, rendered where the person
 *  who needs them is already looking.
 */
export function NextSteps({ incident, busy, onStep }) {
  const steps = incident?.escalation?.next_steps || []
  if (!steps.length) return null
  return (
    <div className="steps">
      <div className="steps-label">What now</div>
      {steps.map((step) => (
        <div key={step.action} className="step">
          <div className="step-text">
            <b>{step.label}</b>
            <span>{step.detail}</span>
            {step.consequence ? <i>{step.consequence}</i> : null}
          </div>
          <button
            className={`btn ${step.destructive ? 'primary' : ''}`}
            disabled={busy}
            onClick={() => onStep(step)}
          >
            {step.destructive ? 'Run it' : 'Do it'}
          </button>
        </div>
      ))}
    </div>
  )
}

/* ------------------------------------------------------------- formatting */

export function readableAction(action) {
  return action ? action.replace(/_/g, ' ') : 'no action'
}

const PARAM_LIMIT = 40

export function paramText(params) {
  const entries = Object.entries(params || {})
    .filter(([, v]) => v !== null && v !== undefined && String(v).length <= PARAM_LIMIT)
    .slice(0, 3)
  return entries.length ? entries.map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`).join(', ') : ''
}

export function confidenceInWords(c) {
  if (c === null || c === undefined) return 'Confidence not recorded'
  const pct = `${Math.round(c * 100)}%`
  if (c >= 0.7) return `Confident (${pct})`
  if (c >= 0.5) return `Moderately confident (${pct})`
  return `Low confidence (${pct})`
}

/** The headline facts, money first.
 *
 *  A closed incident is a financial outcome and an open one is a financial exposure, so the two
 *  get different rows. Showing "revenue at risk per hour" on an incident that finished twenty
 *  minutes ago describes a threat that no longer exists. */
export function impactFacts(incident) {
  const im = incident?.impact
  const rev = incident?.revenue
  if (rev && rev.measurable) {
    const orders = incident?.order_recovery
    return [
      ['Revenue at risk', formatINR(rev.revenue_at_risk_paise)],
      ['Protected', formatINR(rev.revenue_protected_paise)],
      ['Recovered', formatINR(rev.revenue_recovered_paise)],
      ['Recovery', formatPct(rev.recovery_rate, 0)],
      ...(orders && orders.recovered
        ? [['Payments rescued', orders.recovered.toLocaleString()]]
        : []),
    ]
  }
  if (!im) return []
  return [
    ['Revenue at risk', `${formatINR(im.revenue_at_risk_per_hour_paise)}/hr`],
    ['Extra failures', `${im.transactions_at_risk_per_hour.toLocaleString()}/hr`],
    ['Customers affected', `${im.affected_customers_estimate.toLocaleString()}/hr`],
    ['60-min projection', formatINR(im.projected_loss_if_unmitigated_paise)],
  ]
}
