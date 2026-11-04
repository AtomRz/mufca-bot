import React, { useState } from 'react'
import { api } from '../api'
import { SimScopeFields, SimStatus, BarCoverageWarning, useSimJob, barsError } from './InsightsPanel'

const FILTER_ORDER = ['frama', 'slope', 'chop', 'atr', 'htf', 'fake_break', 'liq_sweep', 'hurst']

const offList = (flags) => FILTER_ORDER.filter((k) => !flags?.[k])
const describeSet = (flags) => {
  const off = offList(flags)
  return off.length === 0 ? 'all on' : `off: ${off.join(', ')}`
}
const pct = (v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
const statText = (st) => `n=${st.n} avg ${pct(st.avg)} total ${pct(st.total)}`

function ResultRows({ result }) {
  const rows = []
  Object.entries(result.results || {}).forEach(([ticker, perTf]) => {
    Object.entries(perTf).forEach(([tf, r]) => rows.push({ ticker, tf, r }))
  })
  if (rows.length === 0) return <p style={{ fontSize: 12, color: 'var(--text-faint)' }}>No pair/timeframe produced a result.</p>
  return (
    <div style={{ marginBottom: 10 }}>
      {rows.map(({ ticker, tf, r }) => (
        <div key={`${ticker}-${tf}`} style={{ fontSize: 12, marginBottom: 8 }}>
          <div style={{ fontWeight: 600 }}>
            {ticker} {tf}{' '}
            {r.changed
              ? <span style={{ color: 'var(--long)' }}>{result.applied ? '✅ pinned' : '✅ would pin'}: {describeSet(r.chosen)}</span>
              : <span style={{ color: 'var(--text-faint)' }}>unchanged ({describeSet(r.current)})</span>}
          </div>
          {r.changed ? (
            <div style={{ color: 'var(--text-faint)' }}>
              now {statText(r.best_stats.all)} · was {statText(r.current_stats.all)} (was: {describeSet(r.current)})
              {r.validated && <><br />out-of-sample: now {statText(r.best_stats.test)} · was {statText(r.current_stats.test)}</>}
            </div>
          ) : (
            <div style={{ color: 'var(--text-faint)' }}>{r.reason}</div>
          )}
        </div>
      ))}
    </div>
  )
}

function PinnedSets({ overrides, busy, onRevert, onClear }) {
  const active = overrides?.active || {}
  const rows = []
  Object.keys(active).sort().forEach((ticker) => {
    Object.keys(active[ticker]).sort().forEach((tf) => rows.push({ ticker, tf, flags: active[ticker][tf] }))
  })
  const canRevert = rows.length > 0 || Object.keys(overrides?.previous || {}).length > 0
  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>Pinned filter sets</div>
      {rows.length === 0 ? (
        <p style={{ fontSize: 12, color: 'var(--text-faint)' }}>None — every pair/timeframe uses the global toggles above.</p>
      ) : (
        rows.map(({ ticker, tf, flags }) => (
          <div className="row" key={`${ticker}-${tf}`} style={{ fontSize: 12 }}>
            <span className="row-label">{ticker} {tf}</span>
            <span className="row-value">{describeSet(flags)}</span>
          </div>
        ))
      )}
      <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
        <button className="btn" disabled={busy || !canRevert} onClick={onRevert}>Revert last change</button>
        <button className="btn" disabled={busy || rows.length === 0} onClick={onClear}>Clear all</button>
      </div>
    </div>
  )
}

export default function AutoFilterPanel({ config, onChanged }) {
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(3000)
  const [skipOos, setSkipOos] = useState(false)
  const [busy, setBusy] = useState(false)
  const [localError, setLocalError] = useState(null)
  const sim = useSimJob('autofilter')
  const result = sim.job?.status === 'done' ? sim.job.result : null
  const ranBars = sim.job?.params?.num_bars ?? numBars

  const start = (apply) => {
    const bad = barsError(numBars)
    if (bad) { sim.setError(bad); return }
    setLocalError(null)
    sim.start(() => api.runAutoFilter({ ticker, tf, numBars, apply, validateOos: !skipOos }))
  }

  // refresh the pinned list (config.filter_overrides) once an applying run finishes
  const appliedJobId = result?.applied ? sim.job.id : null
  React.useEffect(() => { if (appliedJobId) onChanged() }, [appliedJobId]) // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (fn) => {
    setBusy(true); setLocalError(null)
    try { await fn(); onChanged() } catch (e) { setLocalError(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="panel">
      <h3 className="panel-title">Auto-tune filters</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 12 }}>
        Backtests every pair × timeframe, tries all combinations of the 8 backtestable filters and pins the set with
        the best PnL (after fees) for each one — so e.g. CHOP can stay on for BTC and be off for ETH. The set is chosen
        on the older 60% of history and only replaces the current one if it also holds up on the newest 40%.
        Spread is live-only and stays global.
      </p>
      <SimScopeFields
        pairs={config.pairs} timeframes={config.timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={
          <>
            <button onClick={() => start(true)} disabled={sim.running}>{sim.running ? 'Running…' : 'Tune & apply'}</button>
            <button className="btn" onClick={() => start(false)} disabled={sim.running}>Preview only</button>
          </>
        }
      />
      <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, marginBottom: 10 }}>
        <input type="checkbox" checked={skipOos} onChange={(e) => setSkipOos(e.target.checked)} />
        Skip the out-of-sample check (maximises past PnL, overfits more)
      </label>
      <SimStatus sim={sim} />
      {localError && <p style={{ color: 'var(--short)', fontSize: 13 }}>Failed: {localError}</p>}
      {result && !sim.running && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          <p style={{ fontSize: 12, marginBottom: 8 }}>
            Net of {result.fee_pct}% fee per trade · {result.applied ? 'applied' : 'nothing applied'}
          </p>
          {(result.skipped || []).map((sk) => (
            <p key={`${sk.ticker}-${sk.tf}`} style={{ fontSize: 12, color: 'var(--short)', marginBottom: 6 }}>
              ❌ {sk.ticker} {sk.tf}: skipped — {sk.reason}
            </p>
          ))}
          <ResultRows result={result} />
        </>
      )}
      <PinnedSets
        overrides={config.filter_overrides}
        busy={busy || sim.running}
        onRevert={() => act(() => api.revertFilterOverrides())}
        onClear={() => act(() => api.clearFilterOverrides())}
      />
      <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
        Pinned sets win over the global toggles for that pair/timeframe. Also available in Discord: !autofilter.
      </p>
    </div>
  )
}
