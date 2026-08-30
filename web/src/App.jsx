import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import SuccessRateChart from './Chart.jsx'
import { api, connectStream, formatINR, formatClock, formatPct, severityClass } from './api.js'

const POLL_MS = 1500

export default function App() {
  const [metrics, setMetrics] = useState(null)
  const [series, setSeries] = useState([])
  const [incidents, setIncidents] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [scenarios, setScenarios] = useState([])
  const [events, setEvents] = useState([])
  const [evaluation, setEvaluation] = useState(null)
  const [status, setStatus] = useState('connecting')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const pinned = useRef(false)

  // --- live event stream -------------------------------------------------
  useEffect(() => {
    const close = connectStream(
      (event) => {
        if (event.kind === 'replay') {
          setEvents((event.events || []).slice(-80).reverse())
          return
        }
        setEvents((prev) => [event, ...prev].slice(0, 80))
      },
      (s) => setStatus(s),
    )
    return close
  }, [])

  // --- polling -----------------------------------------------------------
  const refresh = useCallback(async () => {
    try {
      const [m, s, i] = await Promise.all([api.metrics(), api.series(60, 45), api.incidents()])
      setMetrics(m)
      setSeries(s.points)
      setIncidents(i.incidents)
      setError(null)
      // Follow the newest incident unless the operator has clicked one themselves.
      if (!pinned.current && i.incidents.length) {
        setSelectedId((current) => current ?? i.incidents[0].incident_id)
      }
    } catch (err) {
      setError(String(err.message || err))
    }
  }, [])

  useEffect(() => {
    refresh()
    const timer = setInterval(refresh, POLL_MS)
    return () => clearInterval(timer)
  }, [refresh])

  useEffect(() => {
    api.scenarios().then((d) => setScenarios(d.scenarios)).catch(() => {})
    api.evaluation().then(setEvaluation).catch(() => setEvaluation(null))
  }, [])

  useEffect(() => {
    if (!selectedId) return
    let stale = false
    const load = () => api.incident(selectedId).then((d) => !stale && setDetail(d)).catch(() => {})
    load()
    const timer = setInterval(load, POLL_MS)
    return () => {
      stale = true
      clearInterval(timer)
    }
  }, [selectedId])

  const act = async (fn) => {
    setBusy(true)
    try {
      await fn()
      await refresh()
    } catch (err) {
      setError(String(err.message || err))
    } finally {
      setBusy(false)
    }
  }

  const live = incidents.find((i) => i.awaiting_approval) || incidents[0] || null

  return (
    <div className="app">
      <Topbar metrics={metrics} status={status} />

      {error ? (
        <div className="approval" role="alert">
          <h3>Connection problem</h3>
          <p>{error}</p>
        </div>
      ) : null}

      <Tiles metrics={metrics} />

      <div className="grid">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
          <section className="panel">
            <div className="panel-head">
              <h2>Payment success rate</h2>
              <span className="clock">{metrics ? `${metrics.transactions} payments in window` : ''}</span>
            </div>
            <div className="panel-body">
              <SuccessRateChart points={series} baseline={metrics?.baseline_success_rate} />
            </div>
          </section>

          <ScenarioPanel scenarios={scenarios} busy={busy} onTrigger={(id) => act(() => api.trigger(id))} />

          <section className="panel">
            <div className="panel-head">
              <h2>Agent event stream</h2>
              <span className="clock">{status}</span>
            </div>
            <div className="panel-body tight">
              <EventFeed events={events} />
            </div>
          </section>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
          <section className="panel">
            <div className="panel-head">
              <h2>Incidents</h2>
              <span className="clock">{incidents.length} total</span>
            </div>
            <div className="panel-body">
              {incidents.length === 0 ? (
                <div className="empty onboarding">
                  <strong>Nothing is broken right now.</strong>
                  <p>
                    Inject a scenario below. The agents will observe, investigate, diagnose, ask
                    the policy gateway for permission, act — and then check their own work.
                  </p>
                  <p className="hint">
                    Start with <em>UPI rails degraded network-wide</em>. The obvious fix cannot
                    work there, so the agent measures no improvement, rolls its own change back and
                    escalates rather than declaring victory.
                  </p>
                </div>
              ) : (
                <div className="incident-list">
                  {incidents.slice(0, 6).map((i) => (
                    <button
                      key={i.incident_id}
                      className={`incident-row ${i.incident_id === selectedId ? 'selected' : ''}`}
                      onClick={() => {
                        pinned.current = true
                        setSelectedId(i.incident_id)
                      }}
                    >
                      <span className="r-id">{i.incident_id}</span>
                      <span className={`badge ${severityClass(i.severity)}`}>{i.severity}</span>
                      <span className="r-title">{i.title}</span>
                      <span className={`badge ${outcomeBadge(i)}`}>{shortState(i)}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </section>

          <IncidentPanel
            incident={detail}
            busy={busy}
            onApprove={() => act(() => api.approve(detail.incident_id))}
            onReject={() => act(() => api.reject(detail.incident_id))}
          />
        </div>
      </div>

      <EvaluationPanel report={evaluation} />

      <p className="note">
        Every figure on this page is produced by the running system. Benchmark numbers come from{' '}
        <code>python -m pic.evaluation.harness</code> and are read from its JSON output — none are
        written by hand.
      </p>
    </div>
  )
}

/* ------------------------------------------------------------------ parts */

function Topbar({ metrics, status }) {
  const agent = metrics?.agent_status || 'Starting…'
  const tone =
    status !== 'connected' ? 'offline' : agent === 'Monitoring' ? '' : agent.includes('Escalated') ? 'alert' : 'busy'
  return (
    <header className="topbar">
      <div className="brand">
        <h1>Payment Command Center</h1>
        <span className="sub">autonomous payment incident response</span>
      </div>
      <div className="topbar-right">
        <span className="clock">sim clock {formatClock(metrics?.timestamp)}</span>
        <div className="agent-status">
          <span className={`pulse ${tone}`} />
          {status === 'connected' ? agent : 'Reconnecting…'}
        </div>
      </div>
    </header>
  )
}

function Tiles({ metrics }) {
  const deviation = metrics?.deviation ?? 0
  const down = deviation < -0.005
  return (
    <div className="tiles">
      <div className="tile">
        <span className="label">Payment success</span>
        <span className="value">{formatPct(metrics?.success_rate)}</span>
        <span className={`foot delta ${down ? 'down' : 'up'}`}>
          {down ? '▼' : '▲'} {formatPct(Math.abs(deviation), 1)} vs baseline{' '}
          {formatPct(metrics?.baseline_success_rate)}
        </span>
      </div>
      <div className="tile">
        <span className="label">Revenue at risk</span>
        <span className="value" style={{ color: metrics?.revenue_at_risk_per_hour_paise ? 'var(--status-critical)' : undefined }}>
          {formatINR(metrics?.revenue_at_risk_per_hour_paise)}
        </span>
        <span className="foot">per hour, across open incidents</span>
      </div>
      <div className="tile">
        <span className="label">Active incidents</span>
        <span className="value">{metrics?.active_incidents ?? 0}</span>
        <span className="foot">
          {metrics?.resolved_incidents ?? 0} resolved · {metrics?.escalated_incidents ?? 0} escalated
        </span>
      </div>
      <div className="tile">
        <span className="label">Revenue protected</span>
        <span className="value" style={{ color: metrics?.revenue_protected_per_hour_paise ? 'var(--status-good)' : undefined }}>
          {formatINR(metrics?.revenue_protected_per_hour_paise)}
        </span>
        <span className="foot">per hour, verified against control</span>
      </div>
    </div>
  )
}

/** What a scenario exists to prove, derived from the scenario's own flags rather than a
 *  hardcoded list that would quietly drift away from the scenarios themselves. */
function scenarioTag(s) {
  if (!s.injects_failure) return { text: 'restraint test', tone: 'good' }
  if (s.fallback_healthy === false) return { text: 'fix fails → rollback', tone: 'high' }
  if (s.recommended_action === 'escalate') return { text: 'hands to a human', tone: 'medium' }
  return null
}

function ScenarioPanel({ scenarios, busy, onTrigger }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Inject an incident</h2>
        <span className="clock">{scenarios.length} scenarios</span>
      </div>
      <div className="panel-body">
        <div className="scenario-grid">
          {scenarios.map((s) => {
            const tag = scenarioTag(s)
            return (
              <button
                key={s.scenario_id}
                className="btn scenario"
                disabled={busy}
                title={s.description}
                onClick={() => onTrigger(s.scenario_id)}
              >
                <span className="s-head">
                  <span className="s-name">{s.name}</span>
                  {tag ? <span className={`badge ${tag.tone}`}>{tag.text}</span> : null}
                </span>
                <span className="s-desc">{s.description.slice(0, 118)}…</span>
              </button>
            )
          })}
        </div>
      </div>
    </section>
  )
}

function EventFeed({ events }) {
  if (!events.length) return <div className="empty">Waiting for agent activity…</div>
  return (
    <ul className="feed" aria-live="polite" aria-relevant="additions">
      {events.map((e, index) => (
        <li key={index}>
          <span className="f-time">{e._at ? formatClock(e._at) : ''}</span>
          <span className="f-kind">{e.kind}</span>
          <span className="f-text">{describeEvent(e)}</span>
        </li>
      ))}
    </ul>
  )
}

function describeEvent(e) {
  switch (e.kind) {
    case 'agent_finished':
      return `${e.agent}: ${e.summary || (e.ok ? 'done' : 'failed')}`
    case 'agent_started':
      return `${e.agent} started`
    case 'state_changed':
      return `${e.incident_id}: ${e.from} → ${e.to}`
    case 'incident_opened':
      return `${e.incident_id} opened — ${e.title}`
    case 'incident_closed':
      return `${e.incident_id} closed — ${e.outcome}`
    case 'policy_decision':
      return `${e.action}: ${e.outcome}${e.reason ? ` — ${e.reason}` : ''}`
    case 'approval_required':
      return `${e.incident_id}: ${e.action} needs human approval — ${e.reason}`
    case 'verification':
      return `${e.status} (${formatPct(e.before_success_rate)} → ${formatPct(e.after_success_rate)})`
    case 'audit':
      return `${e.action} ${e.execution_result} (approved by ${e.approved_by})`
    case 'escalated':
      return `${e.incident_id} escalated — ${e.reason}`
    case 'rollback':
      return e.summary
    case 'scenario_triggered':
      return `${e.name}`
    default:
      return e.summary || e.reason || ''
  }
}

/* -------------------------------------------------------------- lifecycle */

function IncidentPanel({ incident, busy, onApprove, onReject }) {
  if (!incident) {
    return (
      <section className="panel">
        <div className="panel-head">
          <h2>Incident lifecycle</h2>
        </div>
        <div className="panel-body">
          <div className="empty">Select an incident to follow the agent workflow.</div>
        </div>
      </section>
    )
  }

  const stages = buildStages(incident)
  const awaiting = incident.state === 'AWAITING_HUMAN_APPROVAL'

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>
          {incident.incident_id} · {incident.state.replace(/_/g, ' ').toLowerCase()}
        </h2>
        <span className={`badge ${severityClass(incident.severity)}`}>{incident.severity}</span>
      </div>
      <div className="panel-body">
        <IncidentTiming incident={incident} />
        <OutcomeBanner incident={incident} />
        {awaiting ? (
          <div className="approval">
            <h3>⚠ Agent recommends action — human approval required</h3>
            <p>
              <strong>{readableAction(incident.proposal?.action)}</strong>{' '}
              {paramText(incident.policy_decision?.granted_parameters)}
              <br />
              {incident.policy_decision?.reason}
            </p>
            <div className="actions">
              <button className="btn primary" disabled={busy} onClick={onApprove}>
                Approve and execute
              </button>
              <button className="btn danger" disabled={busy} onClick={onReject}>
                Reject
              </button>
            </div>
          </div>
        ) : null}

        {stages.map((stage) => (
          <div key={stage.key} className={`stage ${stage.status}`}>
            <div className="stage-dot">{stage.status === 'done' ? '✓' : stage.status === 'failed' ? '✕' : stage.index}</div>
            <div>
              <div className="stage-title">
                {stage.title}
                {stage.badge ? <span className={`badge ${stage.badgeTone}`}>{stage.badge}</span> : null}
              </div>
              {stage.detail ? <div className="stage-detail">{stage.detail}</div> : null}
              {stage.extra}
              {stage.meta ? <div className="stage-meta">{stage.meta}</div> : null}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

/** The headline of what happened, so the outcome that matters most is not buried in the stage
 *  list. A reverted intervention is the one worth showing loudest: acting is easy, noticing that
 *  the action did not help and undoing it is the hard part. */
function OutcomeBanner({ incident }) {
  const v = incident.verification
  const rolledBack = (incident.audit || []).some(
    (a) => typeof a.approved_by === 'string' && a.approved_by.startsWith('policy_engine:rollback'),
  )
  if (rolledBack) {
    return (
      <div className="outcome reverted" role="status">
        <h3>The fix did not work — so the agent undid it</h3>
        <p>
          The action executed, was measured against a concurrent control group, and produced no
          real improvement. Rather than declare victory or thrash between routes, the agent rolled
          its own change back and handed the incident to a human.
        </p>
      </div>
    )
  }
  if (v && (v.status === 'RECOVERED' || v.status === 'PARTIALLY_RECOVERED')) {
    return (
      <div className="outcome recovered" role="status">
        <h3>Recovery verified against a control group</h3>
        <p>{v.explanation}</p>
      </div>
    )
  }
  if (incident.escalation) {
    return (
      <div className="outcome escalated" role="status">
        <h3>Handed to a human</h3>
        <p>{incident.escalation.reason}</p>
      </div>
    )
  }
  return null
}

function IncidentTiming({ incident }) {
  const bits = []
  if (incident.time_to_mitigate_s != null) {
    bits.push(`acted ${Math.round(incident.time_to_mitigate_s)}s after detection`)
  }
  if (incident.opened_at) bits.push(`opened ${formatClock(incident.opened_at)}`)
  if (incident.closed_at) bits.push(`closed ${formatClock(incident.closed_at)}`)
  if (!bits.length) return null
  return <div className="incident-timing">{bits.join(' · ')}</div>
}

function readableAction(action) {
  return action ? action.replace(/_/g, ' ') : 'no action'
}

/** Granted parameters as prose. `{"max_shift_pct": 25}` reads as "max shift pct 25". */
function paramText(params) {
  const entries = Object.entries(params || {})
  if (!entries.length) return ''
  return entries.map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`).join(', ')
}

function buildStages(incident) {
  const a = incident.anomaly
  const e = incident.evidence
  const im = incident.impact
  const rc = incident.root_cause
  const p = incident.proposal
  const pd = incident.policy_decision
  const ar = incident.action_result
  const v = incident.verification

  const stages = []
  let index = 0
  const push = (key, title, present, body) => {
    index += 1
    stages.push({ key, index, title, status: present ? body.status || 'done' : 'pending', ...body })
  }

  push('detect', 'Detection', !!a, {
    detail: a
      ? `Success rate ${formatPct(a.current_value)} against a baseline of ${formatPct(a.baseline)} (${formatPct(a.deviation)}).`
      : null,
    meta: a ? `z=${a.z_score} · confidence ${a.confidence} · n=${a.sample_size} · ${a.detection_method?.join(' + ')}` : null,
    badge: a?.severity,
    badgeTone: severityClass(a?.severity),
  })

  push('investigate', 'Investigation', !!e, {
    detail: e ? `${e.findings.length} findings from ${e.tools_used.length} tools.` : null,
    extra: e ? (
      <ul className="evidence" style={{ marginTop: 8 }}>
        {e.findings.slice(0, 4).map((f) => (
          <li key={f.finding_id}>
            <span className="fid">{f.finding_id}</span>
            {f.statement}
          </li>
        ))}
      </ul>
    ) : null,
    meta: e ? `dominant failure code ${e.dominant_error_code || 'n/a'} (${formatPct(e.dominant_error_share, 0)} of failures)` : null,
    badge: e?.degraded ? 'partial evidence' : null,
    badgeTone: 'medium',
  })

  push('impact', 'Business impact', !!im, {
    detail: im ? `${formatINR(im.revenue_at_risk_per_hour_paise)} per hour at risk.` : null,
    extra: im ? (
      <ol className="calc" style={{ marginTop: 8 }}>
        {im.calculation.map((line, i) => (
          <li key={i}>{line}</li>
        ))}
      </ol>
    ) : null,
    meta: im
      ? `${im.transactions_at_risk_per_hour.toLocaleString()} extra failures/hr · ~${im.affected_customers_estimate.toLocaleString()} customers/hr`
      : null,
  })

  push('diagnose', 'Root cause', !!rc, {
    detail: rc ? rc.most_likely_root_cause : null,
    extra: rc ? (
      <ul className="evidence" style={{ marginTop: 8 }}>
        {rc.hypotheses.slice(0, 3).map((h) => (
          <li key={h.cause_id}>
            <span className="fid">{formatPct(h.probability, 0)}</span>
            {h.cause}
          </li>
        ))}
        {rc.contradicting_evidence?.slice(0, 2).map((c, i) => (
          <li key={`c${i}`} className="contra">
            <span className="fid">against</span>
            {c}
          </li>
        ))}
      </ul>
    ) : null,
    meta: rc ? `confidence ${rc.confidence} · reasoner ${rc.reasoner}` : null,
    badge: rc?.ambiguous ? 'ambiguous' : null,
    badgeTone: 'medium',
  })

  push('decide', 'Decision', !!p, {
    detail: p ? `${p.action} ${JSON.stringify(p.parameters)}` : null,
    extra: p ? <div className="stage-detail" style={{ marginTop: 6 }}>{p.rationale}</div> : null,
    meta: p
      ? `expected value ${formatINR(p.expected_value_paise)} · protects ${formatINR(p.expected_revenue_protected_per_hour_paise)}/hr · risk ${p.risk_score}`
      : null,
  })

  push('policy', 'Policy gateway', !!pd, {
    status: pd ? (pd.approved ? 'done' : pd.requires_human ? 'blocked' : 'failed') : 'pending',
    detail: pd ? pd.reason : null,
    extra: pd && JSON.stringify(pd.requested_parameters) !== JSON.stringify(pd.granted_parameters) ? (
      <div className="stage-meta">
        requested {JSON.stringify(pd.requested_parameters)} → granted {JSON.stringify(pd.granted_parameters)}
      </div>
    ) : null,
    meta: pd?.bound_by?.length ? `bound by ${pd.bound_by.join(', ')}` : null,
    badge: pd?.outcome,
    badgeTone: pd ? (pd.approved ? 'good' : pd.requires_human ? 'high' : 'critical') : 'neutral',
  })

  push('execute', 'Execution', !!ar, {
    status: ar ? (ar.success ? 'done' : 'failed') : 'pending',
    detail: ar ? `${ar.action} ${JSON.stringify(ar.parameters)}` : null,
    meta: ar ? `adapter ${ar.adapter}${ar.inverse_action ? ' · reversible' : ''}` : null,
  })

  push('verify', 'Verification', !!v, {
    status: v ? (['RECOVERED', 'PARTIALLY_RECOVERED'].includes(v.status) ? 'done' : 'failed') : 'pending',
    detail: v ? v.explanation : null,
    meta: v
      ? v.control_used
        ? `treated ${formatPct(v.treated_success_rate)} vs control ${formatPct(v.control_success_rate)} · p=${v.p_value} · n=${v.treated_sample}/${v.control_sample}`
        : `${formatPct(v.before_success_rate)} → ${formatPct(v.after_success_rate)} · p=${v.p_value} · n=${v.after_sample}`
      : null,
    badge: v?.status,
    badgeTone: v ? (['RECOVERED', 'PARTIALLY_RECOVERED'].includes(v.status) ? 'good' : 'critical') : 'neutral',
  })

  if (incident.escalation) {
    index += 1
    stages.push({
      key: 'escalate',
      index,
      title: 'Escalated to human',
      status: 'blocked',
      detail: incident.escalation.reason,
      meta: `${incident.escalation.reason_code} · ${incident.escalation.recommended_human_action}`,
      badge: incident.escalation.urgency,
      badgeTone: severityClass(incident.escalation.urgency),
    })
  }

  // The stage after the last completed one is what the agent is working on now.
  const firstPending = stages.findIndex((s) => s.status === 'pending')
  if (firstPending >= 0 && incident.state !== 'CLOSED') stages[firstPending].status = 'active'
  return stages
}

/* ------------------------------------------------------------- evaluation */

function EvaluationPanel({ report }) {
  if (!report || report.detail) {
    return (
      <section className="panel">
        <div className="panel-head">
          <h2>Reproducible benchmark</h2>
        </div>
        <div className="panel-body">
          <div className="empty">
            No evaluation results yet. Run <code>python -m pic.evaluation.harness</code>.
          </div>
        </div>
      </section>
    )
  }
  const { detection: d, diagnosis: g, business: b, reliability: r, end_to_end: e } = report
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Reproducible benchmark</h2>
        <span className="clock">
          {report.reasoner} reasoner · seeds {report.seeds?.join(', ')}
        </span>
      </div>
      <div className="panel-body">
        <div className="eval-grid">
          <div className="eval-block">
            <h4>Detection</h4>
            <dl className="kv">
              <dt>precision / recall</dt>
              <dd>{d.precision} / {d.recall}</dd>
              <dt>false positives</dt>
              <dd>{d.false_positives} of {d.false_positives + d.true_negatives} healthy windows</dd>
              <dt>scenarios detected</dt>
              <dd>{d.scenarios_detected}/{d.scenarios_total}</dd>
              <dt>median latency</dt>
              <dd>{d.median_detection_latency_s}s</dd>
            </dl>
          </div>
          <div className="eval-block">
            <h4>Diagnosis</h4>
            <dl className="kv">
              <dt>top-1 accuracy</dt>
              <dd>{g.top1_accuracy}</dd>
              <dt>evidence grounding</dt>
              <dd>{g.evidence_grounding_rate}</dd>
              <dt>Brier score</dt>
              <dd>{g.brier_score}</dd>
              <dt>flagged ambiguous</dt>
              <dd>{g.ambiguous_rate}</dd>
            </dl>
          </div>
          <div className="eval-block">
            <h4>Safety</h4>
            <dl className="kv">
              <dt>policy violations</dt>
              <dd>{r.policy_violations}</dd>
              <dt>unauthorised actions</dt>
              <dd>{r.unauthorised_executions}</dd>
              <dt>tool success rate</dt>
              <dd>{r.tool_success_rate}</dd>
              <dt>appropriate action</dt>
              <dd>{r.appropriate_action_rate}</dd>
            </dl>
          </div>
          <div className="eval-block">
            <h4>Speed vs human model</h4>
            <dl className="kv">
              <dt>detection</dt>
              <dd>{e.ai_median_detection_s}s vs {e.baseline_detection_s}s</dd>
              <dt>mitigation</dt>
              <dd>{e.ai_median_mitigation_s}s vs {e.baseline_mitigation_s}s</dd>
              <dt>speed-up</dt>
              <dd>{e.mitigation_speedup_x}×</dd>
              <dt>revenue error</dt>
              <dd>{b.median_abs_revenue_error_pct}% median</dd>
            </dl>
          </div>
        </div>
        <p className="note">{e.baseline_note}</p>
      </div>
    </section>
  )
}

/* ------------------------------------------------------------------ utils */

function shortState(incident) {
  if (incident.awaiting_approval) return 'approval'
  if (incident.outcome) return incident.outcome.replace(/_/g, ' ').toLowerCase()
  return incident.state.replace(/_/g, ' ').toLowerCase()
}

function outcomeBadge(incident) {
  if (incident.awaiting_approval) return 'high'
  const outcome = incident.outcome || ''
  if (outcome.includes('RECOVERED') || outcome === 'NO_ACTION_REQUIRED') return 'good'
  if (outcome.includes('ESCALATED') || outcome.includes('REGRESSED')) return 'critical'
  return 'neutral'
}
