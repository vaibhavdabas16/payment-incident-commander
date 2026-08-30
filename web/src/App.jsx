import { useCallback, useEffect, useRef, useState } from 'react'
import Landing from './Landing.jsx'
import { api, connectStream, formatClock } from './api.js'
import { Led } from './components.jsx'
import { Agents, Benchmark, CommandCenter, Health, Incidents, Investigation } from './pages.jsx'
import Simulate from './Simulate.jsx'

const POLL_MS = 1500

const NAV = [
  ['command', 'Command Center', 'ico-grid'],
  ['incidents', 'Incidents', 'ico-pulse'],
  ['simulate', 'Simulate', 'ico-time'],
  ['investigation', 'AI Investigation', 'ico-agent'],
  ['health', 'Payment Health', 'ico-heart'],
  ['analytics', 'Analytics', 'ico-chart'],
]

const PAGE_KEYS = [...NAV.map(([k]) => k), 'agents']

const ROUTE = '#/app/'

function readPage() {
  if (typeof window === 'undefined') return 'command'
  const hash = window.location.hash
  const key = hash.startsWith(ROUTE) ? hash.slice(ROUTE.length) : ''
  return PAGE_KEYS.includes(key) ? key : 'command'
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
    const onHash = () => { setEntered(readEntered()); setPage(readPage()) }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    if (entered && window.location.hash !== ROUTE + page) window.location.hash = ROUTE + page
  }, [page, entered])

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
        onLaunch={() => { window.location.hash = ROUTE + 'command'; setEntered(true); setPage('command'); window.scrollTo({ top: 0 }) }}
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
          <span className="mark" aria-hidden="true" />
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

        <div className="nav-group">
          <div className="nav-label">System</div>
          <button className={`nav-item ${page === 'agents' ? 'on' : ''}`} onClick={() => setPage('agents')}>
            <span className="ico ico-agent" aria-hidden="true" />
            <span>Agent Fleet</span>
          </button>
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
              selectedId={selectedId}
              onSelect={(id) => { pinned.current = true; setSelectedId(id) }}
              onOpen={open} onApprove={approve} onReject={reject} onGoto={setPage}
            />
          ) : null}
          {page === 'incidents' ? (
            <Incidents {...shared} selectedId={selectedId} onOpen={openInList} onApprove={approve} onReject={reject} />
          ) : null}
          {page === 'investigation' ? <Investigation incident={detail} agents={agents} busy={busy} onApprove={approve} onReject={reject} /> : null}
          {page === 'health' ? <Health health={health} metrics={metrics} series={series} /> : null}
          {page === 'simulate' ? (
            <Simulate
              scenarios={scenarios} options={simOptions} busy={busy} notice={notice}
              onInject={inject} onCustom={runCustom} onReset={reset} onGoto={setPage}
            />
          ) : null}
          {page === 'analytics' ? <Benchmark report={evaluation} /> : null}
          {page === 'agents' ? <Agents agents={agents} /> : null}
        </main>
      </div>
    </div>
  )
}
