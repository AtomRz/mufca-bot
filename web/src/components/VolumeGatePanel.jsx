import React, { useState } from 'react'
import { api } from '../api'
import { SimScopeFields, SimStatus, BarCoverageWarning, useSimJob, barsError } from './InsightsPanel'

const pct = (v) => (Number.isFinite(v) ? `${v >= 0 ? '+' : ''}${v.toFixed(2)}%` : '—')
const statText = (st) => (st ? `n=${st.n} avg ${pct(st.avg)} total ${pct(st.total)}` : '—')
const thrText = (v) => (!v ? 'off' : Number(v).toFixed(2))

function GridTable({ rows, recommended }) {
  if (!rows || rows.length === 0) return null
  return (
    <details style={{ marginTop: 4 }}>
      <summary style={{ cursor: 'pointer', color: 'var(--text-faint)' }}>threshold grid</summary>
      <table style={{ fontSize: 11, marginTop: 4, borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ color: 'var(--text-faint)', textAlign: 'right' }}>
            <th style={{ textAlign: 'left', paddingRight: 10 }}>min ratio</th>
            <th style={{ paddingRight: 10 }}>blocked (train)</th>
            <th style={{ paddingRight: 10 }}>gain (train)</th>
            <th style={{ paddingRight: 10 }}>blocked (test)</th>
            <th>gain (test)</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.threshold} style={{ textAlign: 'right', fontWeight: r.threshold === recommended ? 700 : 400 }}>
              <td style={{ textAlign: 'left', paddingRight: 10 }}>{r.threshold.toFixed(2)}{r.eligible ? ' ·' : ''}</td>
              <td style={{ paddingRight: 10 }}>{r.blocked_train}</td>
              <td style={{ paddingRight: 10 }}>{pct(r.gain_train)}</td>
              <td style={{ paddingRight: 10 }}>{r.blocked_test}</td>
              <td>{pct(r.gain_test)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div style={{ fontSize: 10, color: 'var(--text-faint)' }}>gain = change of net total PnL vs. no gate · "·" = passed the training checks</div>
    </details>
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
      {rows.map(({ ticker, tf, r }) => {
        const dt = r.by_daytype || {}
        const wb = r.weekend_blocked
        return (
          <div key={`${ticker}-${tf}`} style={{ fontSize: 12, marginBottom: 10 }}>
            <div style={{ fontWeight: 600 }}>
              {ticker} {tf}{' '}
              {r.recommended === null && <span style={{ color: 'var(--text-faint)' }}>➖ left as is ({thrText(r.current)})</span>}
              {r.recommended !== null && r.changed && (
                <span style={{ color: 'var(--long)' }}>
                  {result.applied ? '✅ pinned' : '✅ would pin'}: min ratio {thrText(r.recommended)} (was {thrText(r.current)})
                </span>
              )}
              {r.recommended !== null && !r.changed && <span style={{ color: 'var(--text-faint)' }}>unchanged ({thrText(r.current)})</span>}
            </div>
            <div style={{ color: 'var(--text-faint)' }}>{r.reason}</div>
            {r.chosen_stats && (
              <div style={{ color: 'var(--text-faint)' }}>
                with gate {statText(r.chosen_stats.all)} · without {statText(r.base_stats.all)}
                {r.validated && <><br />out-of-sample: with gate {statText(r.chosen_stats.test)} · without {statText(r.base_stats.test)}</>}
              </div>
            )}
            {dt.weekend?.n > 0 && dt.weekday?.n > 0 && (
              <div style={{ color: 'var(--text-faint)' }}>
                weekend trades n={dt.weekend.n} avg {pct(dt.weekend.avg)} · weekdays n={dt.weekday.n} avg {pct(dt.weekday.avg)} (net of fee)
              </div>
            )}
            {wb && wb.blocked > 0 && (
              <div style={{ color: 'var(--text-faint)' }}>
                at {wb.threshold.toFixed(2)} the gate removes {wb.blocked} trades, {wb.weekend} of them on weekends (UTC)
              </div>
            )}
            <GridTable rows={r.rows} recommended={r.recommended} />
          </div>
        )
      })}
    </div>
  )
}

function LiveRatios({ live, loading, error, onCheck }) {
  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
        <span style={{ fontSize: 13, fontWeight: 600 }}>Live volume ratio</span>
        <button className="btn" disabled={loading} onClick={onCheck}>{loading ? 'Checking…' : 'Check now'}</button>
      </div>
      {error && <p style={{ color: 'var(--short)', fontSize: 12 }}>Failed: {error}</p>}
      {live && live.rows.map((r) => (
        <div className="row" key={`${r.ticker}-${r.tf}`} style={{ fontSize: 12 }}>
          <span className="row-label">{r.ticker} {r.tf}</span>
          <span className="row-value">
            {r.ratio === null || r.ratio === undefined
              ? 'n/a'
              : <>
                  <b>{r.ratio.toFixed(2)}</b> vs min {thrText(r.threshold)}{' '}
                  <span style={{ color: r.would_block ? 'var(--short)' : 'var(--long)' }}>{r.would_block ? '🔻 below' : '✅ ok'}</span>
                </>}
          </span>
        </div>
      ))}
      {live && !live.enabled && (
        <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 4 }}>The gate is off — nothing is blocked, this is only a preview.</p>
      )}
    </div>
  )
}

function PinnedThresholds({ gate, tracked, busy, onRevert, onClear }) {
  const rows = []
  Object.keys(gate.overrides || {}).sort().forEach((ticker) => {
    Object.keys(gate.overrides[ticker]).sort().forEach((tf) => rows.push({ ticker, tf, v: gate.overrides[ticker][tf] }))
  })
  const canRevert = rows.length > 0 || Object.keys(gate.previous || {}).length > 0
  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>Pinned thresholds</div>
      {rows.length === 0 ? (
        <p style={{ fontSize: 12, color: 'var(--text-faint)' }}>None — every pair/timeframe uses the default threshold.</p>
      ) : (
        rows.map(({ ticker, tf, v }) => (
          <div className="row" key={`${ticker}-${tf}`} style={{ fontSize: 12 }}>
            <span className="row-label">
              {ticker} {tf}
              {tracked && !tracked.includes(ticker) && <span style={{ color: 'var(--accent)' }}> · pair no longer tracked</span>}
            </span>
            <span className="row-value">{v === 0 ? 'off for this pair' : `min ratio ${v.toFixed(2)}`}</span>
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

export default function VolumeGatePanel({ config, onChanged }) {
  const gate = config.volume_gate || { enabled: false, threshold: 0.5, baseline_days: 14, overrides: {}, previous: {} }
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(3000)
  const [skipOos, setSkipOos] = useState(false)
  const [thrDraft, setThrDraft] = useState(null)
  const [daysDraft, setDaysDraft] = useState(null)
  const [busy, setBusy] = useState(false)
  const [localError, setLocalError] = useState(null)
  const [live, setLive] = useState(null)
  const [liveLoading, setLiveLoading] = useState(false)
  const [liveError, setLiveError] = useState(null)
  const sim = useSimJob('volfit')
  const result = sim.job?.status === 'done' ? sim.job.result : null
  const ranBars = sim.job?.params?.num_bars ?? numBars

  const start = (apply) => {
    const bad = barsError(numBars)
    if (bad) { sim.setError(bad); return }
    setLocalError(null)
    sim.start(() => api.runVolumeFit({ ticker, tf, numBars, apply, validateOos: !skipOos }))
  }

  // refresh the pinned list (config.volume_gate) once an applying run finishes
  const appliedJobId = result?.applied ? sim.job.id : null
  React.useEffect(() => { if (appliedJobId) onChanged() }, [appliedJobId]) // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (fn) => {
    setBusy(true); setLocalError(null)
    try { await fn(); onChanged() } catch (e) { setLocalError(e.message) } finally { setBusy(false) }
  }

  const checkLive = async () => {
    setLiveLoading(true); setLiveError(null)
    try { setLive(await api.getVolumeGateLive()) } catch (e) { setLiveError(e.message) } finally { setLiveLoading(false) }
  }

  const thrValue = thrDraft ?? String(gate.threshold)
  const daysValue = daysDraft ?? String(gate.baseline_days)
  const dirty = thrDraft !== null || daysDraft !== null
  const save = () => act(async () => {
    const patch = {}
    if (thrDraft !== null) {
      const v = Number(thrDraft.replace(',', '.'))
      if (!Number.isFinite(v) || v < 0 || v > 5) throw new Error('Threshold must be a number between 0 and 5')
      patch.threshold = v
    }
    if (daysDraft !== null) {
      const d = Number(daysDraft)
      if (!Number.isInteger(d) || d < 3 || d > 60) throw new Error('Baseline must be a whole number of days between 3 and 60')
      patch.baseline_days = d
    }
    await api.setVolumeGate(patch)
    setThrDraft(null); setDaysDraft(null)
  })

  return (
    <div className="panel">
      <h3 className="panel-title">Low-volume gate</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 12 }}>
        Stops NEW entries while the signal bar's volume is far below what is normal for the pair/timeframe — thin trading
        (weekends, holidays, dead hours) is easy to push around. ratio = bar volume ÷ median volume of the prior baseline
        window, so weekends and holidays show up low on their own, no calendar. Blocked signals are not lost: they are
        tracked as shadow records so you can check what they would have done. Open trades are not affected.
      </p>

      <div className="toggle-row">
        <span className="row-label" title="Off by default. Pinned per-pair thresholds do nothing while this is off.">Block entries on low volume</span>
        <label className="switch">
          <input type="checkbox" checked={!!gate.enabled} disabled={busy}
                 onChange={(e) => act(() => api.setVolumeGate({ enabled: e.target.checked }))} />
          <span className="switch-track" />
        </label>
      </div>

      <div style={{ display: 'flex', gap: 14, alignItems: 'flex-end', flexWrap: 'wrap', margin: '10px 0' }}>
        <div className="field" style={{ marginBottom: 0 }}>
          <label title="Default minimum ratio, used wherever no pair/timeframe threshold is pinned. 0.5 = block when the bar's volume is under half of normal.">Default min ratio</label>
          <input type="number" step="0.05" min="0" max="5" value={thrValue} disabled={busy}
                 onChange={(e) => setThrDraft(e.target.value)} style={{ width: 90 }} />
        </div>
        <div className="field" style={{ marginBottom: 0 }}>
          <label title="How many prior days make up 'normal' volume. Changing it changes the ratios, so re-run the auto-tune afterwards.">Baseline (days)</label>
          <input type="number" step="1" min="3" max="60" value={daysValue} disabled={busy}
                 onChange={(e) => setDaysDraft(e.target.value)} style={{ width: 90 }} />
        </div>
        <button className="btn" disabled={busy || !dirty} onClick={save}>Save</button>
      </div>

      <LiveRatios live={live} loading={liveLoading} error={liveError} onCheck={checkLive} />

      <div style={{ fontSize: 13, fontWeight: 600, margin: '16px 0 6px' }}>Auto-tune thresholds</div>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 10 }}>
        Backtests every pair × timeframe, records the volume ratio of every signal and tries thresholds 0.30–0.90 on top of
        the current filter set, scoring net PnL (after fees). It is picked on the older 60% of history and kept only if it
        beats noise, blocks enough trades to be measurable, sits on a plateau (neighbouring thresholds gain too) and is
        confirmed on the newest 40%. If nothing proves itself the gate stays off for that pair/timeframe — a normal result.
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
            Net of {result.fee_pct}% fee per trade · baseline {result.baseline_days} days · {result.applied ? 'applied' : 'nothing applied'}
          </p>
          {result.applied && !result.gate_enabled && (
            <p style={{ fontSize: 12, color: 'var(--accent)', marginBottom: 8 }}>
              ⚠️ The gate is off — the pinned thresholds do nothing until you switch it on above.
            </p>
          )}
          {(result.skipped || []).map((sk) => (
            <p key={`${sk.ticker}-${sk.tf}`} style={{ fontSize: 12, color: 'var(--short)', marginBottom: 6 }}>
              ❌ {sk.ticker} {sk.tf}: skipped — {sk.reason}
            </p>
          ))}
          <ResultRows result={result} />
        </>
      )}
      <PinnedThresholds
        gate={gate}
        tracked={config.pairs}
        busy={busy || sim.running}
        onRevert={() => act(() => api.revertVolumeGate())}
        onClear={() => act(() => api.clearVolumeGate())}
      />
      <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
        Pinned thresholds win over the default for that pair/timeframe (off = no gate there). Also available in Discord: !volume, !volfit.
      </p>
    </div>
  )
}
