// Thin API client. Same-origin in production (FastAPI serves the built bundle);
// the Vite dev server proxies /api and /ws to the backend.

/* Each visitor gets their own simulation on the server; this is the id that selects it. Kept in
 * localStorage so a reload rejoins the same world rather than starting a fresh one, and generated
 * client-side so no round trip is needed before the first request. */
const SESSION_KEY = 'pic.session'

function sessionId() {
  try {
    // An explicit ?session= in the page URL wins, so a live world can be shared: send someone the
    // link and they watch the same incident rather than a fresh, quiet simulation of their own.
    const shared = new URLSearchParams(window.location.search).get('session')
    if (shared) {
      const clean = shared.replace(/[^A-Za-z0-9_-]/g, '').slice(0, 64)
      if (clean) {
        localStorage.setItem(SESSION_KEY, clean)
        return clean
      }
    }
    let id = localStorage.getItem(SESSION_KEY)
    if (!id) {
      id = (crypto.randomUUID?.() || String(Math.random()).slice(2)).replace(/-/g, '').slice(0, 24)
      localStorage.setItem(SESSION_KEY, id)
    }
    return id
  } catch {
    // Private windows and blocked storage fall back to the shared world rather than failing.
    return 'public'
  }
}

export const SESSION = sessionId()

function withSession(path) {
  return path + (path.includes('?') ? '&' : '?') + 'session=' + encodeURIComponent(SESSION)
}

async function json(path, options) {
  path = withSession(path)
  const response = await fetch(path, options)
  if (!response.ok) {
    const body = await response.text().catch(() => '')
    throw new Error(`${response.status} ${response.statusText}: ${body.slice(0, 200)}`)
  }
  return response.json()
}

export const api = {
  health: () => json('/api/health'),
  metrics: () => json('/api/metrics'),
  series: (windowSeconds = 60, count = 60) =>
    json(`/api/series?window_seconds=${windowSeconds}&count=${count}`),
  incidents: () => json('/api/incidents'),
  incident: (id) => json(`/api/incidents/${id}`),
  scenarios: () => json('/api/scenarios'),
  agents: () => json('/api/agents'),
  health: () => json('/api/health/segments'),
  simulationOptions: () => json('/api/simulation/options'),
  customScenario: (body) =>
    json('/api/scenarios/custom', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  trigger: (id) => json(`/api/scenarios/${id}/trigger`, { method: 'POST' }),
  approve: (id) => json(`/api/incidents/${id}/approve`, { method: 'POST' }),
  reject: (id) => json(`/api/incidents/${id}/reject`, { method: 'POST' }),
  control: (command, speedup) =>
    json(`/api/control/${command}${speedup ? `?speedup=${speedup}` : ''}`, { method: 'POST' }),
  evaluation: () => json('/api/evaluation'),
  notifications: () => json('/api/notifications'),
}

// Formats paise the way an Indian merchant reads money: lakh and crore.
export function formatINR(paise, { compact = true } = {}) {
  if (paise === null || paise === undefined) return '—'
  const rupees = paise / 100
  if (!compact) return `₹${Math.round(rupees).toLocaleString('en-IN')}`
  const abs = Math.abs(rupees)
  if (abs >= 1e7) return `₹${(rupees / 1e7).toFixed(2)} Cr`
  if (abs >= 1e5) return `₹${(rupees / 1e5).toFixed(1)}L`
  if (abs >= 1000) return `₹${(rupees / 1000).toFixed(1)}K`
  return `₹${Math.round(rupees)}`
}

export function formatPct(value, digits = 1) {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

export function formatClock(iso) {
  if (!iso) return '—'
  return new Date(iso).toISOString().slice(11, 19)
}

export function severityClass(severity) {
  return (severity || 'low').toLowerCase()
}

/** Reconnecting WebSocket for the live agent event stream. */
export function connectStream(onEvent, onStatus) {
  let socket = null
  let closed = false
  let retry = 0

  const open = () => {
    if (closed) return
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
    socket = new WebSocket(`${protocol}://${location.host}/ws?session=${encodeURIComponent(SESSION)}`)

    socket.onopen = () => {
      retry = 0
      onStatus?.('connected')
    }
    socket.onmessage = (message) => {
      try {
        onEvent(JSON.parse(message.data))
      } catch {
        /* a malformed frame must not kill the stream */
      }
    }
    socket.onclose = () => {
      onStatus?.('disconnected')
      if (closed) return
      // Back off, but keep trying: a dropped socket during a live demo should heal itself.
      retry = Math.min(retry + 1, 6)
      setTimeout(open, 400 * 2 ** retry)
    }
    socket.onerror = () => socket?.close()
  }

  open()
  return () => {
    closed = true
    socket?.close()
  }
}
