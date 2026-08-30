import { formatClock, formatINR, formatPct } from './api.js'

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

/** A sparkline drawn by hand: no chart library for a 130px trend. */
export function Sparkline({ points, tone = 'info', width = 150, height = 26 }) {
  const values = (points || []).filter((p) => p.transactions > 0).slice(-30).map((p) => p.success_rate)
  if (values.length < 2) return null
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const span = hi - lo || 1
  const step = width / (values.length - 1)
  const y = (v) => height - 2 - ((v - lo) / span) * (height - 4)
  const d = values.map((v, i) => `${i ? 'L' : 'M'}${(i * step).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const stroke = { ok: 'var(--ok)', crit: 'var(--crit)', info: 'var(--info)' }[tone] || 'var(--info)'
  return (
    <svg className="metric-spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      <path d={d} fill="none" stroke={stroke} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}

export function Metric({ label, value, delta, deltaTone, spark, sparkTone }) {
  return (
    <div className="metric">
      <span className="metric-k">{label}</span>
      <span className="metric-v">{value}</span>
      {delta ? (
        <span className={`metric-d ${deltaTone || ''}`}>
          {deltaTone ? <Led tone={deltaTone === 'up' ? 'ok' : 'crit'} /> : null}
          {delta}
        </span>
      ) : null}
      {spark ? <Sparkline points={spark} tone={sparkTone} /> : null}
    </div>
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

export function Evidence({ incident }) {
  const rc = incident?.root_cause
  const findings = incident?.evidence?.findings || []
  const cited = new Set((rc?.supporting_evidence || []).map(String))
  const supporting = findings.filter((f) => cited.has(String(f.finding_id))).slice(0, 5)
  const rows = supporting.length ? supporting : findings.slice(0, 5)
  if (!rows.length) return <Skeleton rows={3} />
  return (
    <div className="ev">
      {rows.map((f) => (
        <div className="ev-item" key={f.finding_id}>
          <span className="ev-tick">✓</span>
          <span>{f.statement}</span>
        </div>
      ))}
      {(rc?.contradicting_evidence || []).slice(0, 2).map((c, i) => (
        <div className="ev-item against" key={`c${i}`}>
          <span className="ev-tick">✕</span>
          <span>{c}</span>
        </div>
      ))}
    </div>
  )
}

/* --------------------------------------------------------------- feed */

export function EventFeed({ events }) {
  if (!events?.length) return <div className="empty" style={{ padding: '28px 12px' }}><p>Waiting for agent activity…</p></div>
  return (
    <ul className="feed" aria-live="polite" aria-relevant="additions">
      {events.map((e, i) => (
        <li key={i}>
          <span className="f-time">{e._at ? formatClock(e._at) : ''}</span>
          <span className="f-kind">{e.kind}</span>
          <span className="f-text">{describeEvent(e)}</span>
        </li>
      ))}
    </ul>
  )
}

export function describeEvent(e) {
  switch (e.kind) {
    case 'agent_finished': return `${e.agent}: ${e.summary || (e.ok ? 'done' : 'failed')}`
    case 'agent_started': return `${e.agent} started`
    case 'state_changed': return `${e.incident_id}: ${e.from} → ${e.to}`
    case 'incident_opened': return `${e.incident_id} opened — ${e.title}`
    case 'incident_closed': return `${e.incident_id} closed — ${e.outcome}`
    case 'policy_decision': return `${e.action}: ${e.outcome}${e.reason ? ` — ${e.reason}` : ''}`
    case 'approval_required': return `${e.incident_id}: ${e.action} needs approval — ${e.reason}`
    case 'verification': return `${e.status} (${formatPct(e.before_success_rate)} → ${formatPct(e.after_success_rate)})`
    case 'audit': return `${e.action} ${e.execution_result} (approved by ${e.approved_by})`
    case 'escalated': return `${e.incident_id} escalated — ${e.reason}`
    case 'diagnosis_revised': return `${e.incident_id}: diagnosis revised ${e.from} → ${e.to}`
    case 'rollback': return e.summary
    case 'scenario_triggered': return e.name
    default: return e.summary || e.reason || ''
  }
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

export function impactFacts(incident) {
  const im = incident?.impact
  if (!im) return []
  return [
    ['Revenue at risk', `${formatINR(im.revenue_at_risk_per_hour_paise)}/hr`],
    ['Extra failures', `${im.transactions_at_risk_per_hour.toLocaleString()}/hr`],
    ['Customers affected', `${im.affected_customers_estimate.toLocaleString()}/hr`],
    ['60-min projection', formatINR(im.projected_loss_if_unmitigated_paise)],
  ]
}
