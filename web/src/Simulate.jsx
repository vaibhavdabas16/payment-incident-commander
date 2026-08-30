import { useState } from 'react'
import { Card, Led, Tag } from './components.jsx'

/* Running a degradation is its own page rather than a menu in the chrome: it is the thing a
 * visitor comes here to do, and a dropdown made it feel like a developer toggle.
 *
 * Two ways in. One of the nine designed scenarios, or an incident described by the person
 * watching. A custom one is injected into the same simulator and handled by the same detector,
 * agents, policy gateway and tools — there is no separate path for it, which is the point. */

const DIMENSION_LABEL = {
  payment_method: 'Payment method',
  psp: 'Provider',
  issuer: 'Issuing bank',
  geography: 'Region',
  route_id: 'Route',
  gateway: 'Gateway',
}

function scenarioTag(s) {
  if (!s.injects_failure) return { text: 'restraint test', tone: 'ok' }
  if (s.fallback_healthy === false) return { text: 'the fix cannot work', tone: 'elevated' }
  if (s.recommended_action === 'escalate') return { text: 'hands to a human', tone: 'warn' }
  return null
}

export default function Simulate({ scenarios, options, busy, notice, onInject, onCustom, onReset, onGoto }) {
  const running = (scenarios || []).filter((s) => s.active)

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Simulate</h1>
          <p>
            Break something and watch the agents work it. Nothing here is scripted — the same
            detector, agents, policy gateway and tools run whether you are watching or the
            benchmark is.
          </p>
        </div>
        <div style={{ display: 'flex', gap: 9, alignItems: 'center' }}>
          {running.length ? (
            <span className="pill"><Led tone="warn" live />{running.length} running</span>
          ) : null}
          <button className="btn" disabled={busy || !running.length} onClick={onReset}>Reset</button>
        </div>
      </div>

      {notice ? (
        <div className={`notice ${notice.kind}`}>
          {notice.text}{' '}
          <button className="linkish" onClick={() => onGoto('command')}>Watch it →</button>
        </div>
      ) : null}

      <div className="grid-73">
        <Card title="Designed scenarios" sub={`${scenarios?.length || 0} cases`}>
          <div className="scen">
            {(scenarios || []).map((s) => {
              const tag = scenarioTag(s)
              return (
                <button
                  key={s.scenario_id}
                  className={`scen-btn ${s.active ? 'on' : ''}`}
                  disabled={busy}
                  onClick={() => onInject(s)}
                >
                  <span className="scen-head">
                    <span className="scen-name">{s.name}</span>
                    {s.active ? <Tag tone="warn">running</Tag> : tag ? <Tag tone={tag.tone}>{tag.text}</Tag> : null}
                  </span>
                  <span className="scen-desc">{s.description}</span>
                </button>
              )
            })}
          </div>
        </Card>

        <Card title="Your own incident" sub="not benchmarked">
          <CustomForm options={options} busy={busy} onCustom={onCustom} />
        </Card>
      </div>
    </>
  )
}

function CustomForm({ options, busy, onCustom }) {
  const [dimension, setDimension] = useState('issuer')
  const [value, setValue] = useState('')
  const [method, setMethod] = useState('')
  const [severity, setSeverity] = useState(45)
  const [minutes, setMinutes] = useState(12)
  const [code, setCode] = useState('PSP_UNAVAILABLE')

  if (!options) return <p className="menu-note">Loading options…</p>

  const values = options.dimensions[dimension] || []
  const chosen = value || values[0] || ''

  return (
    <form
      className="cform"
      onSubmit={(e) => {
        e.preventDefault()
        onCustom({
          dimension,
          value: chosen,
          payment_method: method || null,
          severity_pct: Number(severity),
          duration_minutes: Number(minutes),
          error_code: code,
        })
      }}
    >
      <label>
        <span>Break this</span>
        <div className="cform-pair">
          <select value={dimension} onChange={(e) => { setDimension(e.target.value); setValue('') }}>
            {Object.keys(options.dimensions).map((d) => (
              <option key={d} value={d}>{DIMENSION_LABEL[d] || d}</option>
            ))}
          </select>
          <select value={chosen} onChange={(e) => setValue(e.target.value)}>
            {values.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
        </div>
      </label>

      {dimension !== 'payment_method' ? (
        <label>
          <span>Only on</span>
          <select value={method} onChange={(e) => setMethod(e.target.value)}>
            <option value="">every payment method</option>
            {options.dimensions.payment_method.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </label>
      ) : null}

      <label>
        <span>Severity — loses {severity}% of its success rate</span>
        <input
          type="range"
          min={options.limits.severity_pct[0]}
          max={options.limits.severity_pct[1]}
          value={severity}
          onChange={(e) => setSeverity(e.target.value)}
        />
      </label>

      <div className="cform-pair">
        <label>
          <span>For (minutes)</span>
          <input
            type="number"
            min={options.limits.duration_minutes[0]}
            max={options.limits.duration_minutes[1]}
            value={minutes}
            onChange={(e) => setMinutes(e.target.value)}
          />
        </label>
        <label>
          <span>Failing with</span>
          <select value={code} onChange={(e) => setCode(e.target.value)}>
            {options.error_codes.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
      </div>

      <button className="btn primary" type="submit" disabled={busy || !chosen}>Run this incident</button>
      <p className="menu-note">
        Runs through the same pipeline as the designed scenarios. It is not part of the benchmark —
        that measures the nine cases where there is a known correct answer to score against.
      </p>
    </form>
  )
}
