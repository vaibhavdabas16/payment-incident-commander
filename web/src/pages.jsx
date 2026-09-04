import { Fragment } from 'react'
import SuccessRateChart from './Chart.jsx'
import { formatClock, formatINR, formatPct } from './api.js'
import {
  ActionOutcomes, ActivityLog, AgentTrace, Card, DecisionTrace, Empty, ErrorBox, HealthBars, Led,
  MemoryTable, NextSteps, PreventionCard, RevenueBar, SimilarIncidents, Skeleton, Tag,
  confidenceInWords, impactFacts, readableAction, severityTone, statusTone,
} from './components.jsx'

/* Pages. Every figure comes from the API; where the system has not produced something yet the
 * page says so rather than filling the space. */

/* =========================================================== command centre */

export function CommandCenter({
  metrics, series, incidents, incident, agents, health, busy, error, notice,
  selectedId, onSelect, onOpen, onApprove, onReject, onGoto, onRunHeadline, onStep, learning,
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

      <RevenueHeadline metrics={metrics} incident={matches ? incident : null} live={live} />

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

              {matches ? <Result incident={incident} busy={busy} onStep={onStep} /> : null}

              <div className="focus-facts">
                {impactFacts(matches ? incident : null).map(([k, v]) => (
                  <div key={k}><span>{k}</span><b>{v}</b></div>
                ))}
              </div>

              {matches ? <WhyThisAction incident={incident} /> : null}
              {matches && incident?.revenue?.measurable ? (
                <RevenueBar revenue={incident.revenue} compact />
              ) : null}
              {matches ? <Learned incident={incident} /> : null}

              <div className="focus-actions">
                {awaiting && matches ? (
                  <>
                    <button className="btn primary" disabled={busy} onClick={onApprove}>Approve and execute</button>
                    <button className="btn danger" disabled={busy} onClick={onReject}>Reject</button>
                  </>
                ) : null}
                <button className="btn" onClick={() => onOpen(shown.incident_id)}>
                  View the investigation
                </button>
              </div>
            </div>
          ) : (
            <div className="focus-idle">
              <div className="idle-cta">
                <div>
                  <b>Nothing is broken right now.</b>
                  <span>
                    Break something and watch the agents detect it, investigate, price it, decide,
                    ask permission, act — then check their own work, go back for the payments
                    already lost, and remember what happened.
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

      <NextUp learning={learning} onGoto={onGoto} />
    </>
  )
}

/** The first thing on the screen, and the only thing a merchant actually asked about.
 *
 *  While an incident is live this is exposure — the rate at which money is being lost right now.
 *  Once incidents have closed it becomes the outcome, because a threat that has been dealt with is
 *  no longer the headline. Both numbers come from the same ledger; neither is a projection. */
function RevenueHeadline({ metrics, incident, live }) {
  const totals = metrics?.revenue
  const settled = totals && totals.revenue_at_risk_paise > 0
  const exposure = metrics?.revenue_at_risk_per_hour_paise || 0

  if (live && exposure > 0) {
    return (
      <section className="headline alert">
        <div className="headline-main">
          <span className="headline-k">Revenue at risk, right now</span>
          <b className="headline-v">{formatINR(exposure)}<i>/hour</i></b>
          <span className="headline-sub">
            {incident?.root_cause
              ? `${incident.root_cause.most_likely_root_cause} · ${confidenceInWords(incident.root_cause.confidence).toLowerCase()}`
              : 'Agents are establishing the cause.'}
          </span>
        </div>
        {settled ? <HeadlineTotals totals={totals} metrics={metrics} /> : null}
      </section>
    )
  }

  if (!settled) {
    return (
      <section className="headline">
        <div className="headline-main">
          <span className="headline-k">Revenue protected so far</span>
          <b className="headline-v">{formatINR(0)}</b>
          <span className="headline-sub">
            Nothing has been at risk yet. Break something from Simulate and watch the money move.
          </span>
        </div>
      </section>
    )
  }

  return (
    <section className="headline">
      <div className="headline-main">
        <span className="headline-k">Revenue recovered across {totals.incidents} incident{totals.incidents === 1 ? '' : 's'}</span>
        <b className="headline-v">
          {formatINR(totals.revenue_protected_paise + totals.revenue_recovered_paise)}
        </b>
        <span className="headline-sub">
          of {formatINR(totals.revenue_at_risk_paise)} at risk · {formatPct(totals.recovery_rate, 0)} recovery rate
        </span>
      </div>
      <HeadlineTotals totals={totals} metrics={metrics} />
    </section>
  )
}

function HeadlineTotals({ totals, metrics }) {
  return (
    <div className="headline-side">
      <RevenueBar revenue={totals} />
      <div className="facts">
        <div className="fact"><span>Failed payments rescued</span><b>{(metrics?.orders_recovered || 0).toLocaleString()}</b></div>
        <div className="fact"><span>Incidents remembered</span><b>{metrics?.incidents_remembered ?? 0}</b></div>
        <div className="fact"><span>Prevention proposed</span><b>{metrics?.prevention_recommendations ?? 0}</b></div>
      </div>
    </div>
  )
}

/** Why this action and not another. The historical case, where there is one. */
function WhyThisAction({ incident }) {
  const plan = incident?.recovery_plan
  if (!plan) return null
  const chosen = incident.proposal
  return (
    <div className="why">
      <div className="why-k">Why this action</div>
      <p>{chosen ? chosen.rationale : 'No action has been chosen yet.'}</p>
      <p className="why-hist">{plan.historical_recommendation}</p>
      {plan.similar_incidents?.length ? (
        <details className="more">
          <summary>{`The ${plan.similar_incidents.length} comparable incidents behind that`}</summary>
          <SimilarIncidents rows={plan.similar_incidents} compact />
        </details>
      ) : null}
    </div>
  )
}

/** What the agent took away from this one. */
function Learned({ incident }) {
  const l = incident?.learning
  if (!l) return null
  const size = l.intervention_magnitude ? ` at ${l.intervention_magnitude}%` : ''
  return (
    <div className="why learned">
      <div className="why-k">What it learned</div>
      <p>
        <b>{readableAction(l.actual_action_executed)}{size}</b> under this failure pattern →{' '}
        {l.verification_result.replace(/_/g, ' ').toLowerCase()}
        {l.rollback_required ? ', and had to be rolled back' : ''}.
      </p>
      <p className="why-hist">
        Recorded against <code>{signatureLabel(l.failure_signature)}</code>.
        {' '}Future incidents matching this signature will be weighed against it.
      </p>
    </div>
  )
}

/** The signature as a person reads it.
 *
 *  Built here rather than shipped from the backend because `FailureSignature.label()` is a method
 *  and methods do not survive `model_dump`. The fields it composes are all present, so the label
 *  is derived from the same data on both sides rather than being a second, drifting string. */
function signatureLabel(signature) {
  if (!signature) return 'unclassified'
  const parts = [
    signature.payment_method,
    signature.psp || signature.issuer || signature.gateway,
    signature.dominant_error_code,
  ].filter(Boolean)
  return parts.length ? parts.join(' · ') : signature.root_cause_id || 'unclassified'
}

/** What should happen next: the prevention the system is asking for. */
function NextUp({ learning, onGoto }) {
  const recs = (learning?.prevention || []).filter((r) => r.status === 'PROPOSED')
  if (!recs.length) return null
  const top = recs[0]
  return (
    <section className="headline next">
      <div className="headline-main">
        <span className="headline-k">What should happen next</span>
        <b className="headline-v small">{top.pattern}</b>
        <span className="headline-sub">
          {readableAction(top.proposed_action)} · {formatINR(top.estimated_benefit_paise)} estimated benefit ·
          needs merchant approval
        </span>
      </div>
      <div className="headline-side">
        <button className="btn" onClick={() => onGoto('learning')}>
          {recs.length > 1 ? `Review ${recs.length} recommendations` : 'Review the recommendation'}
        </button>
      </div>
    </section>
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

function Result({ incident, busy, onStep }) {
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
          {onStep ? <NextSteps incident={incident} busy={busy} onStep={onStep} /> : null}
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

/* ============================================================== incidents */

export function Incidents({ incidents, incident, trace, busy, selectedId, onOpen, onApprove, onReject, onStep }) {
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
        <IncidentDetail incident={incident} trace={trace} summary={summary} busy={busy} onApprove={onApprove} onReject={onReject} onStep={onStep} />
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

function IncidentDetail({ incident, trace, summary, busy, onApprove, onReject, onStep }) {
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

      {incident.escalation ? (
        <div className="handover">
          {/* The reason has to sit above the buttons. An operator asked to choose an action
              before being told what happened is being asked to guess. */}
          <p className="handover-why">
            {incident.escalation.because || incident.escalation.reason}
          </p>
          <p className="handover-rec">{incident.escalation.recommended_human_action}</p>
          {onStep ? <NextSteps incident={incident} busy={busy} onStep={onStep} /> : null}
        </div>
      ) : null}

      {incident.revenue?.measurable ? (
        <>
          <h4 className="wf-heading">What happened financially</h4>
          <RevenueBar revenue={incident.revenue} />
          <ol className="calc">{incident.revenue.calculation.map((l, i) => <li key={i}>{l}</li>)}</ol>
        </>
      ) : null}

      <h4 className="wf-heading">Decision trace</h4>
      <p className="wf-note" style={{ marginBottom: 12 }}>
        Observation to learning, one stage at a time. Every stage can be opened to see the evidence
        underneath it — findings and the tool that produced them, hypotheses for and against, the
        comparable incidents this was decided against, every option that was priced, and every
        policy rule the gateway evaluated.
      </p>
      <DecisionTrace trace={trace && trace.incident_id === incident.incident_id ? trace : null} loading />
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

/* =============================================================== learning */

/** What the system has learned, and what it wants to do about it.
 *
 *  This page is the closed loop made inspectable: every record here was written when a real
 *  incident closed in this session, every success rate is a count over those records, and every
 *  recommendation is derived from them. Nothing on it is generated for display.
 */
export function Learning({ learning, busy, onDecide }) {
  const records = learning?.records || []
  const outcomes = learning?.outcomes || {}
  const prevention = learning?.prevention || []

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Learning</h1>
          <p>
            Every incident that closes is priced, reduced to a structured record and stored —
            including the ones that went badly. Those records are what the next incident is decided
            against, and what the recurring-failure patterns below are mined from.
          </p>
        </div>
        <span className="pill mono">{records.length} incidents remembered</span>
      </div>

      {outcomes.incidents ? (
        <Card title="Revenue across everything it remembers" sub={`${outcomes.incidents} incidents`}>
          <RevenueBar revenue={{
            revenue_at_risk_paise: outcomes.revenue_at_risk_paise,
            revenue_protected_paise: outcomes.revenue_protected_paise,
            revenue_recovered_paise: outcomes.revenue_recovered_paise,
            revenue_lost_paise: outcomes.revenue_lost_paise,
          }} />
          <div className="facts" style={{ marginTop: 14 }}>
            <div className="fact"><span>Recovery rate</span><b>{formatPct(outcomes.recovery_rate, 0)}</b></div>
            <div className="fact"><span>Median time to recovery</span><b>{outcomes.median_time_to_recovery_s ? `${Math.round(outcomes.median_time_to_recovery_s)}s` : '—'}</b></div>
            <div className="fact"><span>Rolled back</span><b>{outcomes.rollbacks ?? 0}</b></div>
            <div className="fact"><span>Needed a human</span><b>{outcomes.human_interventions ?? 0}</b></div>
          </div>
        </Card>
      ) : null}

      <Card
        title="What has actually worked"
        sub="counts over recorded outcomes, not estimates"
      >
        <ActionOutcomes rows={outcomes.actions} />
        <p className="wf-note" style={{ marginTop: 12 }}>
          A partial recovery counts as having helped — it is a control-verified improvement that
          did not finish the job. An intervention that had to be rolled back never counts, whatever
          it looked like beforehand. These counts move the efficacy prior behind every future
          decision, within a hard bound, and they cannot reach the policy gateway.
        </p>
      </Card>

      <Card title="Incident memory" sub="newest first">
        <MemoryTable records={records} />
      </Card>

      <div className="page-head" style={{ marginTop: 6 }}>
        <div>
          <h2 className="section-h" style={{ margin: 0 }}>Prevention</h2>
          <p>
            Patterns that have repeated often enough to be worth stopping rather than handling.
            These are documents: approving one records the request, and merchant policy is only ever
            changed by a person editing the policy file.
          </p>
        </div>
      </div>
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
            body="A pattern needs at least three comparable incidents before it is worth a merchant's attention. Run the same scenario a few times from Simulate."
          />
        </Card>
      )}
    </>
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
