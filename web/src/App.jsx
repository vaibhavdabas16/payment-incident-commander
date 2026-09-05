import { useCallback, useEffect, useRef, useState } from 'react'
import Landing from './Landing.jsx'
import { api, connectStream, formatClock } from './api.js'
import { Led } from './components.jsx'
import { CommandCenter, Health, Incidents, Learning, Proof } from './pages.jsx'
import Mark from './Mark.jsx'
import Simulate from './Simulate.jsx'

const POLL_MS = 1500
// Segment health is a five-minute window, so refetching it on the live cadence redrew identical
// bars while re-scanning five minutes of events per payment method, provider and issuer on the
// server — comfortably the most expensive request the dashboard made, for no visible difference.
const HEALTH_POLL_MS = 6000
// Learning only changes when an incident closes.
const LEARNING_POLL_MS = 6000

const NAV = [
  ['command', 'Overview', 'ico-grid'],
  ['incidents', 'Incidents', 'ico-pulse'],
  // What the system has learned and what it wants to prevent. Its own section because it is the
  // only view that is about the system across incidents rather than about one of them.
  ['learning', 'Learning', 'ico-agent'],
  ['simulate', 'Simulate', 'ico-time'],
  ['health', 'Payment Health', 'ico-heart'],
  // How it works and how well it works are the same question for a visitor, and neither is a
  // place you do anything — they were two nav items pretending to be workspaces.
  ['proof', 'How it works', 'ico-chart'],
]

const PAGE_KEYS = NAV.map(([k]) => k)

// The scenario the landing page starts, because it is the one that shows the whole claim: the
// obvious fix cannot work, so the agent measures, reverts and hands over.
const HEADLINE_SCENARIO = 'SCN-UPI-PSP-BADFALLBACK'

const ROUTE = '#/app/'

function readRoute() {
  if (typeof window === 'undefined') return { page: 'command', incident: null }
  const hash = window.location.hash
  const rest = hash.startsWith(ROUTE) ? hash.slice(ROUTE.length) : ''
  const [key, id] = rest.split('/')
  return {
    page: PAGE_KEYS.includes(key) ? key : 'command',
    // An incident carries its own address, so a link can point at one rather than at a list.
    incident: key === 'incidents' && id ? decodeURIComponent(id) : null,
  }
}

function readPage() {
  return readRoute().page
}

function readEntered() {
  return typeof window !== 'undefined' && window.location.hash.startsWith(ROUTE)
}

export default function App() {
  const [metrics, setMetrics] = useState(null)
  const [series, setSeries] = useState([])
  const [incidents, setIncidents] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [scenarios, setScenarios] = useState([])
  const [agents, setAgents] = useState([])
  const [health, setHealth] = useState(null)
  const [simOptions, setSimOptions] = useState(null)
  const [evaluation, setEvaluation] = useState(null)
  const [learning, setLearning] = useState(null)
  const [trace, setTrace] = useState(null)
  const [status, setStatus] = useState('connecting')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [page, setPage] = useState(readPage)
  const linkedIncident = useRef(readRoute().incident)
  const [collapsed, setCollapsed] = useState(false)
  // The landing page is the front door; a deep link to any page skips it.
  const [entered, setEntered] = useState(readEntered)
  const pinned = useRef(false)

  /* ---------------------------------------------------------- live stream */

  /* The socket is what makes the header honest about whether the page is still connected to a
   * running world. Its payloads used to feed an activity feed that no page renders any more, so
   * they are read and dropped; the per-incident step trace shows the same work, sourced from the
   * incident record rather than from a buffer that a reconnect would lose. */
  useEffect(() => connectStream(() => {}, setStatus), [])

  /* -------------------------------------------------------------- polling */

  const refresh = useCallback(async () => {
    try {
      const [m, s, i] = await Promise.all([api.metrics(), api.series(60, 45), api.incidents()])
      setMetrics(m)
      setSeries(s.points)
      setIncidents(i.incidents)
      setError(null)
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
    const load = () => api.health().then(setHealth).catch(() => {})
    load()
    const timer = setInterval(load, HEALTH_POLL_MS)
    return () => clearInterval(timer)
  }, [])

  /* What the system has learned only changes when an incident closes, so it is polled on the slow
   * cadence rather than the live one - it is a growing record, not a moving number. */
  useEffect(() => {
    const load = () => api.learning().then(setLearning).catch(() => {})
    load()
    const timer = setInterval(load, LEARNING_POLL_MS)
    return () => clearInterval(timer)
  }, [])

  useEffect(() => {
    api.scenarios().then((d) => setScenarios(d.scenarios)).catch(() => {})
    api.agents().then((d) => setAgents(d.agents)).catch(() => {})
    api.evaluation().then(setEvaluation).catch(() => setEvaluation(null))
    api.simulationOptions().then(setSimOptions).catch(() => {})
  }, [])

  useEffect(() => {
    if (!selectedId) return
    let stale = false
    const load = () =>
      Promise.all([api.incident(selectedId), api.trace(selectedId)])
        .then(([d, t]) => {
          if (stale) return
          setDetail(d)
          setTrace(t)
        })
        .catch(() => {})
    load()
    const timer = setInterval(load, POLL_MS)
    return () => { stale = true; clearInterval(timer) }
  }, [selectedId])

  useEffect(() => {
    const onHash = () => {
      const route = readRoute()
      setEntered(readEntered())
      setPage(route.page)
      if (route.incident) { pinned.current = true; setSelectedId(route.incident) }
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  // The URL follows the incident, so a specific one can be linked to and reloaded.
  useEffect(() => {
    if (!entered) return
    const want = ROUTE + page + (page === 'incidents' && selectedId ? `/${selectedId}` : '')
    if (window.location.hash !== want) window.location.hash = want
  }, [page, entered, selectedId])

  useEffect(() => {
    if (linkedIncident.current) { setSelectedId(linkedIncident.current); linkedIncident.current = null }
  }, [])

  /* -------------------------------------------------------------- actions */

  const act = async (fn) => {
    setBusy(true)
    try { await fn(); await refresh() } catch (err) { setError(String(err.message || err)) } finally { setBusy(false) }
  }

  /** Start the scenario the landing page and the idle state both promise.
   *
   *  Prefers the fetched scenario for its name in the notice, but the id is a constant and the
   *  endpoint takes the id, so a missing list is no reason to refuse to act. */
  const runHeadline = async () => {
    const known = scenarios.find((x) => x.scenario_id === HEADLINE_SCENARIO)
    if (known) return inject(known)
    setNotice({
      kind: 'injected',
      text: 'Injected the scenario. Detection needs a few seconds of traffic \u2014 the incident appears once three independent tests agree.',
    })
    await act(() => api.trigger(HEADLINE_SCENARIO))
  }

  const inject = async (scenario) => {
    setNotice({
      kind: 'injected',
      text: `Injected "${scenario.name}". Detection needs a few seconds of traffic — the incident appears once three independent tests agree.`,
    })
    await act(() => api.trigger(scenario.scenario_id))
  }

  /** Run one of the moves a handover offers.
   *
   *  The step ids come from the server, so a step the backend does not offer cannot be invoked
   *  from here by editing the page - the endpoints refuse anything the incident is not in a state
   *  for, rather than trusting the button that called them.
   */
  const runStep = async (step) => {
    const id = detail?.incident_id || selectedId
    if (!id) return
    if (step.action === 'approve') return approve(id)
    if (step.action === 'reject') return reject(id)
    if (step.action === 'retry') {
      setNotice({ kind: 'injected', text: 'Looking again at the traffic since this opened\u2026' })
      return act(() => api.retryIncident(id))
    }
    if (step.action === 'acknowledge') {
      const note = window.prompt('Who is taking this, and what are you doing about it?', '')
      if (note === null) return
      setNotice({ kind: 'reset', text: 'Recorded. The incident stays in the audit trail.' })
      return act(() => api.acknowledge(id, note))
    }
    if (step.action === 'override') {
      // Required, not optional: an override is a decision somebody answers for.
      const reason = window.prompt('Why are you overriding the policy? This is recorded.', '')
      if (!reason) return
      return act(() => api.override(id, reason))
    }
  }

  const reset = async () => {
    setNotice({ kind: 'reset', text: 'Cleared every running scenario and incident.' })
    await act(() => api.control('reset'))
    setSelectedId(null); setDetail(null); setTrace(null); pinned.current = false
    api.learning().then(setLearning).catch(() => {})
  }

  const runCustom = async (body) => {
    setNotice({ kind: 'injected', text: 'Injected your incident. Detection needs a few seconds of traffic.' })
    await act(() => api.customScenario(body))
  }

  /** Record a decision on a preventive recommendation.
   *
   *  It records and nothing more: the endpoint cannot change merchant policy, and the card the
   *  button sits on says so. */
  /** Load the deterministic seeded history, so the learning surfaces have something to show.
   *
   *  Explicit and labelled: seeded records carry a flag the UI renders wherever they appear. It
   *  exists because a merchant who has run one incident has an empty playbook - honest, and a
   *  demonstration of nothing. */
  const seedHistory = async () => {
    setNotice({
      kind: 'reset',
      text: 'Loaded seeded demo history. It is marked as seeded everywhere it appears — the next incident of this kind will be decided against it.',
    })
    await act(async () => {
      await api.seedHistory()
      setLearning(await api.learning())
    })
  }

  const decidePrevention = async (recommendation, decision) => {
    setNotice({
      kind: 'reset',
      text:
        decision === 'accept'
          ? 'Recorded as accepted. Merchant policy is unchanged \u2014 applying it means editing the policy file.'
          : 'Dismissed. It will not be raised again unless the pattern changes.',
    })
    await act(async () => {
      await api.preventionDecision(recommendation.recommendation_id, decision)
      setLearning(await api.learning())
    })
  }

  const open = (id) => { pinned.current = true; setSelectedId(id); setPage('incidents') }
  // In the list, clicking the open row collapses it again.
  const openInList = (id) => { pinned.current = true; setSelectedId(id); if (!id) setDetail(null) }
  const approve = () => detail && act(() => api.approve(detail.incident_id))
  const reject = () => detail && act(() => api.reject(detail.incident_id))

  if (!entered) {
    return (
      <Landing
        report={evaluation}
        metrics={metrics}
        onEnter={() => { window.location.hash = ROUTE + 'command'; setEntered(true); setPage('command'); window.scrollTo({ top: 0 }) }}
        onWatch={() => {
          window.location.hash = ROUTE + 'command'
          setEntered(true)
          setPage('command')
          window.scrollTo({ top: 0 })
          runHeadline()
        }}
      />
    )
  }

  const live = incidents.find((i) => i.state !== 'CLOSED') || null
  const shared = { metrics, series, incidents, incident: detail, trace, agents, health, scenarios, busy, notice, error, learning }

  return (
    <div className={`shell ${collapsed ? 'collapsed' : ''}`}>
      <nav className="side" aria-label="Sections">
        <button
          className="side-top"
          title="Back to the overview"
          onClick={() => { history.replaceState(null, '', window.location.pathname); setEntered(false) }}
        >
          <Mark size={24} />
          <span className="side-name">
            <b>Payment Incident Commander</b>
            <span>Autonomous payment recovery</span>
          </span>
        </button>

        <div className="nav-group">
          <div className="nav-label">Operations</div>
          {NAV.map(([key, label, icon]) => (
            <button key={key} className={`nav-item ${page === key ? 'on' : ''}`} onClick={() => setPage(key)} aria-current={page === key ? 'page' : undefined}>
              <span className={`ico ${icon}`} aria-hidden="true" />
              <span>{label}</span>
            </button>
          ))}
        </div>

        <div className="side-foot">
          <button className="side-toggle" onClick={() => setCollapsed((c) => !c)}>
            {collapsed ? '›' : '‹  Collapse'}
          </button>
        </div>
      </nav>

      <div className="main">
        <header className="topbar">
          <div className="crumb">
            <b>{(NAV.find(([k]) => k === page)?.[1]) || 'Agent Fleet'}</b>
            {live ? <><span className="crumb-sep">/</span><span>{live.incident_id}</span></> : null}
          </div>
          <div className="topbar-right">
            <span className="pill mono">sim {formatClock(metrics?.timestamp)}</span>
            <span className="pill">Production</span>
            <span className="pill">
              <Led tone={status !== 'connected' ? '' : live ? 'crit' : 'agent'} live={status === 'connected'} />
              {status === 'connected' ? (metrics?.agent_status || 'AI agents monitoring') : 'Reconnecting…'}
            </span>

          </div>
        </header>

        <main className="content">
          {page === 'command' ? (
            <CommandCenter
              {...shared}
              onRunHeadline={runHeadline}
              selectedId={selectedId}
              onOpen={open} onApprove={approve} onGoto={setPage}
              onSeed={seedHistory}
            />
          ) : null}
          {page === 'incidents' ? (
            <Incidents
              {...shared} selectedId={selectedId} onOpen={openInList}
              onApprove={approve} onReject={reject} onStep={runStep} onGoto={setPage}
            />
          ) : null}
          {page === 'learning' ? (
            <Learning learning={learning} busy={busy} onDecide={decidePrevention} onSeed={seedHistory} />
          ) : null}
          {page === 'health' ? <Health health={health} metrics={metrics} series={series} /> : null}
          {page === 'simulate' ? (
            <Simulate
              scenarios={scenarios} options={simOptions} busy={busy} notice={notice}
              onInject={inject} onCustom={runCustom} onReset={reset} onGoto={setPage}
            />
          ) : null}
          {page === 'proof' ? <Proof report={evaluation} agents={agents} /> : null}
        </main>
      </div>
    </div>
  )
}
