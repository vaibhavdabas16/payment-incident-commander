import { useCallback, useEffect, useRef, useState } from 'react'
import Landing from './Landing.jsx'
import { api, connectStream, formatClock } from './api.js'
import { Led } from './components.jsx'
import { CommandCenter, Health, Incidents, Proof } from './pages.jsx'
import Mark from './Mark.jsx'
import Simulate from './Simulate.jsx'

const POLL_MS = 1500

const NAV = [
  ['command', 'Overview', 'ico-grid'],
  ['incidents', 'Incidents', 'ico-pulse'],
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
  const [events, setEvents] = useState([])
  const [evaluation, setEvaluation] = useState(null)
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

  /* -------------------------------------------------------------- polling */

  const refresh = useCallback(async () => {
    try {
      const [m, s, i, h] = await Promise.all([
        api.metrics(), api.series(60, 45), api.incidents(), api.health().catch(() => null),
      ])
      setMetrics(m)
      setSeries(s.points)
      setIncidents(i.incidents)
      if (h) setHealth(h)
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
    api.scenarios().then((d) => setScenarios(d.scenarios)).catch(() => {})
    api.agents().then((d) => setAgents(d.agents)).catch(() => {})
    api.evaluation().then(setEvaluation).catch(() => setEvaluation(null))
    api.simulationOptions().then(setSimOptions).catch(() => {})
  }, [])

  useEffect(() => {
    if (!selectedId) return
    let stale = false
    const load = () => api.incident(selectedId).then((d) => !stale && setDetail(d)).catch(() => {})
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

  const inject = async (scenario) => {
    setNotice({
      kind: 'injected',
      text: `Injected "${scenario.name}". Detection needs a few seconds of traffic — the incident appears once three independent tests agree.`,
    })
    await act(() => api.trigger(scenario.scenario_id))
  }

  const reset = async () => {
    setNotice({ kind: 'reset', text: 'Cleared every running scenario and incident.' })
    await act(() => api.control('reset'))
    setSelectedId(null); setDetail(null); pinned.current = false
  }

  const runCustom = async (body) => {
    setNotice({ kind: 'injected', text: 'Injected your incident. Detection needs a few seconds of traffic.' })
    await act(() => api.customScenario(body))
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
          const s = scenarios.find((x) => x.scenario_id === HEADLINE_SCENARIO)
          window.location.hash = ROUTE + 'command'
          setEntered(true)
          setPage('command')
          window.scrollTo({ top: 0 })
          if (s) inject(s)
        }}
      />
    )
  }

  const live = incidents.find((i) => i.state !== 'CLOSED') || null
  const shared = { metrics, series, incidents, incident: detail, agents, health, events, scenarios, busy, notice, error }

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
            <b>Incident Commander</b>
            <span>AI payment operations</span>
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
              onRunHeadline={() => {
                const s = scenarios.find((x) => x.scenario_id === HEADLINE_SCENARIO)
                if (s) inject(s)
              }}
              selectedId={selectedId}
              onSelect={(id) => { pinned.current = true; setSelectedId(id) }}
              onOpen={open} onApprove={approve} onReject={reject} onGoto={setPage}
            />
          ) : null}
          {page === 'incidents' ? (
            <Incidents {...shared} selectedId={selectedId} onOpen={openInList} onApprove={approve} onReject={reject} />
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
