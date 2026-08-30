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


/* ---------------------------------------------------------------- workflow */

/** The whole incident, stage by stage, in the order the supervisor ran it.
 *
 *  A summary card tells you what was decided; this tells you how. Each stage is either done,
 *  running, blocked or not reached, and carries the evidence that stage produced — so the claim
 *  and the working behind it sit together instead of the working living on another page.
 */
export function Workflow({ incident }) {
  if (!incident) return <Skeleton rows={5} />
  const a = incident.anomaly
  const e = incident.evidence
  const im = incident.impact
  const rc = incident.root_cause
  const p = incident.proposal
  const pd = incident.policy_decision
  const ar = incident.action_result
  const v = incident.verification
  const esc = incident.escalation
  const steps = incident.steps || []
  const stepFor = (agent) => steps.find((s) => s.agent === agent)
  // How many times each agent ran. Collapsing the pipeline into one row per stage is what
  // makes it readable, but it also hides a second attempt - and an incident that proposed,
  // acted, failed verification and went round again is a materially different story from one
  // that went straight through.
  const runsFor = (agent) => steps.filter((s) => s.agent === agent).length

  const stages = [
    {
      key: 'detect',
      title: 'Detection',
      done: !!a,
      lead: a ? `Success rate ${formatPct(a.current_value)} against a baseline of ${formatPct(a.baseline)}.` : null,
      meta: a ? `z=${a.z_score} · confidence ${a.confidence} · n=${a.sample_size} · ${a.detection_method?.join(' + ')}` : null,
      tag: a ? { tone: severityTone(a.severity), text: a.severity } : null,
    },
    {
      key: 'investigate',
      title: 'Investigation',
      done: !!e,
      lead: e ? `${e.findings.length} findings from ${e.tools_used.length} read-only tools. Dominant failure code ${e.dominant_error_code || 'n/a'} (${formatPct(e.dominant_error_share, 0)} of failures).` : null,
      body: e ? (
        <ul className="wf-list">
          {e.findings.slice(0, 4).map((f) => (
            <li key={f.finding_id}><code>{f.finding_id}</code>{f.statement}</li>
          ))}
        </ul>
      ) : null,
    },
    {
      key: 'impact',
      title: 'Business impact',
      done: !!im,
      lead: im ? `${formatINR(im.revenue_at_risk_per_hour_paise)} per hour at risk, from ${im.transactions_at_risk_per_hour.toLocaleString()} extra failures affecting ${im.affected_customers_estimate.toLocaleString()} customers an hour.` : null,
      body: im ? <ol className="calc">{im.calculation.map((l, i) => <li key={i}>{l}</li>)}</ol> : null,
    },
    {
      key: 'diagnose',
      title: 'Root cause',
      done: !!rc,
      lead: rc ? `${rc.most_likely_root_cause}. ${confidenceInWords(rc.confidence)}${rc.ambiguous ? ', flagged ambiguous' : ''}.` : null,
      body: rc ? <Hypotheses rootCause={rc} /> : null,
      meta: rc ? `reasoner ${rc.reasoner}` : null,
    },
    {
      key: 'decide',
      title: 'Decision',
      done: !!p,
      lead: p ? `${readableAction(p.action)} ${paramText(p.parameters)}` : null,
      body: p ? <p className="wf-note">{p.rationale}</p> : null,
      meta: p ? `expected value ${formatINR(p.expected_value_paise)} · protects ${formatINR(p.expected_revenue_protected_per_hour_paise)}/hr · risk ${p.risk_score}` : null,
    },
    {
      key: 'policy',
      title: 'Policy gate',
      done: !!pd,
      blocked: pd ? pd.requires_human : false,
      failed: pd ? !pd.approved && !pd.requires_human : false,
      lead: pd ? pd.reason : null,
      meta: pd?.bound_by?.length ? `bound by ${pd.bound_by.join(', ')}` : null,
      tag: pd ? { tone: pd.approved ? 'ok' : pd.requires_human ? 'warn' : 'crit', text: pd.outcome.replace(/_/g, ' ').toLowerCase() } : null,
    },
    {
      key: 'execute',
      title: 'Execution',
      done: !!ar,
      failed: ar ? !ar.success : false,
      lead: ar ? `${readableAction(ar.action)} ${paramText(ar.parameters)}` : null,
      meta: ar ? `adapter ${ar.adapter}${ar.inverse_action ? ' · reversible' : ''}` : null,
    },
    {
      key: 'verify',
      title: 'Verification',
      done: !!v,
      failed: v ? !['RECOVERED', 'PARTIALLY_RECOVERED'].includes(v.status) : false,
      lead: v ? v.explanation : null,
      meta: v ? (v.control_used
        ? `treated ${formatPct(v.treated_success_rate)} vs control ${formatPct(v.control_success_rate)} · p=${v.p_value} · n=${v.treated_sample}/${v.control_sample}`
        : `${formatPct(v.before_success_rate)} → ${formatPct(v.after_success_rate)} · p=${v.p_value}`) : null,
      tag: v ? { tone: ['RECOVERED', 'PARTIALLY_RECOVERED'].includes(v.status) ? 'ok' : 'crit', text: v.status.replace(/_/g, ' ').toLowerCase() } : null,
    },
  ]

  if (esc) {
    stages.push({
      key: 'escalate',
      title: 'Handed to a human',
      done: true,
      blocked: true,
      lead: esc.reason,
      meta: `${esc.reason_code} · ${esc.recommended_human_action}`,
      tag: { tone: 'warn', text: esc.urgency },
    })
  }

  // The last stage is always the outcome, so the workflow ends by saying how it ended rather
  // than trailing off after whatever the final agent happened to be.
  const closed = incident.state === 'CLOSED'
  const reverted = (incident.audit || []).some((x) => String(x.approved_by || '').startsWith('policy_engine:rollback'))
  stages.push({
    key: 'outcome',
    title: 'Outcome',
    done: closed,
    blocked: closed && (reverted || !!esc),
    lead: closed
      ? (reverted ? 'The fix did not help, so the agent reverted its own change and handed over.'
        : v?.status === 'RECOVERED' ? 'Payments recovered, verified against a concurrent control group.'
        : v?.status === 'PARTIALLY_RECOVERED' ? 'Partly recovered — not claimed as a success, and the incident stayed open.'
        : esc ? 'Handed to a human with the evidence attached.'
        : 'Closed.')
      : incident.state === 'AWAITING_HUMAN_APPROVAL'
        ? 'Waiting for a human to approve or reject the proposed action.'
        : 'Still in progress.',
    tag: closed ? { tone: reverted ? 'warn' : v?.status === 'RECOVERED' ? 'ok' : esc ? 'warn' : 'mute', text: (incident.outcome || 'closed').replace(/_/g, ' ').toLowerCase() } : null,
  })

  // The stage after the last completed one is what is happening now.
  const firstPending = stages.findIndex((s) => !s.done)
  if (firstPending >= 0 && incident.state !== 'CLOSED') stages[firstPending].running = true

  return (
    <ol className="wf">
      {stages.map((s, i) => {
        const state = s.failed ? 'failed' : s.blocked && s.done ? 'blocked' : s.done ? 'done' : s.running ? 'running' : 'todo'
        const agent = ({ detect: 'detection', investigate: 'investigation', impact: 'impact', diagnose: 'root_cause', decide: 'decision', execute: 'action', verify: 'verification', escalate: 'escalation' })[s.key]
        const step = stepFor(agent)
        const runs = agent ? runsFor(agent) : 0
        return (
          <li className={`wf-step ${state}`} key={s.key}>
            <span className="wf-dot">{state === 'done' ? '✓' : state === 'failed' ? '✕' : state === 'blocked' ? '!' : i + 1}</span>
            <div className="wf-main">
              <div className="wf-title">
                {s.title}
                {s.tag ? <Tag tone={s.tag.tone}>{s.tag.text}</Tag> : null}
                {state === 'running' ? <Tag tone="agent">working</Tag> : null}
                {state === 'todo' ? <Tag tone="mute">not reached</Tag> : null}
                {runs > 1 ? <Tag tone="warn">{`ran ${runs}\u00d7`}</Tag> : null}
                {step ? <span className="wf-when">{formatClock(step.started_at)}</span> : null}
              </div>
              {s.lead ? <div className="wf-lead">{s.lead}</div> : null}
              {s.body ? <div className="wf-body">{s.body}</div> : null}
              {s.meta ? <div className="wf-meta">{s.meta}</div> : null}
            </div>
          </li>
        )
      })}
    </ol>
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
