import React, { useState } from 'react'
import { api } from '../api'
import { SimScopeFields, SimStatus, BarCoverageWarning, useSimJob, barsError } from './InsightsPanel'

const FILTER_ORDER = ['frama', 'slope', 'chop', 'atr', 'htf', 'fake_break', 'liq_sweep', 'hurst']

const describeSet = (flags) => {
  const off = FILTER_ORDER.filter((k) => !flags?.[k])
  return off.length === 0 ? 'all on' : `off: ${off.join(', ')}`
}
const pct = (v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
const stat = (st) => `n=${st.n} total ${pct(st.total)}`

const VERDICTS = {
  robust: { icon: '✅', label: 'robust', color: 'var(--long)' },
  stable: { icon: '🟦', label: 'stable — nothing to re-tune', color: 'var(--text-faint)' },
  not_robust: { icon: '❌', label: 'not robust', color: 'var(--short)' },
  inconclusive: { icon: '🟨', label: 'inconclusive', color: 'var(--accent)' },
  no_data: { icon: '⬜', label: 'no data', color: 'var(--text-faint)' },
}

const AUDIT_STATUS = { holding: '✅', worse: '❌', no_difference: '➖', too_early: '⏳' }

function AuditRow({ name, w }) {
  if (!w) return null
  return (
    <div>
      {AUDIT_STATUS[w.status] || ''} {name}: pinned {stat(w.pinned)} vs before {stat(w.reference)} →{' '}
      <b>{pct(w.gain.gain)}</b> (noise {w.gain.noise.toFixed(2)}%) — {w.status.replace('_', ' ')}
    </div>
  )
}

function PairResult({ ticker, tf, r }) {
  const wf = r.walk_forward
  const v = VERDICTS[wf.verdict] || { icon: '', label: wf.verdict, color: 'var(--text-faint)' }
  const agg = wf.aggregate
  const stab = wf.stability
  const thin = wf.folds.map((f, i) => (f.thin ? i + 1 : null)).filter(Boolean)
  return (
    <div style={{ fontSize: 12, marginBottom: 12 }}>
      <div style={{ fontWeight: 600 }}>
        {ticker} {tf} <span style={{ color: v.color }}>{v.icon} {v.label}</span>
      </div>
      <div style={{ color: 'var(--text-faint)' }}>
        {wf.reason}<br />
        current{r.current_pinned ? ' (pinned)' : ''}: {describeSet(r.current)}<br />
        out-of-sample, all folds — re-tuned {stat(agg.policy)} · current {stat(agg.current)} · all on {stat(agg.all_on)} · all off {stat(agg.all_off)}
        {agg.folds_switched > 0 && (
          <><br />switched in {agg.folds_switched}/{agg.folds} folds: saw {pct(agg.in_sample_gain)} in training,
            got {pct(agg.out_of_sample_gain)} on the next block</>
        )}
        {stab.folds_scored > 0 && stab.modal_pick && (
          <><br />stability: {stab.distinct_picks} distinct pick(s) in {stab.folds_scored} folds; most common
            ({Math.round(stab.modal_share * 100)}%): {describeSet(stab.modal_pick)}</>
        )}
        {thin.length > 0 && <><br />⚠ thin test block(s): {thin.join(', ')}</>}
        {r.audit && (
          <div style={{ marginTop: 4 }}>
            📌 pinned-set audit vs {r.audit.reference_kind === 'previous pin' ? 'the set before the pin' : 'the global toggles'}:
            <AuditRow name="since it was pinned" w={r.audit.since_pin} />
            <AuditRow name="newest bars" w={r.audit.recent} />
          </div>
        )}
        {r.current_pinned && <div>(the current set was itself tuned on this history, so its numbers here are optimistic)</div>}
      </div>
    </div>
  )
}

export default function WalkForwardPanel({ config }) {
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(6000)
  const [folds, setFolds] = useState(4)
  const sim = useSimJob('walkfwd')
  const result = sim.job?.status === 'done' ? sim.job.result : null
  const ranBars = sim.job?.params?.num_bars ?? numBars

  const start = () => {
    const bad = barsError(numBars)
    if (bad) { sim.setError(bad); return }
    if (!Number.isInteger(folds) || folds < 2 || folds > 12) { sim.setError('Folds must be a whole number between 2 and 12'); return }
    sim.start(() => api.runWalkForward({ ticker, tf, numBars, folds }))
  }

  const rows = []
  Object.entries(result?.results || {}).forEach(([t, perTf]) => {
    Object.entries(perTf).forEach(([f, r]) => rows.push({ t, f, r }))
  })

  return (
    <div className="panel">
      <h3 className="panel-title">Walk-forward check</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 12 }}>
        Auto-tune confirms its pick on one out-of-sample window. This cuts the history into several train → test folds,
        lets the tuner choose on each training part only and trades that choice on the next block it has never seen — so
        you see whether re-tuning keeps its edge or just fits noise, and how stable the chosen set is. Sets that are
        already pinned are also compared with the set they replaced on the bars after they were pinned.
        Dry run: nothing is applied.
      </p>
      <SimScopeFields
        pairs={config.pairs} timeframes={config.timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={
          <>
            <div>
              <label style={{ display: 'block', fontSize: 11, color: 'var(--text-faint)', marginBottom: 4 }}>Folds</label>
              <input type="number" value={folds} min={2} max={12} step={1}
                     onChange={(e) => setFolds(Number(e.target.value))} style={{ width: 70 }} />
            </div>
            <button onClick={start} disabled={sim.running}>{sim.running ? 'Running…' : 'Run check'}</button>
          </>
        }
      />
      <SimStatus sim={sim} />
      {result && !sim.running && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          <p style={{ fontSize: 12, marginBottom: 8 }}>
            {result.folds} test folds after the first {Math.round(result.initial_fraction * 100)}% of the history ·
            net of {result.fee_pct}% fee per trade · nothing applied
          </p>
          {(result.skipped || []).map((sk) => (
            <p key={`${sk.ticker}-${sk.tf}`} style={{ fontSize: 12, color: 'var(--short)', marginBottom: 6 }}>
              ❌ {sk.ticker} {sk.tf}: skipped — {sk.reason}
            </p>
          ))}
          {rows.length === 0
            ? <p style={{ fontSize: 12, color: 'var(--text-faint)' }}>No pair/timeframe produced a result.</p>
            : rows.map(({ t, f, r }) => <PairResult key={`${t}-${f}`} ticker={t} tf={f} r={r} />)}
          <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
            ✅ robust = re-tuning gained out-of-sample beyond the noise; ❌ = in-sample gains did not carry over, keep the
            current sets. Small samples stay noisy — re-run as bars accumulate. Also in Discord: !walkfwd.
          </p>
        </>
      )}
    </div>
  )
}
