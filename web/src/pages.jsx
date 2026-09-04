import { Fragment } from 'react'
import SuccessRateChart from './Chart.jsx'
import IncidentStory from './Incident.jsx'
import { formatDuration, formatINR, formatPct } from './api.js'
import {
  Card, Empty, ErrorBox, HealthBars, Led, MemoryTable, PreventionCard, RevenueBar, Skeleton, Tag,
  confidenceInWords, readableAction, severityTone, statusTone,
} from './components.jsx'

/* Pages. Every figure comes from the API; where the system has not produced something yet the
 * page says so rather than filling the space. */

/* =========================================================== command centre */

/** The executive view. Five seconds should answer: how much is at risk, how much came back, how
 *  much of that was automatic, what has been learned, and what could be prevented next.
 *
 *  It deliberately does not show the agent pipeline. Someone opening this page is asking about
 *  their money, not about the software; the pipeline is one click away on the incident itself. */
export function CommandCenter({
  metrics, series, incidents, incident, health, busy, error, notice,
  selectedId, onOpen, onApprove, onRunHeadline, learning, onGoto, onSeed,
}) {
  const live = incidents?.find((i) => i.state !== 'CLOSED') || null
  const shown = incidents?.find((i) => i.incident_id === selectedId) || live || incidents?.[0] || null
  const matches = incident && shown && incident.incident_id === shown.incident_id
  const rev = metrics?.revenue
  const prevention = (learning?.prevention || []).filter((r) => r.status === 'PROPOSED')

  return (
    <>
      {error ? <ErrorBox>{error}</ErrorBox> : null}

      <div className="page-head">
        <div>
          <h1>Revenue operations</h1>
          <p>
            What the agent is protecting, what it has recovered, and what it has learned to do
            about it. Every figure is measured from this merchant's own payment traffic.
          </p>
        </div>
        <span className="pill mono">{metrics ? formatPct(metrics.success_rate) : '\u2014'} success rate</span>
      </div>

      <div className="exec">
        <ExecCard
          k="Revenue at risk"
          v={formatINR(metrics?.revenue_at_risk_per_hour_paise)}
          unit="/hour"
          hint={live ? 'across open incidents, right now' : 'nothing open'}
          tone={metrics?.revenue_at_risk_per_hour_paise ? 'crit' : 'ok'}
        />
        <ExecCard
          k="Revenue recovered"
          v={formatINR((rev?.revenue_protected_paise || 0) + (rev?.revenue_recovered_paise || 0))}
          hint={rev?.incidents ? `across ${rev.incidents} closed incident${rev.incidents === 1 ? '' : 's'}` : 'nothing closed yet'}
          tone="ok"
        />
        <ExecCard
          k="Recovery rate"
          v={rev?.revenue_at_risk_paise ? formatPct(rev.recovery_rate, 0) : '\u2014'}
          hint={rev?.revenue_at_risk_paise ? `of ${formatINR(rev.revenue_at_risk_paise)} at risk` : ''}
        />
        <ExecCard
          k="Active incidents"
          v={metrics?.active_incidents ?? '\u2014'}
          hint={live ? live.title : 'all clear'}
          tone={metrics?.active_incidents ? 'crit' : 'ok'}
        />
        <ExecCard
          k="Auto-recovered"
          v={metrics?.auto_recovered ?? '\u2014'}
          hint="closed without a person"
        />
        <ExecCard
          k="Learned patterns"
          v={metrics?.learned_patterns ?? '\u2014'}
          hint={`${metrics?.incidents_remembered ?? 0} incidents remembered`}
          onClick={() => onGoto('learning')}
        />
        <ExecCard
          k="Prevention"
          v={prevention.length}
          hint={prevention.length ? 'awaiting merchant approval' : 'none proposed'}
          tone={prevention.length ? 'warn' : ''}
          onClick={prevention.length ? () => onGoto('learning') : undefined}
        />
        <ExecCard
          k="Payments rescued"
          v={(metrics?.orders_recovered ?? 0).toLocaleString()}
          hint={metrics?.orders_recoverable ? `of ${metrics.orders_recoverable.toLocaleString()} recoverable` : 'after infrastructure recovery'}
        />
      </div>

      {shown ? (
        <Card
          title={live ? 'Open incident' : 'Most recent incident'}
          right={<button className="btn sm" onClick={() => onOpen(shown.incident_id)}>Open the full story</button>}
        >
          <div className="glance">
            <div className="glance-main">
              <div className="glance-top">
                <Led tone={live ? 'crit' : 'ok'} live={!!live} />
                <b>{shown.title}</b>
                <Tag tone={severityTone(shown.severity)}>{shown.severity}</Tag>
              </div>
              <p className="glance-why">
                {matches && incident.root_cause
                  ? `${incident.root_cause.most_likely_root_cause}. ${confidenceInWords(incident.root_cause.confidence)}.`
                  : 'Agents are investigating\u2026'}
              </p>
              {matches && incident.proposal ? (
                <p className="glance-do">
                  <span>Agent recommends</span> {readableAction(incident.proposal.action)}
                  {incident.proposal.parameters?.percentage != null
                    ? ` ${incident.proposal.parameters.percentage}%`
                    : ''}
                </p>
              ) : null}
              {matches && incident.state === 'AWAITING_HUMAN_APPROVAL' ? (
                <button className="btn primary" disabled={busy} onClick={onApprove}>
                  Approve and execute
                </button>
              ) : null}
            </div>
            <div className="glance-side">
              {matches && incident.revenue?.measurable ? (
                <>
                  <RevenueBar revenue={incident.revenue} compact />
                  <div className="facts">
                    <div className="fact"><span>At risk</span><b>{formatINR(incident.revenue.revenue_at_risk_paise)}</b></div>
                    <div className="fact"><span>Recovery</span><b>{formatPct(incident.revenue.recovery_rate, 0)}</b></div>
                    <div className="fact"><span>Duration</span><b>{formatDuration(incident.revenue.exposure_seconds)}</b></div>
                  </div>
                </>
              ) : (
                <div className="facts">
                  <div className="fact">
                    <span>At risk</span>
                    <b>{formatINR(shown.revenue_at_risk_per_hour_paise)}/hr</b>
                  </div>
                </div>
              )}
            </div>
          </div>
        </Card>
      ) : (
        <Card>
          <div className="idle-cta">
            <div>
              <b>Nothing is broken right now.</b>
              <span>
                Break something and watch the agent detect it, price it in rupees, choose the
                safest way out, prove whether it worked, and remember the answer.
              </span>
            </div>
            <div style={{ display: 'flex', gap: 9 }}>
              {onSeed && !metrics?.incidents_remembered ? (
                <button className="btn" disabled={busy} onClick={onSeed}>Load demo history</button>
              ) : null}
              <button className="btn primary" disabled={busy} onClick={onRunHeadline}>
                {busy ? 'Starting\u2026' : 'Run an incident'}
              </button>
            </div>
          </div>
          {notice ? <div className={`notice ${notice.kind}`}>{notice.text}</div> : null}
          <SuccessRateChart points={series} baseline={metrics?.baseline_success_rate} height={168} />
        </Card>
      )}

      <div className="grid-2">
        <Card title="Payment health" right={<button className="btn sm" onClick={() => onGoto('health')}>Details</button>}>
          <HealthChips groups={[['Methods', health?.payment_method], ['Providers', health?.psp]]} />
        </Card>
        <Card title="What it would prevent next" right={<button className="btn sm" onClick={() => onGoto('learning')}>Learning</button>}>
          {prevention.length ? (
            <>
              <p className="glance-why">{prevention[0].pattern}</p>
              <div className="facts">
                <div className="fact"><span>Occurrences</span><b>{prevention[0].occurrences}</b></div>
                <div className="fact"><span>Historical loss</span><b>{formatINR(prevention[0].historical_revenue_lost_paise)}</b></div>
                <div className="fact"><span>Estimated benefit</span><b>{formatINR(prevention[0].estimated_benefit_paise)}</b></div>
              </div>
              <p className="wf-note" style={{ marginTop: 10 }}>
                Advisory. Merchant policy is only ever changed by a person editing the policy file.
              </p>
            </>
          ) : (
            <p className="muted">
              A pattern needs at least three comparable incidents before it is worth a merchant's
              attention.
            </p>
          )}
        </Card>
      </div>
    </>
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

function ExecCard({ k, v, unit, hint, tone = '', onClick }) {
  const Tag_ = onClick ? 'button' : 'div'
  return (
    <Tag_ className={`exec-card ${tone} ${onClick ? 'clickable' : ''}`} onClick={onClick}>
      <span className="exec-k">{k}</span>
      <b className="exec-v">{v}{unit ? <i>{unit}</i> : null}</b>
      {hint ? <span className="exec-hint">{hint}</span> : null}
    </Tag_>
  )
}

/* ============================================================== incidents */

export function Incidents({
  incidents, incident, trace, learning, agents, metrics, busy, selectedId,
  onOpen, onApprove, onReject, onStep, onGoto,
}) {
  if (!incidents?.length) {
    return (
      <>
        <div className="page-head"><div><h1>Incidents</h1><p>Every incident the agent has worked, told as one story each.</p></div></div>
        <Card><Empty title="No incidents yet" body="Run something from Simulate and the agent will work it end to end here." facts={[{ label: 'Detector running', tone: 'ok', live: true }]} /></Card>
      </>
    )
  }

  const open = incidents.find((i) => i.incident_id === selectedId) || null

  return (
    <>
      <div className="page-head">
        <div><h1>Incidents</h1><p>{incidents.length} recorded · newest first</p></div>
      </div>

      {/* The list stays a list. One incident is open at a time, because the story below is long
          and two of them side by side is unreadable. */}
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
              <span className="inc-row-money mono">
                {i.revenue_at_risk_per_hour_paise ? `${formatINR(i.revenue_at_risk_per_hour_paise)}/hr` : ''}
              </span>
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

      {open && incident && incident.incident_id === open.incident_id ? (
        <IncidentStory
          incident={incident} trace={trace} learning={learning} agents={agents} busy={busy}
          metrics={metrics}
          onApprove={onApprove} onReject={onReject} onStep={onStep} onGoto={onGoto}
        />
      ) : open ? (
        <Card><Skeleton rows={6} /></Card>
      ) : (
        <Card>
          <div className="empty" style={{ padding: '30px 20px' }}>
            <p>Select an incident above to read its story, from revenue at risk to what the agent learned.</p>
          </div>
        </Card>
      )}
    </>
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

/* =============================================================== learning */

/** What the system has learned, as a playbook a payments engineer could argue with.
 *
 *  This is the between-incidents view. `store.py` answers what an agent asks mid-incident; this
 *  answers what a person asks afterwards - what do you now believe, on what evidence, and what
 *  would you avoid? It is read-only: the strategy layer queries memory directly, so nothing on
 *  this page sits on the path from a proposal to an execution.
 */
export function Learning({ learning, busy, onDecide, onSeed }) {
  const records = learning?.records || []
  const outcomes = learning?.outcomes || {}
  const prevention = learning?.prevention || []
  const playbook = learning?.playbook || []
  const seeded = learning?.seeded_remembered || 0
  const influenced = learning?.influenced || []

  return (
    <>
      <div className="page-head">
        <div>
          <h1>What the agent has learned</h1>
          <p>
            Every incident that closes is priced, reduced to a structured record and stored —
            including the ones that went badly. Those records are what the next incident is decided
            against, and what the playbook below is derived from.
          </p>
        </div>
        <div style={{ display: 'flex', gap: 9, alignItems: 'center' }}>
          <span className="pill mono">{records.length} remembered</span>
          {onSeed ? (
            <button className="btn" disabled={busy} onClick={onSeed}>Load demo history</button>
          ) : null}
        </div>
      </div>

      {seeded ? (
        <div className="notice">
          <b>{seeded} of {records.length}</b> remembered incidents are seeded demo history, marked{' '}
          <span className="tag mute">seeded</span> wherever they appear. They describe the same
          failure the UPI PSP scenario produces, so a live incident retrieves them on merit — but
          they were not measured from traffic in this session.
        </div>
      ) : null}

      {records.length ? (
        <div className="exec">
          <ExecCard k="Patterns discovered" v={playbook.length} hint="distinct failure signatures" />
          <ExecCard
            k="Interventions that worked"
            v={(outcomes.actions || []).reduce((n, a) => n + a.helped, 0)}
            hint={`of ${(outcomes.actions || []).reduce((n, a) => n + a.attempts, 0)} executed`}
            tone="ok"
          />
          <ExecCard
            k="Rolled back"
            v={outcomes.rollbacks ?? 0}
            hint="undone after failing verification"
            tone={outcomes.rollbacks ? 'warn' : ''}
          />
          <ExecCard
            k="Revenue protected"
            v={formatINR((outcomes.revenue_protected_paise || 0) + (outcomes.revenue_recovered_paise || 0))}
            hint={`of ${formatINR(outcomes.revenue_at_risk_paise)} at risk`}
          />
          <ExecCard
            k="Average recovery"
            v={formatPct(outcomes.recovery_rate, 0)}
            hint="across everything it remembers"
          />
          <ExecCard
            k="Median time to recover"
            v={outcomes.median_time_to_recovery_s ? formatDuration(outcomes.median_time_to_recovery_s) : '—'}
            hint="open to closed"
          />
        </div>
      ) : null}

      {playbook.length ? (
        <>
          <h2 className="section-h">Learned playbook</h2>
          {playbook.map((e) => <PlaybookEntry key={e.signature_key} entry={e} />)}
        </>
      ) : (
        <Card>
          <Empty
            title="Nothing learned yet"
            body="Every incident that closes is priced, reduced to a structured record and stored here — including the ones that went badly. Run an incident from Simulate, or load the seeded demo history."
          />
        </Card>
      )}

      {(learning?.by_payment_method?.length || learning?.by_provider?.length) ? (
        <div className="grid-2">
          <Card title="Most effective action, by payment method">
            <Effectiveness rows={learning.by_payment_method} keyField="payment_method" />
          </Card>
          <Card title="Most effective action, by provider">
            <Effectiveness rows={learning.by_provider} keyField="psp" />
          </Card>
        </div>
      ) : null}

      {influenced.length ? (
        <Card
          title="Incidents decided with history on the record"
          sub={`${influenced.length} of ${records.length}`}
        >
          <div className="rows">
            {influenced.slice(-8).reverse().map((r) => (
              <div className="row" key={r.incident_id}>
                <span className="row-id">{r.incident_id}</span>
                <Tag tone={r.helped ? 'ok' : 'warn'}>
                  {(r.verification || '').replace(/_/g, ' ').toLowerCase()}
                </Tag>
                <span className="row-main">
                  <b>{readableAction(r.action)}{r.magnitude != null ? ` ${r.magnitude}%` : ''}</b>
                  <span className="row-sub">
                    decided with {r.informed_by} comparable incident{r.informed_by === 1 ? '' : 's'} already recorded
                    {r.seeded ? ' · seeded' : ''}
                  </span>
                </span>
                <span className="row-num">{formatPct(r.recovery_rate, 0)}</span>
                <span className="row-num dim">{r.label}</span>
              </div>
            ))}
          </div>
          <p className="wf-note" style={{ marginTop: 12 }}>
            The first incident of a kind has nothing to go on. Every one after it is weighed against
            what happened to the ones before — which is the whole claim, and it is a count rather
            than an assertion.
          </p>
        </Card>
      ) : null}

      <h2 className="section-h">Prevention</h2>
      <p className="section-p">
        Patterns that have repeated often enough to be worth stopping rather than handling. These
        are documents: approving one records the request, and merchant policy is only ever changed
        by a person editing the policy file.
      </p>
      {prevention.length ? (
        <Card>
          {prevention.map((r) => (
            <PreventionCard key={r.recommendation_id} recommendation={r} busy={busy} onDecide={onDecide} />
          ))}
        </Card>
      ) : (
        <Card>
          <Empty
            title="No recurring pattern yet"
            body="A pattern needs at least three comparable incidents before it is worth a merchant's attention."
          />
        </Card>
      )}

      <Card title="Incident memory" sub="newest first">
        <MemoryTable records={records} />
      </Card>
    </>
  )
}

/** One learned entry: what works here, what to avoid, and the evidence for both. */
function PlaybookEntry({ entry }) {
  return (
    <Card
      title={entry.label}
      right={
        <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {entry.seeded_incidents ? <Tag tone="mute">{entry.seeded_incidents} seeded</Tag> : null}
          <Tag tone={entry.confident ? 'ok' : 'mute'}>
            {entry.incidents} incident{entry.incidents === 1 ? '' : 's'}
          </Tag>
        </span>
      }
    >
      {entry.confident ? (
        <div className="play">
          <div className="play-pref">
            <span className="play-k">Preferred intervention</span>
            <b>{readableAction(entry.preferred_action)} · {entry.preferred_band}</b>
            <div className="facts">
              <div className="fact"><span>Historical success</span><b>{formatPct(entry.preferred_helped_rate, 0)}</b></div>
              <div className="fact">
                <span>Median recovery</span>
                <b>{entry.preferred_median_recovery_s ? formatDuration(entry.preferred_median_recovery_s) : '—'}</b>
              </div>
              <div className="fact"><span>Revenue protected</span><b>{formatINR(entry.revenue_protected_paise)}</b></div>
              <div className="fact"><span>Recovery rate</span><b>{formatPct(entry.recovery_rate, 0)}</b></div>
            </div>
            <p className="play-note">{entry.note}</p>
          </div>
          {entry.avoid_band ? (
            <div className="play-avoid">
              <span className="play-k">Avoid</span>
              <b>{entry.avoid_band}</b>
              <p className="play-note">{entry.avoid_reason}</p>
            </div>
          ) : null}
        </div>
      ) : (
        <p className="muted">{entry.note}</p>
      )}

      <details className="more">
        <summary>Every approach tried against this failure</summary>
        <div className="rows">
          {entry.bands.map((b) => (
            <div className="row outcome" key={b.band}>
              <span className="row-main">
                <b>{b.band}</b>
                <span className="row-sub">
                  {b.median_recovery_s ? `median recovery ${formatDuration(b.median_recovery_s)}` : 'no recovery time recorded'}
                  {' · '}{formatINR(b.revenue_protected_paise)} protected
                </span>
              </span>
              <span className="row-num">{b.helped}/{b.attempts} helped</span>
              <span className="row-num dim">
                {b.fully_recovered} fully recovered{b.rollbacks ? ` · ${b.rollbacks} rolled back` : ''}
              </span>
            </div>
          ))}
        </div>
      </details>
    </Card>
  )
}

function Effectiveness({ rows, keyField }) {
  if (!rows?.length) return <p className="muted">Nothing executed yet against this dimension.</p>
  return (
    <div className="rows">
      {rows.map((r) => (
        <div className="row outcome" key={r[keyField]}>
          <span className="row-main">
            <b>{r[keyField]}</b>
            <span className="row-sub">
              {readableAction(r.best_action)}
              {r.best_action_band !== 'any' ? ` · ${r.best_action_band}` : ''}
            </span>
          </span>
          <span className="row-num">{formatPct(r.helped_rate, 0)}</span>
          <span className="row-num dim">{formatINR(r.revenue_protected_paise)} protected</span>
        </div>
      ))}
    </div>
  )
}

/* ================================================================= agents */

const PIPELINE = [
  'observe', 'detect', 'investigate', 'diagnose', 'plan recovery', 'decide', 'policy gate',
  'act', 'verify', 'recover orders', 'learn', 'prevent',
]

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
Ten agents with one responsibility each, and a reproducible benchmark that measures
            them — including whether remembering past incidents actually changes the next decision,
            and whether it changes anything it must not. Every figure below is generated by the
            harness and read from its JSON output.
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
  // Sections added as the system grew: revenue and learning are absent from a report generated
  // before they existed, so they render only when the harness actually produced them.
  const rev = report.revenue || {}
  const lrn = report.learning || {}
  const groups = [
    ['Detection', [['Precision', d.precision], ['Recall', d.recall], ['F1', d.f1], ['False positives', `${d.false_positives} of ${d.false_positives + d.true_negatives}`], ['Scenarios detected', `${d.scenarios_detected}/${d.scenarios_total}`], ['Median latency', `${d.median_detection_latency_s}s`]]],
    ['Diagnosis', [['Top-1 accuracy', g.top1_accuracy], ['Evidence grounding', g.evidence_grounding_rate], ['Brier score', g.brier_score], ['Flagged ambiguous', g.ambiguous_rate]]],
    ['Safety', [['Policy violations', r.policy_violations], ['Unauthorised actions', r.unauthorised_executions], ['Tool success', r.tool_success_rate], ['Rollback success', `${r.rollback_success_rate ?? '—'} of ${r.rollbacks_attempted}`], ['Approval required', r.approval_required_rate]]],
    ['Business', [['Median revenue error', `${b.median_abs_revenue_error_pct}%`], ['Within 25% / 50%', `${b.within_25pct} / ${b.within_50pct}`], ['Median time to mitigate', `${b.median_time_to_mitigate_s}s`], ['Detection vs human', `${e.ai_median_detection_s}s vs ${e.baseline_detection_s}s`]]],
    ...(rev.incidents_priced ? [['Revenue recovery', [
      ['At risk', formatINR(rev.revenue_at_risk_paise)],
      ['Protected / recovered', `${formatINR(rev.revenue_protected_paise)} / ${formatINR(rev.revenue_recovered_paise)}`],
      ['Recovery rate', formatPct(rev.recovery_rate, 0)],
      // Must read 1. Anything less means a rupee is counted twice and every figure above is fiction.
      ['Revenue identity holds', rev.revenue_identity_rate],
      ['Failed payments recovered', `${rev.orders_recovered} of ${rev.orders_recoverable} recoverable`],
    ]]] : []),
    ...(lrn.repetitions ? [['Learning', [
      ['Repetitions', `${lrn.repetitions} against one memory`],
      ['Records written', lrn.records_written],
      ['Comparable, first to last', `${lrn.comparable_incidents_first_run} → ${lrn.comparable_incidents_last_run}`],
      ['Efficacy prior moved', `mean ${lrn.mean_efficacy_adjustment}, max ${lrn.max_efficacy_adjustment}`],
      ['Policy consulted', lrn.policy_consulted_rate],
      ['Unauthorised under learning', lrn.unauthorised_executions],
      ['Prevention, all advisory', String(lrn.prevention_all_require_approval)],
    ]]] : []),
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
              {rows.map(([k, v]) => (
                <Fragment key={k}>
                  <dt>{k}</dt>
                  <dd>{v}</dd>
                </Fragment>
              ))}
            </dl>
          </Card>
        ))}
      </div>
      <Card><p style={{ fontSize: 12, color: 'var(--text-3)' }}>{e.baseline_note}</p></Card>
    </>
  )
}
