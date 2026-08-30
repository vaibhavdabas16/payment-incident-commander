import { useState } from 'react'
import { Led } from './components.jsx'

/* Injecting a degradation is a demo control rather than part of an operator's job, so it lives in
 * the chrome. Two ways in: one of the nine designed scenarios, or an incident described by the
 * person watching. A custom incident is injected into the same simulator and handled by the same
 * detector, agents, policy gateway and tools — there is no separate path for it, which is the
 * point. The pipeline either copes with a degradation nobody designed it around or it does not,
 * in front of you. */

const DIMENSION_LABEL = {
  payment_method: 'Payment method',
  psp: 'Provider',
  issuer: 'Issuing bank',
  geography: 'Region',
  route_id: 'Route',
  gateway: 'Gateway',
}

export function SimulateMenu({ scenarios, options, busy, notice, onInject, onCustom, onReset }) {
  const [tab, setTab] = useState('preset')
  const running = scenarios.some((s) => s.active)

  return (
    <details className="menu">
      <summary className="btn sm">Simulate</summary>
      <div className="menu-pop">
        <div className="menu-head">
          <div className="menu-tabs">
            <button className={`menu-tab ${tab === 'preset' ? 'on' : ''}`} onClick={() => setTab('preset')}>
              Scenarios
            </button>
            <button className={`menu-tab ${tab === 'custom' ? 'on' : ''}`} onClick={() => setTab('custom')}>
              Your own
            </button>
          </div>
          <button className="btn sm" disabled={busy || !running} onClick={onReset}>Reset</button>
        </div>

        {notice ? <div className={`notice ${notice.kind}`}>{notice.text}</div> : null}

        {tab === 'preset' ? (
          <div className="menu-list">
            {scenarios.map((s) => (
              <button
                key={s.scenario_id}
                className={`menu-item ${s.active ? 'on' : ''}`}
                disabled={busy}
                title={s.description}
                onClick={() => onInject(s)}
              >
                <span>{s.name}</span>
                {s.active ? <Led tone="warn" live /> : null}
              </button>
            ))}
          </div>
        ) : (
          <CustomForm options={options} busy={busy} onCustom={onCustom} />
        )}
      </div>
    </details>
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

      <button className="btn primary" type="submit" disabled={busy || !chosen}>
        Run this incident
      </button>
      <p className="menu-note">
        Runs through the same detector, agents and policy gateway as the shipped scenarios. It is
        not part of the benchmark — that measures the nine designed cases.
      </p>
    </form>
  )
}
