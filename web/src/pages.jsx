import { useState } from 'react'
import SuccessRateChart from './Chart.jsx'
import { formatINR, formatPct, formatClock, severityClass } from './api.js'
import {
  AgentTrace, Card, Empty, ErrorBox, Evidence, EventFeed, HealthBars, Hypotheses, Led, Metric,
  Skeleton, Tag, Timeline, confidenceInWords, impactFacts, paramText, readableAction, severityTone,
} from './components.jsx'

/* Pages. Every figure comes from the API; where the system has not produced something yet the
 * page says so rather than filling the space. */

const isOpen = (i) => i && i.state !== 'CLOSED'

/* =========================================================== command centre */

export function CommandCenter({
  metrics, series, incidents, incident, agents, health, events, scenarios, busy, notice, error,
  onInject, onReset, onOpen, onApprove, onReject, onGoto,
}) {
  const live = incidents?.find((i) => i.state !== 'CLOSED') || null
  // Three states, not two: nothing has happened, something is happening, or something happened
  // and is done. Showing the calm empty state while a finished investigation sits underneath it
  // reads as a contradiction.
  const recent = live || incidents?.[0] || null
  const failed = metrics ? Math.round(metrics.transactions * (1 - metrics.success_rate)) : null
  const down = (metrics?.deviation ?? 0) < -0.005

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Payment Command Center</h1>
          <p>Real-time monitoring and multi-agent incident response across payment infrastructure.</p>
        </div>
        <span className="pill">
          <Led tone={live ? 'crit' : 'ok'} live />
          {live ? 'Active incident detected' : 'All systems operational'}
        </span>
      </div>

      {error ? <ErrorBox>{error}</ErrorBox> : null}

      <div className="metrics">
        <Metric
          label="Transaction success rate"
          value={metrics ? formatPct(metrics.success_rate) : '—'}
          delta={metrics ? `${formatPct(Math.abs(metrics.deviation), 1)} vs baseline ${formatPct(metrics.baseline_success_rate)}` : null}
          deltaTone={down ? 'down' : 'up'}
          spark={series}
          sparkTone={down ? 'crit' : 'ok'}
        />
        <Metric label="Payment volume" value={metrics ? formatINR(metrics.gmv_paise) : '—'} delta="in the current window" />
        <Metric label="Failed transactions" value={failed != null ? failed.toLocaleString() : '—'} delta={metrics ? `of ${metrics.transactions.toLocaleString()} attempts` : null} />
        <Metric label="Active incidents" value={metrics?.active_incidents ?? '—'} delta={metrics ? `${metrics.resolved_incidents} resolved · ${metrics.escalated_incidents} escalated` : null} />
        <Metric label="p95 latency" value={metrics ? `${metrics.p95_latency_ms} ms` : '—'} delta="checkout to authorisation" />
      </div>

      {recent ? (
        <IncidentBanner incident={incident} summary={recent} resolved={!live} busy={busy} onOpen={onOpen} onApprove={onApprove} onReject={onReject} onGoto={onGoto} />
      ) : (
        <Card>
          <Empty
            title="No active incidents"
            body="Payment infrastructure is operating normally. Agents are continuously monitoring transaction patterns, provider health and issuer behaviour."
            facts={[
              { label: 'AI agents monitoring', tone: 'agent', live: true },
              { label: `Last scan ${metrics ? formatClock(metrics.timestamp) : '—'}`, tone: 'ok' },
              { label: `${(health?.payment_method?.length || 0) + (health?.psp?.length || 0)} components watched`, tone: 'ok' },
            ]}
          />
        </Card>
      )}

      <div className="grid-73">
        <Card title="Payment success rate" sub={metrics ? `${metrics.transactions} payments in window` : ''}>
          <SuccessRateChart points={series} baseline={metrics?.baseline_success_rate} />
        </Card>

        <Card
          title="AI Incident Commander"
          right={<span className="sub">{incident ? 'investigation in progress' : 'idle'}</span>}
          flush
        >
          {incident ? (
            <AgentTrace incident={incident} agents={agents} />
          ) : (
            <div className="card-body">
              <p style={{ fontSize: 13, color: 'var(--text-2)' }}>
                Eight agents stand ready. Each has one responsibility and hands on a typed result;
                the trace appears here the moment an incident opens.
              </p>
            </div>
          )}
        </Card>
      </div>

      <div className="grid-2">
        <Card title="Payment method health" sub={`${health?.window_minutes ?? 5} min window`} flush>
          <HealthBars rows={health?.payment_method} />
        </Card>
        <Card title="Provider health" sub="live success rate vs own baseline" flush>
          <HealthBars rows={health?.psp} />
        </Card>
      </div>

      <div className="grid-73">
        <Card title="Inject an incident" right={<button className="btn sm" disabled={busy || !scenarios?.some((s) => s.active)} onClick={onReset}>Reset</button>}>
          {notice ? <div className={`notice ${notice.kind}`}>{notice.text}</div> : null}
          <div className="scen">
            {(scenarios || []).map((s) => (
              <button key={s.scenario_id} className={`scen-btn ${s.active ? 'on' : ''}`} disabled={busy} title={s.description} onClick={() => onInject(s)}>
                <span className="scen-name">{s.name}</span>
                <span className="scen-desc">{s.description.slice(0, 96)}…</span>
                {s.active ? <Tag tone="warn">running</Tag> : null}
              </button>
            ))}
          </div>
        </Card>
        <Card title="Agent activity" sub="live" flush>
          <EventFeed events={events} />
        </Card>
      </div>
    </>
  )
}

function IncidentBanner({ incident, summary, resolved, busy, onOpen, onApprove, onReject, onGoto }) {
  const awaiting = incident?.state === 'AWAITING_HUMAN_APPROVAL'
  const rc = incident?.root_cause
  const tone = resolved ? 'ok' : awaiting ? 'warn' : summary.severity === 'CRITICAL' ? '' : 'warn'
  return (
    <div className={`inc-banner ${tone}`}>
      <div className="inc-top">
        <Tag tone={resolved ? 'ok' : severityTone(summary.severity)}>
          {resolved ? ((summary.outcome || 'closed').replace(/_/g, ' ').toLowerCase()) : `${summary.severity} incident`}
        </Tag>
        <span className="inc-title">{summary.title}</span>
        <span className="pill mono">{summary.incident_id}</span>
        {incident ? (
          <span className="pill">
            <Led tone={resolved ? 'ok' : awaiting ? 'warn' : 'agent'} live={!resolved} />
            {incident.state.replace(/_/g, ' ').toLowerCase()}
          </span>
        ) : null}
      </div>

      {rc ? (
        <p style={{ fontSize: 13.5, color: 'var(--text-2)', maxWidth: '80ch' }}>
          {rc.most_likely_root_cause}. {confidenceInWords(rc.confidence)}.
        </p>
      ) : (
        <p style={{ fontSize: 13.5, color: 'var(--text-2)' }}>Agents are investigating…</p>
      )}

      <div className="inc-facts">
        {impactFacts(incident).map(([k, v]) => (
          <div key={k}>
            <div className="inc-fact-k">{k}</div>
            <div className="inc-fact-v">{v}</div>
          </div>
        ))}
      </div>

      <div className="inc-actions">
        <button className="btn" onClick={() => onGoto('investigation')}>View investigation</button>
        <button className="btn" onClick={() => onOpen(summary.incident_id)}>Open incident</button>
        {awaiting ? (
          <>
            <button className="btn primary" disabled={busy} onClick={onApprove}>Approve and execute</button>
            <button className="btn danger" disabled={busy} onClick={onReject}>Reject</button>
          </>
        ) : null}
      </div>
    </div>
  )
}

/* ============================================================ investigation */

export function Investigation({ incident, agents, busy, onApprove, onReject }) {
  if (!incident) {
    return (
      <>
        <div className="page-head"><div><h1>AI Investigation</h1><p>Agent-by-agent investigation of an open incident.</p></div></div>
        <Card><Empty title="Nothing under investigation" body="Open an incident from the Command Center and the full investigation trail appears here." facts={[{ label: 'Agents idle and monitoring', tone: 'agent', live: true }]} /></Card>
      </>
    )
  }
  const rc = incident.root_cause
  const im = incident.impact
  return (
    <>
      <div className="page-head">
        <div>
          <h1>AI Investigation</h1>
          <p>{incident.incident_id} · {incident.title}</p>
        </div>
        <span className="pill"><Led tone={incident.state === 'CLOSED' ? 'ok' : 'agent'} live={incident.state !== 'CLOSED'} />{incident.state.replace(/_/g, ' ').toLowerCase()}</span>
      </div>

      <div className="grid-73">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
          <Card title="Investigation timeline" sub={`${incident.steps?.length || 0} steps`}>
            <Timeline incident={incident} />
          </Card>
          <Card title="Agent trace" sub="one responsibility each" flush>
            <AgentTrace incident={incident} agents={agents} />
          </Card>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
          <Card title="Root cause assessment" sub={rc ? `${rc.reasoner} reasoner` : ''}>
            {rc ? (
              <>
                <div style={{ fontSize: 14, fontWeight: 550, marginBottom: 4 }}>{rc.most_likely_root_cause}</div>
                <div style={{ fontSize: 12.5, color: 'var(--text-2)', marginBottom: 14 }}>
                  {confidenceInWords(rc.confidence)}{rc.ambiguous ? ' · flagged ambiguous' : ''}
                </div>
                <Hypotheses rootCause={rc} />
                <details className="more">
                  <summary>Supporting evidence</summary>
                  <Evidence incident={incident} />
                </details>
              </>
            ) : <Skeleton rows={4} />}
          </Card>

          <Card title="Business impact">
            {im ? (
              <>
                <div className="inc-facts" style={{ marginTop: 0, paddingTop: 0, borderTop: 'none' }}>
                  {impactFacts(incident).map(([k, v]) => (
                    <div key={k}><div className="inc-fact-k">{k}</div><div className="inc-fact-v">{v}</div></div>
                  ))}
                </div>
                <details className="more">
                  <summary>How this was calculated</summary>
                  <ol className="calc">{im.calculation.map((l, i) => <li key={i}>{l}</li>)}</ol>
                </details>
              </>
            ) : <Skeleton rows={3} />}
          </Card>

          <Recommendations incident={incident} busy={busy} onApprove={onApprove} onReject={onReject} />
        </div>
      </div>
    </>
  )
}

function Recommendations({ incident, busy, onApprove, onReject }) {
  const p = incident.proposal
  const pd = incident.policy_decision
  const v = incident.verification
  if (!p) return <Card title="Recommended actions"><Skeleton rows={2} /></Card>
  const awaiting = incident.state === 'AWAITING_HUMAN_APPROVAL'
  return (
    <Card title="Recommended actions">
      <div className="rec">
        <div className="rec-top">
          <span className="rec-p">PRIORITY 1</span>
          {pd ? <Tag tone={pd.approved ? 'ok' : pd.requires_human ? 'warn' : 'crit'}>{pd.outcome.replace(/_/g, ' ').toLowerCase()}</Tag> : null}
        </div>
        <h4>{readableAction(p.action)} {paramText(pd?.granted_parameters || p.parameters)}</h4>
        <p>{p.rationale}</p>
        {pd ? <p style={{ color: 'var(--text-3)' }}>{pd.reason}</p> : null}
        {awaiting ? (
          <div className="rec-actions">
            <button className="btn primary" disabled={busy} onClick={onApprove}>Execute mitigation</button>
            <button className="btn danger" disabled={busy} onClick={onReject}>Reject</button>
          </div>
        ) : null}
      </div>

      {v ? (
        <div className="rec">
          <div className="rec-top">
            <span className="rec-p">VERIFIED</span>
            <Tag tone={v.status === 'RECOVERED' ? 'ok' : v.status === 'PARTIALLY_RECOVERED' ? 'warn' : 'crit'}>{v.status.replace(/_/g, ' ').toLowerCase()}</Tag>
          </div>
          <p>{v.explanation}</p>
        </div>
      ) : null}

      {incident.escalation ? (
        <div className="rec">
          <div className="rec-top"><span className="rec-p">HANDED OVER</span><Tag tone="warn">{incident.escalation.reason_code}</Tag></div>
          <p>{incident.escalation.reason}</p>
          <p style={{ color: 'var(--text-3)' }}>{incident.escalation.recommended_human_action}</p>
        </div>
      ) : null}
    </Card>
  )
}

/* ============================================================== incidents */

export function Incidents({ incidents, incident, agents, busy, onOpen, onApprove, onReject }) {
  const [tab, setTab] = useState('overview')
  if (!incidents?.length) {
    return (
      <>
        <div className="page-head"><div><h1>Live Incidents</h1><p>Every incident the system has opened, with its full evidence trail.</p></div></div>
        <Card><Empty title="No incidents yet" body="Inject a scenario from the Command Center to see the agents work one end to end." facts={[{ label: 'Detector running', tone: 'ok', live: true }]} /></Card>
      </>
    )
  }
  return (
    <>
      <div className="page-head"><div><h1>Live Incidents</h1><p>{incidents.length} recorded · newest first</p></div></div>
      <Card flush>
        <div className="inc-list">
          {incidents.map((i) => (
            <button key={i.incident_id} className={`inc-row ${incident?.incident_id === i.incident_id ? 'on' : ''}`} onClick={() => onOpen(i.incident_id)}>
              <span className="inc-row-id">{i.incident_id}</span>
              <Tag tone={severityTone(i.severity)}>{i.severity}</Tag>
              <span className="inc-row-t">{i.title}</span>
              <Tag tone={i.awaiting_approval ? 'warn' : i.outcome?.includes('RECOVERED') ? 'ok' : 'mute'}>
                {i.awaiting_approval ? 'approval' : (i.outcome || i.state).replace(/_/g, ' ').toLowerCase()}
              </Tag>
            </button>
          ))}
        </div>
      </Card>

      {incident ? (
        <Card title={`${incident.incident_id} · ${incident.title}`} right={<Tag tone={severityTone(incident.severity)}>{incident.severity}</Tag>}>
          <div className="tabs">
            {['overview', 'investigation', 'impact', 'timeline', 'actions'].map((t) => (
              <button key={t} className={`tab ${tab === t ? 'on' : ''}`} onClick={() => setTab(t)}>
                {t[0].toUpperCase() + t.slice(1)}
              </button>
            ))}
          </div>
          <div style={{ paddingTop: 16 }}>
            {tab === 'overview' ? <Overview incident={incident} /> : null}
            {tab === 'investigation' ? <><Hypotheses rootCause={incident.root_cause} /><div style={{ height: 14 }} /><Evidence incident={incident} /></> : null}
            {tab === 'impact' ? (
              incident.impact ? <ol className="calc">{incident.impact.calculation.map((l, i) => <li key={i}>{l}</li>)}</ol> : <Skeleton rows={3} />
            ) : null}
            {tab === 'timeline' ? <Timeline incident={incident} /> : null}
            {tab === 'actions' ? <Recommendations incident={incident} busy={busy} onApprove={onApprove} onReject={onReject} /> : null}
          </div>
        </Card>
      ) : null}
    </>
  )
}

function Overview({ incident }) {
  const a = incident.anomaly
  const rc = incident.root_cause
  const v = incident.verification
  return (
    <dl className="kv">
      <dt>Severity</dt><dd>{incident.severity}</dd>
      <dt>Status</dt><dd>{(incident.outcome || incident.state).replace(/_/g, ' ').toLowerCase()}</dd>
      <dt>Opened</dt><dd>{formatClock(incident.opened_at)}</dd>
      {incident.closed_at ? <><dt>Closed</dt><dd>{formatClock(incident.closed_at)}</dd></> : null}
      {a ? <><dt>Success rate</dt><dd>{formatPct(a.current_value)} vs {formatPct(a.baseline)} baseline</dd></> : null}
      {a ? <><dt>Detected by</dt><dd>{a.detection_method?.join(' + ')}</dd></> : null}
      {rc ? <><dt>Root cause</dt><dd>{rc.most_likely_root_cause}</dd></> : null}
      {rc ? <><dt>Confidence</dt><dd>{formatPct(rc.confidence, 0)}</dd></> : null}
      {incident.impact ? <><dt>Revenue at risk</dt><dd>{formatINR(incident.impact.revenue_at_risk_per_hour_paise)}/hr</dd></> : null}
      {v ? <><dt>Verification</dt><dd>{v.status.replace(/_/g, ' ').toLowerCase()}</dd></> : null}
      {incident.time_to_mitigate_s != null ? <><dt>Time to mitigate</dt><dd>{Math.round(incident.time_to_mitigate_s)}s after detection</dd></> : null}
    </dl>
  )
}

/* ================================================================= health */

export function Health({ health, metrics, series }) {
  return (
    <>
      <div className="page-head">
        <div><h1>Payment Health</h1><p>Live success rate by payment method, provider and issuing bank, each measured against its own baseline.</p></div>
        <span className="pill mono">{health?.window_minutes ?? 5} min window</span>
      </div>
      <Card title="Payment success rate" sub={metrics ? `${metrics.transactions} payments in window` : ''}>
        <SuccessRateChart points={series} baseline={metrics?.baseline_success_rate} />
      </Card>
      <div className="grid-2">
        <Card title="Payment methods" flush><HealthBars rows={health?.payment_method} /></Card>
        <Card title="Providers" flush><HealthBars rows={health?.psp} /></Card>
      </div>
      <Card title="Issuing banks" flush><HealthBars rows={health?.issuer} /></Card>
    </>
  )
}

/* ================================================================= agents */

const PIPELINE = ['observe', 'detect', 'investigate', 'diagnose', 'decide', 'policy gate', 'act', 'verify']

export function Agents({ agents }) {
  return (
    <>
      <div className="page-head"><div><h1>Agent Fleet</h1><p>{agents?.length || 0} agents, one responsibility each, in the order the supervisor runs them.</p></div></div>
      <Card>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', fontFamily: 'var(--mono)', fontSize: 11.5, color: 'var(--text-2)' }}>
          {PIPELINE.map((s, i) => <span key={s}>{s}{i < PIPELINE.length - 1 ? '  →' : ''}</span>)}
        </div>
      </Card>
      <div className="grid-2">
        {(agents || []).map((a, i) => (
          <Card key={a.name} title={`${String(i + 1).padStart(2, '0')} · ${a.name.replace(/_/g, ' ')}`} right={<Tag tone={a.writes ? 'elevated' : 'ok'}>{a.writes ? 'write · gated' : 'read only'}</Tag>}>
            <p style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.6 }}>{a.summary}</p>
            <div style={{ marginTop: 10, fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-3)' }}>{a.state} · timeout {a.timeout_s}s</div>
          </Card>
        ))}
      </div>
    </>
  )
}

/* ============================================================== benchmark */

export function Benchmark({ report }) {
  if (!report || report.detail) {
    return (
      <>
        <div className="page-head"><div><h1>Analytics</h1><p>Reproducible benchmark of the whole pipeline.</p></div></div>
        <Card><Empty title="No benchmark yet" body="Run python -m pic.evaluation.harness to generate it. The dashboard never renders numbers the harness did not produce." /></Card>
      </>
    )
  }
  const { detection: d, diagnosis: g, reliability: r, business: b, end_to_end: e } = report
  const groups = [
    ['Detection', [['Precision', d.precision], ['Recall', d.recall], ['F1', d.f1], ['False positives', `${d.false_positives} of ${d.false_positives + d.true_negatives}`], ['Scenarios detected', `${d.scenarios_detected}/${d.scenarios_total}`], ['Median latency', `${d.median_detection_latency_s}s`]]],
    ['Diagnosis', [['Top-1 accuracy', g.top1_accuracy], ['Evidence grounding', g.evidence_grounding_rate], ['Brier score', g.brier_score], ['Flagged ambiguous', g.ambiguous_rate]]],
    ['Safety', [['Policy violations', r.policy_violations], ['Unauthorised actions', r.unauthorised_executions], ['Tool success', r.tool_success_rate], ['Rollback success', `${r.rollback_success_rate ?? '—'} of ${r.rollbacks_attempted}`], ['Approval required', r.approval_required_rate]]],
    ['Business', [['Median revenue error', `${b.median_abs_revenue_error_pct}%`], ['Within 25% / 50%', `${b.within_25pct} / ${b.within_50pct}`], ['Median time to mitigate', `${b.median_time_to_mitigate_s}s`], ['Detection vs human', `${e.ai_median_detection_s}s vs ${e.baseline_detection_s}s`]]],
  ]
  return (
    <>
      <div className="page-head">
        <div><h1>Analytics</h1><p>Produced by the harness over seeds {report.seeds?.join(', ')} on the {report.reasoner} reasoner. No number here is written by hand.</p></div>
      </div>
      <div className="grid-2">
        {groups.map(([name, rows]) => (
          <Card key={name} title={name}>
            <dl className="kv">
              {rows.map(([k, v]) => <><dt key={k}>{k}</dt><dd key={`${k}v`}>{v}</dd></>)}
            </dl>
          </Card>
        ))}
      </div>
      <Card><p style={{ fontSize: 12, color: 'var(--text-3)' }}>{e.baseline_note}</p></Card>
    </>
  )
}
