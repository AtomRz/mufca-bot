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

const FLIP_MARK = { helps: '✅', hurts: '❌', mixed: '▫️', no_effect: '—' }

// The checks a proposal had to pass: each changed filter flipped alone, then the walk-forward gate.
function CheckLines({ chk }) {
  if (!chk) return null
  const wf = chk.walk_forward
  return (
    <div style={{ color: 'var(--text-faint)' }}>
      {chk.flips.map((f) => (
        <div key={f.filter}>
          {FLIP_MARK[f.verdict] || '▫️'} {f.filter} → {f.to ? 'on' : 'off'} alone: net {pct(f.delta)}{' '}
          [early {pct(f.early)} | late {pct(f.late)}] ({f.extra_trades >= 0 ? '+' : ''}{f.extra_trades} trades) — {f.verdict}
        </div>
      ))}
      {chk.trimmed && (
        <div>✂️ trimmed to {chk.trimmed.filters.join(', ')}: net {pct(chk.trimmed.delta)} [early {pct(chk.trimmed.early)} | late {pct(chk.trimmed.late)}]</div>
      )}
      {wf && (
        <div>
          {wf.passed ? '✅' : '⛔'} walk-forward {wf.verdict} ({wf.mode === 'not_worse' ? 'lenient' : 'strict'}): switched in{' '}
          {wf.folds_switched}/{wf.folds} folds, better in {wf.folds_switched_positive}; in-sample {pct(wf.in_sample_gain)} → out-of-sample {pct(wf.out_of_sample_gain)}
        </div>
      )}
      {chk.decision === 'keep' && <div style={{ color: 'var(--short)' }}>⛔ not applied ({chk.blocked_by}): {chk.reason}</div>}
    </div>
  )
}

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
            <div style={{ color: 'var(--text-faint)' }}>
              {r.reason}
              {r.proposed && <><br />🔎 the tuner proposed {describeSet(r.proposed)}</>}
            </div>
          )}
          <CheckLines chk={r.checks} />
        </div>
      ))}
    </div>
  )
}

function PinnedSets({ overrides, tracked, busy, onRevert, onClear, onRemove }) {
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
            <span className="row-label">
              {ticker} {tf}
              {tracked && !tracked.includes(ticker) && <span style={{ color: 'var(--accent)' }}> · pair no longer tracked</span>}
            </span>
            <span className="row-value">
              {describeSet(flags)}{' '}
              <button className="btn" disabled={busy} title="Remove this pin (the pair/timeframe goes back to the global toggles)"
                      onClick={() => onRemove(ticker, tf)} style={{ marginLeft: 6, padding: '0 8px' }}>✕</button>
            </span>
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

const FILTER_LABELS = {
  frama: 'FRAMA trend', slope: 'FRAMA slope', chop: 'CHOP', atr: 'ATR', htf: 'HTF bias',
  fake_break: 'Fake breakout', liq_sweep: 'Liquidity sweep', hurst: 'Hurst',
}

// Hand-edit the filters of ONE pair/timeframe. Shows what is in force right now
// (the pinned set, else the global toggles); flipping a switch pins the pair.
function ManualPin({ config, busy, act }) {
  const pairs = config.pairs || []
  const tfs = config.timeframes || []
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const t = ticker || pairs[0] || ''
  const f = tf || tfs[0] || ''
  const pinned = config.filter_overrides?.active?.[t]?.[f]
  const flags = pinned || config.filter_toggles || {}
  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>Set filters by hand</div>
      <div style={{ display: 'flex', gap: 10, marginBottom: 8 }}>
        <select value={t} onChange={(e) => setTicker(e.target.value)}>
          {pairs.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <select value={f} onChange={(e) => setTf(e.target.value)}>
          {tfs.map((x) => <option key={x} value={x}>{x}</option>)}
        </select>
      </div>
      <p style={{ fontSize: 11, color: 'var(--text-faint)', marginBottom: 6 }}>
        {pinned ? 'Pinned for this pair/timeframe.' : 'Following the global toggles — flipping a switch pins this pair/timeframe.'}
      </p>
      {FILTER_ORDER.map((key) => (
        <div className="toggle-row" key={key}>
          <span className="row-label">{FILTER_LABELS[key]}</span>
          <label className="switch">
            <input type="checkbox" checked={!!flags[key]} disabled={busy || !t || !f}
                   onChange={(e) => act(() => api.pinFilters(t, f, { [key]: e.target.checked }))} />
            <span className="switch-track" />
          </label>
        </div>
      ))}
      <button className="btn" style={{ marginTop: 8 }} disabled={busy || !pinned}
              onClick={() => act(() => api.pinFilters(t, f, null))}>
        Remove pin (use global toggles)
      </button>
    </div>
  )
}

export default function AutoFilterPanel({ config, onChanged }) {
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(6000)
  const [lenient, setLenient] = useState(false)
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
    sim.start(() => api.runAutoFilter({ ticker, tf, numBars, apply, validateOos: !skipOos, lenient }))
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
        A change is applied only if it also passes two more checks in the same run: every filter it touches, flipped
        on its own, must make more money in BOTH the older and the newer part of the history (a change that fails is
        dropped), and the rolling walk-forward of re-tuning that pair/timeframe (the same check as the Walk-forward
        panel) must be robust. Otherwise the pair keeps its filters and the result says which check stopped it.
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
      <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, marginBottom: 6 }}>
        <input type="checkbox" checked={lenient} disabled={skipOos} onChange={(e) => setLenient(e.target.checked)} />
        Lenient walk-forward: let a change through unless the walk-forward is "not robust" (inconclusive is enough)
      </label>
      <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, marginBottom: 10 }}>
        <input type="checkbox" checked={skipOos} onChange={(e) => setSkipOos(e.target.checked)} />
        Skip ALL checks (no out-of-sample guard, no per-filter or walk-forward check — maximises past PnL, overfits more)
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
        tracked={config.pairs}
        busy={busy || sim.running}
        onRemove={(t, f) => act(() => api.pinFilters(t, f, null))}
        onRevert={() => act(() => api.revertFilterOverrides())}
        onClear={() => act(() => api.clearFilterOverrides())}
      />
      <ManualPin config={config} busy={busy || sim.running} act={act} />
      <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
        Pinned sets win over the global toggles for that pair/timeframe. Also available in Discord: !autofilter.
      </p>
    </div>
  )
}
