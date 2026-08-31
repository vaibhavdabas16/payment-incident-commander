import SuccessRateChart from './Chart.jsx'
import { formatClock, formatINR, formatPct } from './api.js'
import {
  ActivityLog, AgentTrace, Card, Empty, ErrorBox, Evidence, HealthBars, Led, Skeleton, Tag, Workflow, confidenceInWords, impactFacts, readableAction, severityTone, statusTone,
} from './components.jsx'

/* Pages. Every figure comes from the API; where the system has not produced something yet the
 * page says so rather than filling the space. */

const isOpen = (i) => i && i.state !== 'CLOSED'

/* =========================================================== command centre */

export function CommandCenter({
  metrics, series, incidents, incident, agents, health, busy, error, notice,
  selectedId, onSelect, onOpen, onApprove, onReject, onGoto, onRunHeadline,
}) {
  const live = incidents?.find((i) => i.state !== 'CLOSED') || null
  const shown = incidents?.find((i) => i.incident_id === selectedId) || live || incidents?.[0] || null
  const closed = shown && shown.state === 'CLOSED'
  const awaiting = incident?.state === 'AWAITING_HUMAN_APPROVAL'
  const rc = incident?.root_cause
  const matches = incident && shown && incident.incident_id === shown.incident_id

  return (
    <>
      {error ? <ErrorBox>{error}</ErrorBox> : null}

      <section className={`focus ${live ? 'alert' : ''}`}>
        <header className="focus-head">
          <span className="focus-status">
            <Led tone={live ? 'crit' : 'ok'} live={!!live} />
            {live
              ? (shown?.severity === 'CRITICAL' ? 'Critical incident' : 'Incident open')
              : shown ? outcomeWords(shown, incident) : 'All systems operational'}
          </span>
          <span className="focus-spacer" />
          {/* With more than one incident the card has to say so and let you move between them.
              Showing only the newest hid three of four. */}
          {incidents?.length > 1 ? (
            <span className="focus-switch">
              {incidents.slice(0, 6).map((i) => (
                <button
                  key={i.incident_id}
                  className={`switch ${i.incident_id === shown?.incident_id ? 'on' : ''}`}
                  onClick={() => onSelect(i.incident_id)}
                  title={i.title}
                >
                  <Led tone={i.state !== 'CLOSED' ? 'crit' : i.outcome?.includes('RECOVERED') ? 'ok' : 'warn'} />
                  {i.incident_id.replace('INC-', '#')}
                </button>
              ))}
            </span>
          ) : null}
          <span className="focus-time mono">{metrics ? formatPct(metrics.success_rate) : '\u2014'} success</span>
        </header>

        <div className="focus-body">
          {shown ? (
            <div className="focus-inc">
              <h2>{shown.title}</h2>
              <p>
                {rc && matches
                  ? `${rc.most_likely_root_cause}. ${confidenceInWords(rc.confidence)}.`
                  : 'Agents are investigating\u2026'}
              </p>

              {matches ? <Result incident={incident} /> : null}

              <div className="focus-facts">
                {impactFacts(matches ? incident : null).map(([k, v]) => (
                  <div key={k}><span>{k}</span><b>{v}</b></div>
                ))}
              </div>

              <div className="focus-actions">
                {awaiting && matches ? (
                  <>
                    <button className="btn primary" disabled={busy} onClick={onApprove}>Approve and execute</button>
                    <button className="btn danger" disabled={busy} onClick={onReject}>Reject</button>
                  </>
                ) : null}
                <button className="btn" onClick={() => onGoto('investigation')}>View investigation</button>
                {incidents?.length > 1 ? (
                  <button className="btn" onClick={() => onOpen(shown.incident_id)}>All incidents</button>
                ) : null}
              </div>
            </div>
          ) : (
            <div className="focus-idle">
              <div className="idle-cta">
                <div>
                  <b>Nothing is broken right now.</b>
                  <span>
                    Break something and watch eight agents detect it, investigate, decide, ask
                    permission, act — and then check their own work.
                  </span>
                </div>
                <button className="btn primary" disabled={busy} onClick={onRunHeadline}>
                  {busy ? 'Starting\u2026' : 'Run an incident'}
                </button>
              </div>
              {/* The scenario starts instantly; the incident does not, because detection has to
                  see enough degraded traffic to be sure. Saying so is the difference between a
                  wait and a broken button. */}
              {notice ? <div className={`notice ${notice.kind}`}>{notice.text}</div> : null}
              <SuccessRateChart points={series} baseline={metrics?.baseline_success_rate} height={168} />
            </div>
          )}

          {shown && matches ? (
            <aside className="focus-trace">
              <div className="trace-cap">{closed ? 'What the agents did' : 'Working now'}</div>
              <AgentTrace incident={incident} agents={agents} />
            </aside>
          ) : null}
        </div>

        <footer className="focus-foot">
          <HealthChips groups={[['', health?.payment_method], ['', health?.psp]]} />
          <button className="btn sm" onClick={() => onGoto('health')}>Health</button>
        </footer>
      </section>
    </>
  )
}

/** How the incident ended, in one line. Everything else on this card describes the problem; the
 *  card never said whether it was solved, which is the only thing a finished incident is about. */
function outcomeWords(summary, incident) {
  const same = incident?.incident_id === summary.incident_id
  const v = same ? incident.verification : null
  if (same && rolledBack(incident)) return 'Fix reverted \u00b7 handed over'
  if (v?.status === 'RECOVERED') return 'Recovered \u00b7 verified'
  if (v?.status === 'PARTIALLY_RECOVERED') return 'Partly recovered'
  if (summary.outcome === 'HANDED_TO_HUMAN' || summary.outcome === 'ESCALATED') return 'Handed to a human'
  return 'Resolved \u00b7 monitoring'
}

function rolledBack(incident) {
  return (incident?.audit || []).some((a) => String(a.approved_by || '').startsWith('policy_engine:rollback'))
}

function Result({ incident }) {
  const v = incident.verification
  const executed = (incident.audit || []).filter((a) => !String(a.approved_by || '').startsWith('policy_engine:'))
  const action = executed[0]
  if (!v && !action && !incident.escalation) return null

  const reverted = rolledBack(incident)
  const tone = reverted
    ? 'warn'
    : v?.status === 'RECOVERED' ? 'ok'
    : v?.status === 'PARTIALLY_RECOVERED' ? 'warn'
    : v ? 'crit' : 'mute'
  const headline = reverted
    ? 'The fix did not work, so the agent undid it'
    : v?.status === 'RECOVERED' ? 'Recovered, verified against a control group'
    : v?.status === 'PARTIALLY_RECOVERED' ? 'Partly recovered \u2014 the incident stays open'
    : v ? 'The action did not help'
    : 'Handed to a human without acting'

  return (
    <div className={`result ${tone}`}>
      <div className="result-top">
        <Tag tone={tone}>result</Tag>
        <b>{headline}</b>
      </div>
      {action ? (
        <div className="result-line">
          <span>Action</span>{readableAction(action.action)} · approved by {action.approved_by}
        </div>
      ) : null}
      {v ? <div className="result-line"><span>Measured</span>{v.explanation}</div> : null}
      {incident.escalation ? (
        <>
          <div className="result-line">
            <span>Handover</span>{incident.escalation.because || incident.escalation.reason}
          </div>
          <div className="result-line">
            <span>Do next</span>{incident.escalation.recommended_human_action}
          </div>
        </>
      ) : null}
      {incident.time_to_mitigate_s != null ? (
        <div className="result-line"><span>Acted</span>{Math.round(incident.time_to_mitigate_s)}s after detection</div>
      ) : null}
    </div>
  )
}

/** Health as chips: one line per group, a dot and a number each. Two panels of full-width bars
 *  said the same thing in ten times the space. */
function HealthChips({ groups }) {
  return (
    <div className="chips-groups">
      {groups.map(([label, rows]) => (
        <div className="chips-row" key={label}>
          <span className="chips-label">{label}</span>
          <div className="chips">
            {(rows || []).map((r) => (
              <span className={`chip ${r.status}`} key={r.segment} title={`${r.transactions} transactions`}>
                <Led tone={statusTone(r.status)} />
                {r.segment}
                <b>{formatPct(r.success_rate)}</b>
              </span>
            ))}
            {!rows?.length ? <span className="chips-empty">collecting…</span> : null}
          </div>
        </div>
      ))}
    </div>
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

/* ============================================================== incidents */

export function Incidents({ incidents, incident, busy, selectedId, onOpen, onApprove, onReject }) {
  if (!incidents?.length) {
    return (
      <>
        <div className="page-head"><div><h1>Incidents</h1><p>Every incident the system has opened, with its full workflow.</p></div></div>
        <Card><Empty title="No incidents yet" body="Run something from Simulate and the agents will work it end to end here." facts={[{ label: 'Detector running', tone: 'ok', live: true }]} /></Card>
      </>
    )
  }

  const open = incidents.find((i) => i.incident_id === selectedId) || null
  const summary = open || null

  return (
    <>
      <div className="page-head">
        <div><h1>Incidents</h1><p>{incidents.length} recorded · newest first</p></div>
      </div>

      {/* The list stays compact. One incident is open at a time, because a workflow is long and
          two of them side by side is unreadable. */}
      <Card flush>
        <div className="inc-list">
          {incidents.map((i) => (
            <button
              key={i.incident_id}
              className={`inc-row ${i.incident_id === selectedId ? 'on' : ''}`}
              onClick={() => onOpen(i.incident_id === selectedId ? null : i.incident_id)}
            >
              <span className="inc-row-id">{i.incident_id}</span>
              <Tag tone={severityTone(i.severity)}>{i.severity}</Tag>
              <span className="inc-row-t">{i.title}</span>
              <span className="inc-row-state">
                <Led
                  tone={i.state !== 'CLOSED' ? 'crit' : i.outcome?.includes('RECOVERED') ? 'ok' : 'warn'}
                  live={i.state !== 'CLOSED'}
                />
                {i.state !== 'CLOSED'
                  ? (i.awaiting_approval ? 'needs approval' : 'in progress')
                  : (i.outcome || 'closed').replace(/_/g, ' ').toLowerCase()}
              </span>
              <span className="inc-row-caret">{i.incident_id === selectedId ? '▾' : '▸'}</span>
            </button>
          ))}
        </div>
      </Card>

      {summary && incident && incident.incident_id === summary.incident_id ? (
        <IncidentDetail incident={incident} summary={summary} busy={busy} onApprove={onApprove} onReject={onReject} />
      ) : summary ? (
        <Card><Skeleton rows={5} /></Card>
      ) : (
        <Card>
          <div className="empty" style={{ padding: '30px 20px' }}>
            <p>Select an incident above to follow its workflow, stage by stage.</p>
          </div>
        </Card>
      )}
    </>
  )
}

function IncidentDetail({ incident, summary, busy, onApprove, onReject }) {
  const closed = incident.state === 'CLOSED'
  const awaiting = incident.state === 'AWAITING_HUMAN_APPROVAL'
  const reverted = (incident.audit || []).some((a) => String(a.approved_by || '').startsWith('policy_engine:rollback'))
  const v = incident.verification
  const tone = !closed ? (awaiting ? 'warn' : 'info')
    : reverted ? 'warn'
    : v?.status === 'RECOVERED' ? 'ok'
    : incident.escalation ? 'warn' : 'mute'
  const headline = !closed
    ? (awaiting ? 'Waiting for your decision' : 'Agents are working this incident')
    : reverted ? 'The fix did not work, so the agent undid it'
    : v?.status === 'RECOVERED' ? 'Recovered, verified against a control group'
    : v?.status === 'PARTIALLY_RECOVERED' ? 'Partly recovered — not claimed as a success'
    : incident.escalation ? 'Handed to a human' : 'Closed'

  return (
    <Card
      title={`${incident.incident_id} · ${incident.title}`}
      right={<Tag tone={severityTone(incident.severity)}>{incident.severity}</Tag>}
    >
      {/* State first: whether it is running, waiting on you, or finished — and how it finished. */}
      <div className={`banner ${tone}`}>
        <div className="banner-top">
          <Led tone={tone === 'info' ? 'agent' : tone} live={!closed} />
          <b>{headline}</b>
          {closed && incident.time_to_mitigate_s != null ? (
            <span className="banner-when">acted {Math.round(incident.time_to_mitigate_s)}s after detection</span>
          ) : null}
        </div>
        {awaiting ? (
          <div className="banner-actions">
            <button className="btn primary" disabled={busy} onClick={onApprove}>Approve and execute</button>
            <button className="btn danger" disabled={busy} onClick={onReject}>Reject</button>
          </div>
        ) : null}
      </div>

      <div className="inc-facts" style={{ marginTop: 16, paddingTop: 16 }}>
        {impactFacts(incident).map(([k, val]) => (
          <div key={k}><div className="inc-fact-k">{k}</div><div className="inc-fact-v">{val}</div></div>
        ))}
      </div>

      <h4 className="wf-heading">Workflow</h4>
      <Workflow incident={incident} />
      <ActivityLog incident={incident} />
    </Card>
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

/** How it works and how well it works. Two nav items were pretending to be workspaces; a visitor
 *  asking "can I trust this" wants the architecture and the measurement together, and an operator
 *  never opens either while working an incident. */
export function Proof({ report, agents }) {
  return (
    <>
      <div className="page-head">
        <div>
          <h1>How it works</h1>
          <p>
            Eight agents with one responsibility each, and a reproducible benchmark that measures
            them. Every figure below is generated by the harness and read from its JSON output.
          </p>
        </div>
      </div>
      <Benchmark report={report} embedded />
      <Agents agents={agents} embedded />
    </>
  )
}

export function Agents({ agents, embedded = false }) {
  return (
    <>
      {embedded ? (
        <h2 className="section-h">The pipeline</h2>
      ) : (
        <div className="page-head"><div><h1>Agent Fleet</h1><p>{agents?.length || 0} agents, one responsibility each, in the order the supervisor runs them.</p></div></div>
      )}
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

export function Benchmark({ report, embedded = false }) {
  if (!report || report.detail) {
    return (
      <>
        {embedded ? null : <div className="page-head"><div><h1>Analytics</h1><p>Reproducible benchmark of the whole pipeline.</p></div></div>}
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
      {embedded ? (
        <h2 className="section-h">Measured, over seeds {report.seeds?.join(', ')}</h2>
      ) : (
        <div className="page-head">
          <div><h1>Analytics</h1><p>Produced by the harness over seeds {report.seeds?.join(', ')} on the {report.reasoner} reasoner. No number here is written by hand.</p></div>
        </div>
      )}
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
