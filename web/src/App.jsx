import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import SuccessRateChart from './Chart.jsx'
import { api, connectStream, formatINR, formatClock, formatPct, severityClass } from './api.js'

const POLL_MS = 1500

const VIEW_KEYS = ['walkthrough', 'command', 'agents', 'benchmark']

/** The open tab lives in the URL hash so a section can be linked to directly. */
function readView() {
  const hash = typeof window !== 'undefined' ? window.location.hash.replace('#', '') : ''
  return VIEW_KEYS.includes(hash) ? hash : 'walkthrough'
}

export default function App() {
  const [metrics, setMetrics] = useState(null)
  const [series, setSeries] = useState([])
  const [incidents, setIncidents] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [scenarios, setScenarios] = useState([])
  const [events, setEvents] = useState([])
  const [evaluation, setEvaluation] = useState(null)
  const [agents, setAgents] = useState([])
  const [view, setView] = useState(readView)
  const [notice, setNotice] = useState(null)
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
    if (readView() !== view) window.location.hash = view
    const onHash = () => setView(readView())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [view])

  useEffect(() => {
    api.scenarios().then((d) => setScenarios(d.scenarios)).catch(() => {})
    api.evaluation().then(setEvaluation).catch(() => setEvaluation(null))
    api.agents().then((d) => setAgents(d.agents)).catch(() => {})
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

  // Clicking inject used to give no sign that anything had happened: detection takes a few
  // seconds of simulated traffic, so the page sat unchanged and the button looked broken.
  const inject = async (scenario) => {
    setNotice({
      kind: 'injected',
      text: `Injected "${scenario.name}". Detection needs a few seconds of traffic — the incident
             will appear on the right when three independent tests agree.`,
    })
    await act(() => api.trigger(scenario.scenario_id))
  }

  const reset = async () => {
    setNotice({ kind: 'reset', text: 'Cleared every running scenario and incident.' })
    await act(() => api.control('reset'))
    setSelectedId(null)
    setDetail(null)
    pinned.current = false
  }

  // Once an incident exists it becomes the subject of the page rather than one panel of six.
  const focused = Boolean(detail && incidents.length)
  const live = incidents.find((i) => i.awaiting_approval) || incidents[0] || null

  return (
    <div className="app">
      <Topbar metrics={metrics} status={status} view={view} setView={setView} />

      {error ? (
        <div className="approval" role="alert">
          <h3>Connection problem</h3>
          <p>{error}</p>
        </div>
      ) : null}

      {view === 'walkthrough' ? (
        <Walkthrough
          scenarios={scenarios}
          incidents={incidents}
          incident={detail}
          metrics={metrics}
          series={series}
          report={evaluation}
          events={events}
          busy={busy}
          notice={notice}
          onInject={inject}
          onReset={reset}
          onApprove={() => act(() => api.approve(detail.incident_id))}
          onReject={() => act(() => api.reject(detail.incident_id))}
        />
      ) : null}

      {view === 'command' ? (
        <>
          {focused ? null : (
            <Hero
              report={evaluation}
              busy={busy}
              onDemo={() => {
                const s = scenarios.find((x) => x.scenario_id === 'SCN-UPI-PSP-BADFALLBACK')
                return s ? inject(s) : act(() => api.trigger('SCN-UPI-PSP-BADFALLBACK'))
              }}
            />
          )}

          <Tiles metrics={metrics} series={series} />

          {focused ? (
            <>
              {incidents.length > 1 ? (
                <div className="incident-switch">
                  {incidents.slice(0, 6).map((i) => (
                    <button
                      key={i.incident_id}
                      className={`switch-chip ${i.incident_id === selectedId ? 'selected' : ''}`}
                      onClick={() => {
                        pinned.current = true
                        setSelectedId(i.incident_id)
                      }}
                    >
                      <span className={`badge ${severityClass(i.severity)}`}>{i.severity}</span>
                      {i.incident_id} · {shortState(i)}
                    </button>
                  ))}
                </div>
              ) : null}

              <IncidentPanel
                incident={detail}
                busy={busy}
                onApprove={() => act(() => api.approve(detail.incident_id))}
                onReject={() => act(() => api.reject(detail.incident_id))}
              />

              <div className="grid">
                <section className="panel">
                  <div className="panel-head">
                    <h2>Payment success rate</h2>
                    <span className="clock">
                      {metrics ? `${metrics.transactions} payments in window` : ''}
                    </span>
                  </div>
                  <div className="panel-body">
                    <SuccessRateChart points={series} baseline={metrics?.baseline_success_rate} />
                  </div>
                </section>

                <section className="panel">
                  <div className="panel-head">
                    <h2>What the agents are doing</h2>
                    <span className="clock">{status}</span>
                  </div>
                  <div className="panel-body tight">
                    <EventFeed events={events} />
                  </div>
                </section>
              </div>

              <ScenarioPanel
                scenarios={scenarios}
                busy={busy}
                notice={notice}
                onTrigger={inject}
                onReset={reset}
                collapsed
              />
            </>
          ) : (
            <div className="grid">
              <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
                <section className="panel">
                  <div className="panel-head">
                    <h2>Payment success rate</h2>
                    <span className="clock">
                      {metrics ? `${metrics.transactions} payments in window` : ''}
                    </span>
                  </div>
                  <div className="panel-body">
                    <SuccessRateChart points={series} baseline={metrics?.baseline_success_rate} />
                  </div>
                </section>

                <section className="panel">
                  <div className="panel-head">
                    <h2>What the agents are doing</h2>
                    <span className="clock">{status}</span>
                  </div>
                  <div className="panel-body tight">
                    <EventFeed events={events} />
                  </div>
                </section>
              </div>

              <ScenarioPanel
                scenarios={scenarios}
                busy={busy}
                notice={notice}
                onTrigger={inject}
                onReset={reset}
              />
            </div>
          )}
        </>
      ) : null}

      {view === 'agents' ? <AgentFleet agents={agents} /> : null}

      {view === 'benchmark' ? (
        <>
          <EvaluationPanel report={evaluation} />
          <p className="note">
            Every figure on this page is produced by the running system. Benchmark numbers come
            from <code>python -m pic.evaluation.harness</code> and are read from its JSON output —
            none are written by hand.
          </p>
        </>
      ) : null}

      <Footer report={evaluation} />
    </div>
  )
}

/* ---------------------------------------------------------- walkthrough */

const STEPS = [
  ['Choose a situation', 'Pick something that can go wrong with payments.'],
  ['The agents investigate', 'They look for what changed, price it, and work out why.'],
  ['You decide', 'Anything risky stops here and waits for a person.'],
  ['What came of it', 'The action is measured, and undone if it did not help.'],
]

/** Which step the visitor is on, derived from the incident rather than from clicks, so the page
 *  cannot claim to be somewhere the system is not. */
function currentStep(incident) {
  if (!incident) return 0
  if (incident.state === 'AWAITING_HUMAN_APPROVAL') return 2
  if (incident.state === 'CLOSED') return 3
  return 1
}

function Walkthrough(props) {
  const { scenarios, incident, metrics, series, report, events, busy, notice } = props
  const step = currentStep(incident)
  return (
    <section className="walk">
      <Stepper step={step} />
      {step === 0 ? <StepChoose {...props} /> : null}
      {step === 1 ? <StepWorking incident={incident} events={events} series={series} metrics={metrics} /> : null}
      {step === 2 ? <StepDecide {...props} /> : null}
      {step === 3 ? <StepOutcome {...props} /> : null}
    </section>
  )
}

function Stepper({ step }) {
  return (
    <ol className="stepper" aria-label="Progress">
      {STEPS.map(([label], i) => (
        <li key={label} className={`step ${i === step ? 'now' : i < step ? 'past' : 'todo'}`}>
          <span className="step-n">{i < step ? '✓' : i + 1}</span>
          <span className="step-l">{label}</span>
        </li>
      ))}
    </ol>
  )
}

function StepChoose({ scenarios, busy, notice, onInject, report }) {
  const d = report?.detection
  return (
    <div className="walk-body">
      <h2>Payments are failing. Something has to work out why, and quickly.</h2>
      <p className="walk-lede">
        This runs a live simulation of a merchant taking payments. Break something below and watch
        eight agents observe it, investigate, price the damage, diagnose the cause, ask a
        deterministic policy gateway for permission, act — and then check whether their own fix
        actually worked.
        {d ? ` Across a reproducible benchmark it detects with ${formatPct(d.precision, 1)} precision.` : ''}
      </p>
      {notice ? <div className={`notice ${notice.kind}`}>{notice.text}</div> : null}
      <h3 className="walk-h3">Pick a situation</h3>
      <div className="scenario-grid">
        {scenarios.map((s) => {
          const tag = scenarioTag(s)
          return (
            <button
              key={s.scenario_id}
              className={`btn scenario ${s.active ? 'running' : ''}`}
              disabled={busy}
              title={s.description}
              onClick={() => onInject(s)}
            >
              <span className="s-head">
                <span className="s-name">{s.name}</span>
                {s.active ? (
                  <span className="badge medium">running</span>
                ) : tag ? (
                  <span className={`badge ${tag.tone}`}>{tag.text}</span>
                ) : null}
              </span>
              <span className="s-desc">{s.description.slice(0, 118)}…</span>
            </button>
          )
        })}
      </div>
      <p className="walk-foot">
        Nothing here is scripted. The same detector, agents, policy gateway and tools run whether
        you are watching or the benchmark is.
      </p>
    </div>
  )
}

function StepWorking({ incident, events, series, metrics }) {
  const stages = buildStages(incident)
  const active = stages.find((s) => s.status === 'active')
  return (
    <div className="walk-body">
      <h2>{active ? `${active.title}…` : 'Working the incident…'}</h2>
      <p className="walk-lede">
        {incident.incident_id} is open. Each agent has one job and hands on a typed result; the
        page below updates as they finish.
      </p>
      <StageStrip stages={stages} />
      <IncidentStory incident={incident} />
      <div className="grid">
        <section className="panel">
          <div className="panel-head"><h2>Payment success rate</h2></div>
          <div className="panel-body">
            <SuccessRateChart points={series} baseline={metrics?.baseline_success_rate} />
          </div>
        </section>
        <section className="panel">
          <div className="panel-head"><h2>What the agents are doing</h2></div>
          <div className="panel-body tight"><EventFeed events={events} /></div>
        </section>
      </div>
    </div>
  )
}

function StepDecide({ incident, busy, onApprove, onReject }) {
  const p = incident.proposal
  const pd = incident.policy_decision
  return (
    <div className="walk-body">
      <h2>The agents want to act. This one is your call.</h2>
      <p className="walk-lede">
        The policy gateway is plain Python evaluating the merchant's own rules — no model is
        involved in this decision, and the Action Agent physically cannot run a write tool without
        it. Here it stopped and asked for a person.
      </p>
      <IncidentStory incident={incident} />
      <div className="approval">
        <h3>⚠ {readableAction(p?.action)} {paramText(pd?.granted_parameters)}</h3>
        <p>{pd?.reason}</p>
        {p?.rationale ? <p className="walk-rationale">{p.rationale}</p> : null}
        <div className="actions">
          <button className="btn primary lg" disabled={busy} onClick={onApprove}>
            Approve and execute
          </button>
          <button className="btn danger lg" disabled={busy} onClick={onReject}>
            Reject
          </button>
        </div>
      </div>
      <p className="walk-foot">
        Either choice is recorded in the audit trail against your name. Rejecting hands the
        incident to a human with the evidence attached.
      </p>
    </div>
  )
}

function StepOutcome({ incident, busy, onReset, report }) {
  const v = incident.verification
  const rel = report?.reliability
  return (
    <div className="walk-body">
      <h2>{outcomeHeadline(incident)}</h2>
      <StageStrip stages={buildStages(incident)} />
      <OutcomeBanner incident={incident} />
      <IncidentStory incident={incident} />
      <div className="walk-points">
        <div>
          <strong>It measured its own work.</strong>
          {v?.control_used
            ? ' Against a control group deliberately left alone, so the incident recovering on its own could not be mistaken for the fix working.'
            : ' Before and after the change, and reported the result whichever way it went.'}
        </div>
        <div>
          <strong>It stopped where it should.</strong>
          {rel
            ? ` Across the benchmark: ${rel.policy_violations} policy violations and ${rel.unauthorised_executions} unauthorised executions.`
            : ' Nothing ran without a policy decision naming that exact action.'}
        </div>
      </div>
      <div className="actions">
        <button className="btn primary lg" disabled={busy} onClick={onReset}>
          Try another situation
        </button>
      </div>
      <details className="tech">
        <summary>Show the working — evidence, arithmetic and statistics</summary>
        <IncidentPanel incident={incident} busy={busy} onApprove={() => {}} onReject={() => {}} />
      </details>
    </div>
  )
}

function outcomeHeadline(incident) {
  const v = incident.verification
  if ((incident.audit || []).some((a) => String(a.approved_by || '').startsWith('policy_engine:rollback')))
    return 'The fix did not work, so the agent undid it.'
  if (v?.status === 'RECOVERED') return 'Payments recovered, and it proved it.'
  if (v?.status === 'PARTIALLY_RECOVERED') return 'Partly recovered — and it says so rather than claiming victory.'
  if (incident.escalation) return 'It stopped and handed the incident to a human.'
  return 'The incident is closed.'
}

/* --------------------------------------------------------------- hero/nav */

function Hero({ report, busy, onDemo }) {
  const d = report?.detection
  const r = report?.reliability
  const stats = [
    {
      value: d ? formatPct(d.precision, 1) : '—',
      label: 'detection precision',
      foot: d
        ? `${d.false_positives} false positives in ${d.false_positives + d.true_negatives} healthy windows`
        : 'run the benchmark to populate',
    },
    {
      value: r ? r.policy_violations : '—',
      label: 'policy violations',
      foot: 'the model cannot call a write tool',
    },
    {
      value: r ? formatPct(r.rollback_success_rate, 0) : '—',
      label: 'rollback success',
      foot: 'failed fixes are undone, not retried',
    },
  ]
  return (
    <section className="hero">
      <div className="hero-copy">
        <span className="eyebrow">Razorpay AI Buildathon · autonomous payment operations</span>
        <h2>
          It does not just detect. It acts, <em>checks its own work</em>, and undoes the fix when
          the fix did not help.
        </h2>
        <p>
          Every action passes a deterministic policy gateway before it runs, and is measured
          against a concurrent control group after it runs. When the numbers do not improve, the
          agent reverts its own change and hands the incident to a human rather than declaring
          victory.
        </p>
        <div className="hero-actions">
          <button className="btn primary lg" disabled={busy} onClick={onDemo}>
            ▶ Run the failed-intervention demo
          </button>
          <span className="hero-hint">
            injects a network-wide UPI failure that rerouting cannot repair, then watch the agent
            measure its own work
          </span>
        </div>
      </div>
      <div className="hero-stats">
        {stats.map((s) => (
          <div className="hero-stat" key={s.label}>
            <span className="hs-value">{s.value}</span>
            <span className="hs-label">{s.label}</span>
            <span className="hs-foot">{s.foot}</span>
          </div>
        ))}
      </div>
    </section>
  )
}

const PIPELINE = [
  'observe',
  'detect',
  'investigate',
  'diagnose',
  'decide',
  'policy gate',
  'act',
  'verify',
]

function AgentFleet({ agents }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Agent fleet</h2>
        <span className="clock">{agents.length} agents · one responsibility each</span>
      </div>
      <div className="panel-body">
        <div className="pipeline">
          {PIPELINE.map((step, i) => (
            <span key={step} className="pipe-step">
              {step}
              {i < PIPELINE.length - 1 ? <i className="pipe-arrow">→</i> : null}
            </span>
          ))}
        </div>
        <div className="agent-grid">
          {agents.map((a, i) => (
            <div className={`agent-card ${a.writes ? 'writer' : ''}`} key={a.name}>
              <div className="a-head">
                <span className="a-index">{String(i + 1).padStart(2, '0')}</span>
                <span className="a-name">{a.name.replace(/_/g, ' ')}</span>
                <span className={`badge ${a.writes ? 'high' : 'good'}`}>
                  {a.writes ? 'write · gated' : 'read only'}
                </span>
              </div>
              <p className="a-summary">{a.summary}</p>
              <div className="a-foot">
                <code>{a.state}</code>
                <span>timeout {a.timeout_s}s</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}

function Footer({ report }) {
  return (
    <footer className="footer">
      <span>
        <strong>Payment Incident Commander</strong> — built for the Razorpay AI Buildathon
      </span>
      <span className="footer-right">
        {report?.reasoner ? `${report.reasoner} reasoner` : 'live simulation'}
        {report?.seeds ? ` · seeds ${report.seeds.join(', ')}` : ''}
      </span>
    </footer>
  )
}

/* ------------------------------------------------------------------ parts */

const VIEWS = [
  ['walkthrough', 'Walkthrough'],
  ['command', 'Console'],
  ['agents', 'Agent Fleet'],
  ['benchmark', 'Benchmark'],
]

function Topbar({ metrics, status, view, setView }) {
  const agent = metrics?.agent_status || 'Starting…'
  const tone =
    status !== 'connected' ? 'offline' : agent === 'Monitoring' ? '' : agent.includes('Escalated') ? 'alert' : 'busy'
  return (
    <header className="topbar">
      <div className="brand">
        <h1>Payment Command Center</h1>
        <span className="sub">autonomous payment incident response</span>
      </div>
      <nav className="tabs" aria-label="Sections">
        {VIEWS.map(([key, label]) => (
          <button
            key={key}
            className={`tab ${view === key ? 'active' : ''}`}
            aria-current={view === key ? 'page' : undefined}
            onClick={() => setView(key)}
          >
            {label}
          </button>
        ))}
      </nav>
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

/** A sparkline drawn by hand rather than pulled from a charting library: seven-odd points of
 *  recent history, so a tile shows direction and not only level. */
function Sparkline({ points, tone = 'neutral', width = 132, height = 30 }) {
  const values = (points || [])
    .filter((p) => p.transactions > 0)
    .slice(-24)
    .map((p) => p.success_rate)
  if (values.length < 2) return null
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const span = hi - lo || 1
  const step = width / (values.length - 1)
  const y = (v) => height - 3 - ((v - lo) / span) * (height - 6)
  const line = values.map((v, i) => `${i === 0 ? 'M' : 'L'}${(i * step).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const area = `${line} L${width},${height} L0,${height} Z`
  const id = `spark-${tone}`
  return (
    <svg className={`spark ${tone}`} width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="currentColor" stopOpacity="0.28" />
          <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${id})`} />
      <path d={line} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  )
}

function Tiles({ metrics, series }) {
  const deviation = metrics?.deviation ?? 0
  const down = deviation < -0.005
  return (
    <div className="tiles">
      <div className="tile">
        <span className="label">Payment success</span>
        <span className="value">{formatPct(metrics?.success_rate)}</span>
        <span className={`foot trend ${down ? 'down' : 'up'}`}>
          <i className="dot" />
          {formatPct(Math.abs(deviation), 1)} vs baseline {formatPct(metrics?.baseline_success_rate)}
        </span>
        <Sparkline points={series} tone={down ? 'down' : 'up'} />
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
  if (s.fallback_healthy === false) return { text: 'the fix cannot work', tone: 'high' }
  if (s.recommended_action === 'escalate') return { text: 'hands to a human', tone: 'medium' }
  return null
}

function ScenarioPanel({ scenarios, busy, notice, onTrigger, onReset, collapsed = false }) {
  const running = scenarios.filter((s) => s.active).length
  return (
    <section className={`panel ${collapsed ? 'aside' : ''}`}>
      <div className="panel-head">
        <h2>Inject an incident</h2>
        <div className="head-actions">
          <span className="clock">
            {running ? `${running} running` : `${scenarios.length} scenarios`}
          </span>
          <button className="btn small" disabled={busy || !running} onClick={onReset}>
            Reset
          </button>
        </div>
      </div>
      <div className="panel-body">
        <p className="panel-lede">
          Pick one. The agents will detect it within a few seconds of simulated traffic, then work
          the incident on the right. Injecting several at once stacks the failures and makes the
          diagnosis genuinely ambiguous — use Reset to get back to a clean baseline.
        </p>
        {notice ? <div className={`notice ${notice.kind}`}>{notice.text}</div> : null}
        <div className="scenario-grid">
          {scenarios.map((s) => {
            const tag = scenarioTag(s)
            return (
              <button
                key={s.scenario_id}
                className={`btn scenario ${s.active ? 'running' : ''}`}
                disabled={busy}
                title={s.description}
                onClick={() => onTrigger(s)}
              >
                <span className="s-head">
                  <span className="s-name">{s.name}</span>
                  {s.active ? (
                    <span className="badge medium">running</span>
                  ) : tag ? (
                    <span className={`badge ${tag.tone}`}>{tag.text}</span>
                  ) : null}
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
        <StageStrip stages={stages} />
        <IncidentStory incident={incident} />
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

        <details className="tech">
          <summary>Show the working — evidence, arithmetic and statistics</summary>
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
        </details>
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

/** Granted parameters as prose. `{"max_shift_pct": 25}` reads as "max shift pct 25".
 *
 *  Long values are dropped rather than truncated. A ticket carries its whole title and
 *  description in its parameters, and printing them turned the heading of the approval prompt
 *  into a paragraph - the reader needs to know *what* is being approved, and the body text
 *  already says why.
 */
const PARAM_VALUE_LIMIT = 40

function paramText(params) {
  const entries = Object.entries(params || {})
    .filter(([, v]) => v !== null && v !== undefined && String(v).length <= PARAM_VALUE_LIMIT)
    .slice(0, 3)
  if (!entries.length) return ''
  return entries.map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`).join(', ')
}

/** Where the agent has got to, as eight dots rather than eight paragraphs. */
function StageStrip({ stages }) {
  return (
    <ol className="strip" aria-label="Agent progress">
      {stages.map((stage) => (
        <li key={stage.key} className={`strip-step ${stage.status}`} title={stage.title}>
          <span className="strip-dot" />
          <span className="strip-label">{stage.title}</span>
        </li>
      ))}
    </ol>
  )
}

/** The incident in plain English, built only from what the agents actually recorded.
 *
 *  The stage list below is the evidence, and it is written for someone who already knows what a
 *  two-proportion test is. Anyone meeting the system for the first time needs the account first
 *  and the arithmetic second, so this says what happened in the order a person would ask.
 */
function IncidentStory({ incident }) {
  const a = incident.anomaly
  const rc = incident.root_cause
  const im = incident.impact
  const p = incident.proposal
  const pd = incident.policy_decision
  const v = incident.verification
  const beats = []

  if (a) {
    beats.push({
      q: 'What happened',
      a: `Payments fell to ${formatPct(a.current_value)}, against a normal rate of
          ${formatPct(a.baseline)}. Three independent checks agreed before an incident was opened.`,
    })
  }
  if (rc) {
    beats.push({
      q: 'What the agents think caused it',
      a: `${rc.most_likely_root_cause}. ${confidenceInWords(rc.confidence)}${
        rc.ambiguous ? ', and the evidence does not clearly separate the top two explanations' : ''
      }.`,
    })
  }
  if (im) {
    beats.push({
      q: 'What it was costing',
      a: `About ${formatINR(im.revenue_at_risk_per_hour_paise)} an hour, from roughly
          ${im.transactions_at_risk_per_hour.toLocaleString()} extra failed payments affecting
          ${im.affected_customers_estimate.toLocaleString()} customers an hour.`,
    })
  }
  if (p && pd) {
    beats.push({
      q: 'What it proposed, and what the policy allowed',
      a: `${readableAction(p.action)}${paramText(pd.granted_parameters) ? ` (${paramText(pd.granted_parameters)})` : ''}. ${policyInWords(pd)}`,
    })
  }
  if (v) {
    beats.push({ q: 'Did it work', a: verificationInWords(v) })
  } else if (incident.escalation && incident.state !== 'AWAITING_HUMAN_APPROVAL') {
    // While the decision is still open the incident has not ended, whatever the escalation
    // record says - claiming otherwise contradicts the buttons directly underneath.
    beats.push({
      q: 'How it ended',
      a: `Handed to a human. ${incident.escalation.reason}`,
    })
  }
  if (!beats.length) return null

  return (
    <div className="story">
      <h3>In plain English</h3>
      {beats.map((b) => (
        <p key={b.q}>
          <span className="story-q">{b.q}</span>
          {b.a}
        </p>
      ))}
    </div>
  )
}

function confidenceInWords(confidence) {
  if (confidence === null || confidence === undefined) return 'Confidence was not recorded'
  const pct = `${Math.round(confidence * 100)}%`
  if (confidence >= 0.7) return `It is confident (${pct})`
  if (confidence >= 0.5) return `It is moderately confident (${pct})`
  return `It is not confident (${pct}), which is why it leans on a human`
}

function policyInWords(decision) {
  if (decision.requires_human) {
    return `The policy gateway would not let this run unattended, so it asked a person first — ${decision.reason}`
  }
  if (!decision.approved) {
    return `The policy gateway refused it — ${decision.reason}`
  }
  if (decision.outcome === 'APPROVE_WITH_CLAMP') {
    return `The policy gateway allowed it but capped how far it could go — ${decision.reason}`
  }
  return `The policy gateway allowed it — ${decision.reason}`
}

function verificationInWords(v) {
  const measured = v.control_used
    ? `Measured against a control group that was deliberately left alone, so the incident's own
       recovery cannot be mistaken for the fix working.`
    : `Measured before and after the change.`
  if (v.status === 'RECOVERED') return `Yes. ${measured} ${v.explanation}`
  if (v.status === 'PARTIALLY_RECOVERED')
    return `Partly. ${measured} The incident stays open because payments have not fully returned to normal. ${v.explanation}`
  if (v.status === 'REGRESSED')
    return `No — it made things worse, so the change was undone. ${measured} ${v.explanation}`
  return `No. ${measured} ${v.explanation}`
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
