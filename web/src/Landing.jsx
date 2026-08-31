import { formatINR, formatPct } from './api.js'
import Mark from './Mark.jsx'
import productShot from './product.png'

/* The product surface. Everything here is real: the numbers come from the benchmark JSON the
 * harness writes, and the pipeline is the eight agents the supervisor actually runs.
 *
 * Laid out left-aligned against a screenshot of the real thing rather than as a stack of centred
 * blocks. A centred column of headline-subhead-buttons is the shape of a pitch deck; a product
 * page should show the product before it argues for it, and the first honest thing this page can
 * say is "here is what you are about to look at". */

const PIPELINE = [
  ['Detection', 'Three independent tests must agree'],
  ['Investigation', 'Read-only tools gather evidence'],
  ['Impact', 'Prices the damage per hour'],
  ['Root cause', 'Scores hypotheses, cites findings'],
  ['Decision', 'Chooses by expected value'],
  ['Policy gate', 'Deterministic. No model involved'],
  ['Action', 'The only agent that can write'],
  ['Verification', 'Measured against a control group'],
]

const PILLARS = [
  {
    k: '01',
    title: 'The model cannot execute anything',
    body:
      'Every action passes a deterministic policy gateway — plain Python evaluating the merchant’s own YAML, with no model anywhere in the path. The Action Agent physically cannot invoke a write tool without a decision naming that exact action.',
    proof: (r) =>
      r ? `${r.reliability.policy_violations} policy violations · ${r.reliability.unauthorised_executions} unauthorised executions` : null,
  },
  {
    k: '02',
    title: 'It checks its own work, and undoes it',
    body:
      'After acting it measures the result against a concurrent control group that was deliberately left alone, so the incident recovering on its own cannot be mistaken for the fix working. When the numbers do not improve it reverts its own change and hands over.',
    proof: (r) => (r ? `${formatPct(r.reliability.rollback_success_rate, 0)} of reverts succeed` : null),
  },
  {
    k: '03',
    title: 'It knows when to say nothing',
    body:
      'A traffic-mix shift drops the headline success rate while every provider keeps working perfectly. Rerouting healthy traffic would add risk and protect no revenue, so the correct response is silence — and that restraint is measured, not assumed.',
    proof: (r) =>
      r ? `${r.detection.false_positives} false positives in ${r.detection.false_positives + r.detection.true_negatives} healthy windows` : null,
  },
]

export default function Landing({ report, metrics, onEnter, onWatch }) {
  const d = report?.detection
  const g = report?.diagnosis
  const e = report?.end_to_end

  return (
    <div className="lp">
      <header className="lp-nav">
        <div className="lp-brand">
          <Mark size={22} />
          Payment Incident Commander
        </div>
        <button className="lp-cta sm" onClick={onEnter}>
          Open the console
        </button>
      </header>

      <section className="lp-hero">
        <div className="lp-hero-copy">
          <span className="lp-eyebrow">Razorpay AI Buildathon · autonomous payment operations</span>
          {/* No hard break: the column narrows with the viewport, and a break that reads well at
              one width strands a single word at another. CSS balances the lines instead. */}
          <h1>It doesn’t just tell you payments are failing.</h1>
          <p className="lp-sub">
            Eight agents detect the degradation, investigate it with read-only tools, price it,
            diagnose it, ask a deterministic policy gateway for permission, act — and then measure
            whether their own fix actually worked. When it didn’t, they undo it and hand over with
            a reason.
          </p>
          <div className="lp-actions">
            <button className="lp-cta" onClick={onWatch}>
              Watch it handle an incident
            </button>
            <a className="lp-ghost" href="https://github.com/vaibhavdabas16/payment-incident-commander">
              Read the code
            </a>
          </div>
          {metrics ? (
            <span className="lp-live">
              <i /> live simulation running · {formatPct(metrics.success_rate)} success right now
            </span>
          ) : null}
        </div>

        {/* The product, before the argument for it. */}
        <figure className="lp-shot">
          <img
            src={productShot}
            alt="The incident workflow: detection, investigation, impact, diagnosis, decision, policy gate, action and verification, each with its own result."
            loading="eager"
            width="1280"
          />
        </figure>
      </section>

      {d ? (
        <dl className="lp-proof">
          <div>
            <dt>Detection precision</dt>
            <dd>{formatPct(d.precision, 1)}</dd>
          </div>
          <div>
            <dt>Root-cause accuracy</dt>
            <dd>{formatPct(g.top1_accuracy, 1)}</dd>
          </div>
          <div>
            <dt>Median detection</dt>
            <dd>{d.median_detection_latency_s}s</dd>
          </div>
          <div>
            <dt>Unauthorised actions</dt>
            <dd>{report.reliability.unauthorised_executions}</dd>
          </div>
        </dl>
      ) : null}

      <section className="lp-section lp-problem">
        <div className="lp-head">
          <h2>A payment failure is a clock, not a ticket.</h2>
          <p>
            Success rate slips two points at 11pm. Somebody is paged, opens six dashboards, and
            starts guessing which of five providers, four routes, forty issuers or the deploy from
            an hour ago is responsible. Every minute of that costs the merchant money.
          </p>
        </div>
        {e ? (
          <div className="lp-compare">
            <div className="lp-bar">
              <span className="lp-bar-l">On-call engineer</span>
              <div className="lp-track">
                <div className="lp-fill human" style={{ width: '100%' }} />
              </div>
              <span className="lp-bar-v">{e.baseline_mitigation_s}s to mitigate</span>
            </div>
            <div className="lp-bar">
              <span className="lp-bar-l">This system</span>
              <div className="lp-track">
                <div
                  className="lp-fill ai"
                  style={{
                    width: `${Math.max(6, (e.ai_median_mitigation_s / e.baseline_mitigation_s) * 100)}%`,
                  }}
                />
              </div>
              <span className="lp-bar-v">{e.ai_median_mitigation_s}s to mitigate</span>
            </div>
            <p className="lp-note">{e.baseline_note}</p>
          </div>
        ) : null}
      </section>

      <section className="lp-section">
        <div className="lp-head">
          <h2>Eight agents, one job each.</h2>
          <p>
            One responsibility, one state, one typed output. Every step is recorded with the tools
            it called, so the audit trail is a property of the framework rather than of anyone’s
            diligence.
          </p>
        </div>
        <ol className="lp-pipe">
          {PIPELINE.map(([name, what], i) => (
            <li key={name} className={name === 'Policy gate' ? 'gate' : name === 'Action' ? 'writer' : ''}>
              <span className="lp-pipe-n">{String(i + 1).padStart(2, '0')}</span>
              <span className="lp-pipe-name">{name}</span>
              <span className="lp-pipe-what">{what}</span>
            </li>
          ))}
        </ol>
      </section>

      <section className="lp-section">
        <div className="lp-head">
          <h2>Three things that are load-bearing.</h2>
        </div>
        <div className="lp-pillars">
          {PILLARS.map((p) => (
            <article key={p.k}>
              <span className="lp-k">{p.k}</span>
              <h3>{p.title}</h3>
              <p>{p.body}</p>
              {p.proof(report) ? <span className="lp-proof-line">{p.proof(report)}</span> : null}
            </article>
          ))}
        </div>
      </section>

      {report ? (
        <section className="lp-section lp-bench">
          <div className="lp-head">
            <h2>Every number here is generated, not written.</h2>
            <p>
              Produced by <code>python -m pic.evaluation.harness</code> over{' '}
              {report.seeds?.length} seeds on the deterministic reasoner, and read from its JSON
              output. Nothing on this page is typed by hand.
            </p>
          </div>
          <div className="lp-grid">
            {[
              ['Precision', d.precision],
              ['Recall', d.recall],
              ['F1', d.f1],
              ['Root-cause accuracy', g.top1_accuracy],
              ['Evidence grounding', g.evidence_grounding_rate],
              ['Scenarios detected', `${d.scenarios_detected}/${d.scenarios_total}`],
              ['Policy violations', report.reliability.policy_violations],
              ['Revenue protected', formatINR(report.business.total_revenue_protected_per_hour_paise)],
            ].map(([k, v]) => (
              <div key={k}>
                <span className="lp-grid-v">{v}</span>
                <span className="lp-grid-k">{k}</span>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      <section className="lp-final">
        <Mark size={44} detail />
        <h2>Break something and watch.</h2>
        <p>
          Nine scenarios, including one where the obvious fix cannot possibly work. Nothing is
          scripted — the same detector, agents, policy gateway and tools run whether you are
          watching or the benchmark is.
        </p>
        <button className="lp-cta" onClick={onWatch}>
          Break something and watch
        </button>
      </section>

      <footer className="lp-foot">
        <span>Payment Incident Commander — built for the Razorpay AI Buildathon</span>
        <span>{report ? `${report.reasoner} reasoner · seeds ${report.seeds?.join(', ')}` : ''}</span>
      </footer>
    </div>
  )
}
