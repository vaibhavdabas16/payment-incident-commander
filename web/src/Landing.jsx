import { formatINR, formatPct } from './api.js'
import Mark from './Mark.jsx'
import productShot from './product.png'

/* The product surface. Everything here is real: the numbers come from the benchmark JSON the
 * harness writes, and the pipeline is the ten agents the supervisor actually runs.
 *
 * Laid out left-aligned against a screenshot of the real thing rather than as a stack of centred
 * blocks. A centred column of headline-subhead-buttons is the shape of a pitch deck; a product
 * page should show the product before it argues for it, and the first honest thing this page can
 * say is "here is what you are about to look at".
 *
 * The order it argues in is deliberate. Money first, mechanism second: the loop below the hero is
 * the thing a merchant is buying, and the ten agents are how it is built. Leading with the agent
 * count describes the implementation to someone who asked what it does for them. */

/* The customer-facing loop. Revenue at both ends on purpose - it opens with what is being lost
 * and closes with what was kept, which is the whole product in eight words. */
const LOOP = [
  ['Revenue at risk', 'money', 'Payment failures priced in rupees, per hour'],
  ['Detect', '', 'Three independent tests must agree'],
  ['Diagnose', '', 'Cause established from evidence, not guessed'],
  ['Recover', '', 'The safest strategy, inside merchant guardrails'],
  ['Verify', '', 'Measured against an untouched control group'],
  ['Revenue protected', 'money', 'Counted only once the recovery is proven'],
  ['Learn', '', 'Every outcome recorded, good and bad'],
  ['Prevent', '', 'Recurring patterns raised for approval'],
]

const PIPELINE = [
  ['Detection', 'Three independent tests must agree'],
  ['Investigation', 'Read-only tools gather evidence'],
  ['Impact', 'Prices the damage per hour'],
  ['Root cause', 'Scores hypotheses, cites findings'],
  ['Recovery strategy', 'Prices every way out, with what happened last time'],
  ['Decision', 'Chooses by expected value'],
  ['Policy gate', 'Deterministic. No model involved'],
  ['Action', 'Can write, and only what policy granted'],
  ['Verification', 'Measured against a control group'],
  ['Order recovery', 'Goes back for the payments already lost'],
]

const PILLARS = [
  {
    k: '01',
    title: 'Autonomy within merchant-defined guardrails',
    body:
      'AI proposes. Policy decides. Every recovery passes a deterministic policy gateway — plain Python evaluating the merchant’s own YAML, with no model anywhere in the path. The Action Agent physically cannot invoke a write tool without a decision naming that exact action.',
    proof: (r) =>
      r ? `${r.reliability.policy_violations} policy violations · ${r.reliability.unauthorised_executions} unauthorised executions` : null,
  },
  {
    k: '02',
    title: 'Recovery is verified before it is counted',
    body:
      'We do not claim recovery just because payment success improved. The intervention is compared with an untouched control group living through the same hour, so the incident recovering on its own cannot be mistaken for the recovery working. If it does not work, the system rolls it back and escalates with the evidence.',
    proof: (r) => (r ? `${formatPct(r.reliability.rollback_success_rate, 0)} of reverts succeed` : null),
  },
  {
    k: '03',
    title: 'It knows when there is no revenue to recover',
    body:
      'A traffic-mix shift drops the headline success rate while every provider keeps working perfectly. Rerouting healthy traffic would add risk and recover nothing, so the correct response is silence — and that restraint is measured, not assumed.',
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
          <span className="lp-brand-name">
            <b>Payment Incident Commander</b>
            <i>Autonomous Payment Recovery &amp; Revenue Protection</i>
          </span>
        </div>
        <button className="lp-cta sm" onClick={onEnter}>
          Open the console
        </button>
      </header>

      <section className="lp-hero">
        <div className="lp-hero-copy">
          <span className="lp-eyebrow">
            Razorpay AI Buildathon · Autonomous Payment Recovery &amp; Revenue Protection
          </span>
          {/* No hard break: the column narrows with the viewport, and a break that reads well at
              one width strands a single word at another. CSS balances the lines instead. */}
          <h1>Recover the revenue, not just the incident.</h1>
          <p className="lp-sub">
            When payment failures put merchant revenue at risk, Payment Incident Commander detects
            the problem, identifies the cause, chooses the safest recovery strategy, executes it
            within merchant guardrails, verifies whether the intervention actually worked, and
            learns from the outcome.
          </p>
          <div className="lp-actions">
            <button className="lp-cta" onClick={onWatch}>
              Watch the recovery
            </button>
            <button className="lp-ghost" onClick={onEnter}>
              See how it works
            </button>
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

      {/* The value story, before any argument for it. */}
      <section className="lp-loop-band">
        <ol className="lp-loop">
          {LOOP.map(([name, kind, what], i) => (
            <li key={name} className={kind}>
              <span className="lp-loop-name">{name}</span>
              <span className="lp-loop-what">{what}</span>
              {i < LOOP.length - 1 ? <span className="lp-loop-arrow" aria-hidden="true" /> : null}
            </li>
          ))}
        </ol>
        <p className="lp-loop-foot">Detect. Recover. Verify. Learn.</p>
      </section>

      {d ? (
        <dl className="lp-proof">
          {/* A report generated before the revenue metrics existed has no `revenue` block, and
              summing two undefineds would print NaN where a rupee figure belongs. */}
          {report.revenue?.revenue_at_risk_paise ? (
            <>
              <div>
                <dt>Revenue recovered</dt>
                <dd>
                  {formatINR(
                    report.revenue.revenue_protected_paise + report.revenue.revenue_recovered_paise,
                  )}
                </dd>
              </div>
              <div>
                <dt>Recovery rate</dt>
                <dd>{formatPct(report.revenue.recovery_rate, 0)}</dd>
              </div>
            </>
          ) : null}
          <div>
            <dt>Detection precision</dt>
            <dd>{formatPct(d.precision, 1)}</dd>
          </div>
          <div>
            <dt>Root-cause accuracy</dt>
            <dd>{formatPct(g.top1_accuracy, 1)}</dd>
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
            an hour ago is responsible. Every minute of that is revenue leaving the merchant — and
            the ticket closing is not the same thing as the money coming back.
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
          <h2>How the recovery is actually built.</h2>
          <p>
            Ten agents, one responsibility each, one typed output each. This is the implementation
            beneath the loop above — every step recorded with the tools it called, so the audit
            trail is a property of the framework rather than of anyone’s diligence.
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
        <h2>Put revenue at risk and watch it come back.</h2>
        <p>
          Nine scenarios, including one where the obvious recovery cannot possibly work. Nothing is
          scripted — the same detector, agents, policy gateway and tools run whether you are
          watching or the benchmark is.
        </p>
        <button className="lp-cta" onClick={onWatch}>
          Watch the recovery
        </button>
      </section>

      <footer className="lp-foot">
        <span>
          Payment Incident Commander · Autonomous Payment Recovery &amp; Revenue Protection
        </span>
        <span>{report ? `${report.reasoner} reasoner · seeds ${report.seeds?.join(', ')}` : ''}</span>
      </footer>
    </div>
  )
}
