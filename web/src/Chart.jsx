import { useMemo, useRef, useState } from 'react'

/**
 * Payment success rate over time.
 *
 * One series, so no legend box — the panel title names it, and the current value is
 * direct-labelled at the right edge rather than numbering every point. The baseline is drawn as a
 * recessive dashed reference line in chrome ink, not as a second series: it is context for the
 * measured line, not a competing quantity.
 *
 * The y-axis is deliberately not zero-based. A payment success rate lives between roughly 70% and
 * 95%, and anchoring at zero would compress every operationally meaningful movement into a flat
 * band near the top. The axis is labelled at both ends so the range it covers is never implied.
 */
export default function SuccessRateChart({ points, baseline, height = 210 }) {
  const [hover, setHover] = useState(null)
  const svgRef = useRef(null)

  const data = useMemo(() => (points || []).filter((p) => p.transactions > 0), [points])

  const geometry = useMemo(() => {
    if (data.length < 2) return null
    const padding = { top: 14, right: 58, bottom: 24, left: 44 }
    const width = 640
    const innerW = width - padding.left - padding.right
    const innerH = height - padding.top - padding.bottom

    const values = data.map((d) => d.success_rate)
    if (baseline) values.push(baseline)
    let min = Math.min(...values)
    let max = Math.max(...values)
    const span = Math.max(max - min, 0.04)
    min = Math.max(0, min - span * 0.22)
    max = Math.min(1, max + span * 0.22)

    const x = (i) => padding.left + (i / (data.length - 1)) * innerW
    const y = (v) => padding.top + (1 - (v - min) / (max - min)) * innerH

    const line = data.map((d, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(2)},${y(d.success_rate).toFixed(2)}`).join(' ')
    const area = `${line} L${x(data.length - 1).toFixed(2)},${padding.top + innerH} L${x(0).toFixed(2)},${padding.top + innerH} Z`

    const ticks = [min, (min + max) / 2, max]
    return { padding, width, innerW, innerH, x, y, line, area, min, max, ticks }
  }, [data, baseline, height])

  if (!geometry) {
    return <div className="empty">Collecting payment traffic…</div>
  }

  const { padding, width, innerW, innerH, x, y, line, area, ticks } = geometry
  const last = data[data.length - 1]

  const handleMove = (event) => {
    const svg = svgRef.current
    if (!svg) return
    const rect = svg.getBoundingClientRect()
    // The SVG scales to its container, so map the pointer back into viewBox units.
    const px = ((event.clientX - rect.left) / rect.width) * width
    const ratio = (px - padding.left) / innerW
    const index = Math.round(ratio * (data.length - 1))
    if (index < 0 || index >= data.length) {
      setHover(null)
      return
    }
    setHover(index)
  }

  const hovered = hover !== null ? data[hover] : null

  return (
    <div className="chart-wrap">
      <figure className="chart-figure">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${width} ${height}`}
          width="100%"
          height={height}
          role="img"
          aria-label={`Payment success rate over the last ${data.length} intervals, currently ${(last.success_rate * 100).toFixed(1)} percent`}
          onMouseMove={handleMove}
          onMouseLeave={() => setHover(null)}
          style={{ display: 'block', cursor: 'crosshair' }}
        >
          <defs>
            <linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--series-1)" stopOpacity="0.26" />
              <stop offset="100%" stopColor="var(--series-1)" stopOpacity="0" />
            </linearGradient>
          </defs>

          {/* Recessive gridlines and axis labels */}
          {ticks.map((t) => (
            <g key={t}>
              <line
                x1={padding.left}
                x2={padding.left + innerW}
                y1={y(t)}
                y2={y(t)}
                stroke="var(--border)"
                strokeWidth="1"
              />
              <text
                x={padding.left - 9}
                y={y(t) + 4}
                textAnchor="end"
                fontSize="10.5"
                fill="var(--text-muted)"
                fontFamily="var(--mono)"
              >
                {(t * 100).toFixed(0)}%
              </text>
            </g>
          ))}

          {baseline ? (
            <g>
              <line
                x1={padding.left}
                x2={padding.left + innerW}
                y1={y(baseline)}
                y2={y(baseline)}
                stroke="var(--text-muted)"
                strokeWidth="1.5"
                strokeDasharray="5 4"
                opacity="0.75"
              />
              <text
                x={padding.left + innerW + 6}
                y={y(baseline) + 3.5}
                fontSize="10"
                fill="var(--text-muted)"
                fontFamily="var(--mono)"
              >
                baseline
              </text>
            </g>
          ) : null}

          <path d={area} fill="url(#fill)" />
          <path d={line} fill="none" stroke="var(--series-1)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />

          {/* Direct label on the current value only — never a number on every point. */}
          <circle cx={x(data.length - 1)} cy={y(last.success_rate)} r="4.5" fill="var(--series-1)" stroke="var(--surface-1)" strokeWidth="2" />
          <text
            x={x(data.length - 1) + 9}
            y={y(last.success_rate) + 4}
            fontSize="12"
            fontWeight="600"
            fill="var(--text-primary)"
            fontFamily="var(--mono)"
          >
            {(last.success_rate * 100).toFixed(1)}%
          </text>

          {hovered ? (
            <g pointerEvents="none">
              <line
                x1={x(hover)}
                x2={x(hover)}
                y1={padding.top}
                y2={padding.top + innerH}
                stroke="var(--border-strong)"
                strokeWidth="1"
              />
              <circle
                cx={x(hover)}
                cy={y(hovered.success_rate)}
                r="5"
                fill="var(--series-1)"
                stroke="var(--surface-1)"
                strokeWidth="2"
              />
            </g>
          ) : null}
        </svg>
      </figure>

      {hovered ? (
        <div
          className="tooltip"
          style={{
            left: `${Math.min(Math.max((x(hover) / width) * 100, 6), 78)}%`,
            top: 6,
          }}
        >
          <div>
            <span className="t-label">success </span>
            <span className="t-value">{(hovered.success_rate * 100).toFixed(1)}%</span>
          </div>
          <div>
            <span className="t-label">payments </span>
            <span className="t-value">{hovered.transactions}</span>
          </div>
          <div>
            <span className="t-label">at </span>
            <span className="t-value">{new Date(hovered.t).toISOString().slice(11, 19)}</span>
          </div>
        </div>
      ) : null}

      <figcaption className="chart-caption">
        Each point is a 60-second window. Dashed line is the rolling EWMA baseline the detector
        compares against.
      </figcaption>
    </div>
  )
}
