import React, { useState } from 'react'
import { api } from '../api'
import { useI18n } from '../i18n'
import { SimScopeFields, SimStatus, BarCoverageWarning, useSimJob, barsError } from './InsightsPanel'

const pct = (v) => (Number.isFinite(v) ? `${v >= 0 ? '+' : ''}${v.toFixed(2)}%` : '—')
const statText = (st, t) => (st ? t('n={n} avg {avg} total {total}', { n: st.n, avg: pct(st.avg), total: pct(st.total) }) : '—')
const thrText = (v, t) => (!v ? t('off') : Number(v).toFixed(2))
const METRICS = ['bar', 'smooth', 'thin', 'how']
const METRIC_HELP = {
  bar: 'this bar ÷ median of the baseline',
  smooth: 'mean of the last 3 bars ÷ median of the baseline',
  thin: 'normal price move per unit of volume ÷ this bar\'s (low = big move on thin volume)',
  how: 'this bar ÷ same hour of the week over the past weeks',
}
// {metric, threshold} (or a bare number = bar) -> "smooth ≥ 0.55" / "off"
const ruleText = (r, t) => {
  if (r && typeof r === 'object') return !r.threshold ? t('off') : `${r.metric} ≥ ${Number(r.threshold).toFixed(2)}`
  return thrText(r, t)
}

function GridTable({ rows, recommended, metric }) {
  const { t } = useI18n()
  if (!rows || rows.length === 0) return null
  return (
    <details style={{ marginTop: 4 }}>
      <summary style={{ cursor: 'pointer', color: 'var(--text-faint)' }}>{t('threshold grid')}{metric ? ` (${metric})` : ''}</summary>
      <table style={{ fontSize: 11, marginTop: 4, borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ color: 'var(--text-faint)', textAlign: 'right' }}>
            <th style={{ textAlign: 'left', paddingRight: 10 }}>{t('min value')}</th>
            <th style={{ paddingRight: 10 }}>{t('blocked (train)')}</th>
            <th style={{ paddingRight: 10 }}>{t('gain (train)')}</th>
            <th style={{ paddingRight: 10 }}>{t('blocked (test)')}</th>
            <th>{t('gain (test)')}</th>
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
      <div style={{ fontSize: 10, color: 'var(--text-faint)' }}>{t('gain = change of net total PnL vs. no gate · "·" = passed the training checks')}</div>
    </details>
  )
}

function ResultRows({ result }) {
  const { t } = useI18n()
  const rows = []
  Object.entries(result.results || {}).forEach(([ticker, perTf]) => {
    Object.entries(perTf).forEach(([tf, r]) => rows.push({ ticker, tf, r }))
  })
  if (rows.length === 0) return <p style={{ fontSize: 12, color: 'var(--text-faint)' }}>{t('No pair/timeframe produced a result.')}</p>
  return (
    <div style={{ marginBottom: 10 }}>
      {rows.map(({ ticker, tf, r }) => {
        const dt = r.by_daytype || {}
        const wb = r.weekend_blocked
        return (
          <div key={`${ticker}-${tf}`} style={{ fontSize: 12, marginBottom: 10 }}>
            <div style={{ fontWeight: 600 }}>
              {ticker} {tf}{' '}
              {r.recommended === null && <span style={{ color: 'var(--text-faint)' }}>{t('➖ left as is ({rule})', { rule: ruleText(r.current, t) })}</span>}
              {r.recommended !== null && r.changed && (
                <span style={{ color: 'var(--long)' }}>
                  {t(result.applied ? '✅ pinned: {rule} (was {was})' : '✅ would pin: {rule} (was {was})', {
                    rule: r.recommended.threshold ? ruleText(r.recommended, t) : t('gate off for this pair'), was: ruleText(r.current, t),
                  })}
                </span>
              )}
              {r.recommended !== null && !r.changed && <span style={{ color: 'var(--text-faint)' }}>{t('unchanged ({rule})', { rule: ruleText(r.current, t) })}</span>}
            </div>
            <div style={{ color: 'var(--text-faint)' }}>{r.reason}</div>
            {r.metrics && Object.keys(r.metrics).length > 0 && (
              <div style={{ color: 'var(--text-faint)' }}>
                {t('best on training:')}{' '}
                {Object.entries(r.metrics).map(([m, mr], i) => (
                  <span key={m}>
                    {i > 0 && ' · '}
                    {mr.best_threshold !== null && mr.best_threshold !== undefined
                      ? `${m} ≥${mr.best_threshold.toFixed(2)} (${pct(mr.best_gain_train)})`
                      : `${m} ✗ (${t('top')} ${pct(mr.top_gain_train)})`}
                  </span>
                ))}
                {' '}{t('— noise bar {train}σ train / {test}σ test', { train: r.gain_k, test: r.test_k })}
                {Object.entries(r.metrics_unavailable || {}).map(([m, why]) => <span key={m}><br />{t('{metric}: skipped — {reason}', { metric: m, reason: why })}</span>)}
              </div>
            )}
            {r.chosen_stats && (
              <div style={{ color: 'var(--text-faint)' }}>
                {t('with gate {with} · without {without}', { with: statText(r.chosen_stats.all, t), without: statText(r.base_stats.all, t) })}
                {r.validated && <><br />{t('out-of-sample: with gate {with} · without {without}', { with: statText(r.chosen_stats.test, t), without: statText(r.base_stats.test, t) })}</>}
              </div>
            )}
            {dt.weekend?.n > 0 && dt.weekday?.n > 0 && (
              <div style={{ color: 'var(--text-faint)' }}>
                {t('weekend trades n={wn} avg {wavg} · weekdays n={dn} avg {davg} (net of fee)', {
                  wn: dt.weekend.n, wavg: pct(dt.weekend.avg), dn: dt.weekday.n, davg: pct(dt.weekday.avg),
                })}
              </div>
            )}
            {wb && wb.blocked > 0 && (
              <div style={{ color: 'var(--text-faint)' }}>
                {t('with {metric} ≥ {thr} the gate removes {blocked} trades, {weekend} of them on weekends (UTC)', {
                  metric: wb.metric, thr: wb.threshold.toFixed(2), blocked: wb.blocked, weekend: wb.weekend,
                })}
              </div>
            )}
            <GridTable rows={r.rows} recommended={r.recommended?.threshold} metric={r.rows_metric} />
          </div>
        )
      })}
    </div>
  )
}

function LiveRatios({ live, loading, error, onCheck }) {
  const { t } = useI18n()
  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
        <span style={{ fontSize: 13, fontWeight: 600 }}>{t('Live volume (bar / smooth / thin / how)')}</span>
        <button className="btn" disabled={loading} onClick={onCheck}>{loading ? t('Checking…') : t('Check now')}</button>
      </div>
      {error && <p style={{ color: 'var(--short)', fontSize: 12 }}>{t('Failed: {error}', { error })}</p>}
      {live && live.rows.map((r) => (
        <div className="row" key={`${r.ticker}-${r.tf}`} style={{ fontSize: 12 }}>
          <span className="row-label">{r.ticker} {r.tf}</span>
          <span className="row-value">
            {!r.values || Object.values(r.values).every((v) => v === null || v === undefined)
              ? t('n/a')
              : <>
                  {METRICS.map((m, i) => (
                    <span key={m}>
                      {i > 0 && ' / '}
                      {r.values[m] === null || r.values[m] === undefined
                        ? '–'
                        : (m === r.metric ? <b>{r.values[m].toFixed(2)}</b> : r.values[m].toFixed(2))}
                    </span>
                  ))}
                  {' '}· {t('rule {metric} ≥ {thr}', { metric: r.metric, thr: thrText(r.threshold, t) })}{' '}
                  <span style={{ color: r.would_block ? 'var(--short)' : 'var(--long)' }}>{r.would_block ? t('🔻 below') : t('✅ ok')}</span>
                </>}
          </span>
        </div>
      ))}
      {live && !live.enabled && (
        <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 4 }}>{t('The gate is off — nothing is blocked, this is only a preview.')}</p>
      )}
    </div>
  )
}

function PinnedThresholds({ gate, tracked, busy, onRevert, onClear }) {
  const { t } = useI18n()
  const rows = []
  Object.keys(gate.overrides || {}).sort().forEach((ticker) => {
    Object.keys(gate.overrides[ticker]).sort().forEach((tf) => rows.push({ ticker, tf, v: gate.overrides[ticker][tf] }))
  })
  const canRevert = rows.length > 0 || Object.keys(gate.previous || {}).length > 0
  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>{t('Pinned rules')}</div>
      {rows.length === 0 ? (
        <p style={{ fontSize: 12, color: 'var(--text-faint)' }}>{t('None — every pair/timeframe uses the default rule.')}</p>
      ) : (
        rows.map(({ ticker, tf, v }) => (
          <div className="row" key={`${ticker}-${tf}`} style={{ fontSize: 12 }}>
            <span className="row-label">
              {ticker} {tf}
              {tracked && !tracked.includes(ticker) && <span style={{ color: 'var(--accent)' }}> · {t('pair no longer tracked')}</span>}
            </span>
            <span className="row-value">{!v?.threshold ? t('gate off for this pair') : ruleText(v, t)}</span>
          </div>
        ))
      )}
      <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
        <button className="btn" disabled={busy || !canRevert} onClick={onRevert}>{t('Revert last change')}</button>
        <button className="btn" disabled={busy || rows.length === 0} onClick={onClear}>{t('Clear all')}</button>
      </div>
    </div>
  )
}

export default function VolumeGatePanel({ config, onChanged }) {
  const { t } = useI18n()
  const gate = config.volume_gate || { enabled: false, metric: 'bar', threshold: 0.5, baseline_days: 14, overrides: {}, previous: {} }
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(3000)
  const [skipOos, setSkipOos] = useState(false)
  const [fitMetrics, setFitMetrics] = useState(METRICS)
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
    if (fitMetrics.length === 0) { sim.setError(t('Pick at least one metric to compare')); return }
    setLocalError(null)
    sim.start(() => api.runVolumeFit({ ticker, tf, numBars, apply, validateOos: !skipOos, metrics: fitMetrics }))
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
      if (!Number.isFinite(v) || v < 0 || v > 5) throw new Error(t('Threshold must be a number between 0 and 5'))
      patch.threshold = v
    }
    if (daysDraft !== null) {
      const d = Number(daysDraft)
      if (!Number.isInteger(d) || d < 3 || d > 60) throw new Error(t('Baseline must be a whole number of days between 3 and 60'))
      patch.baseline_days = d
    }
    await api.setVolumeGate(patch)
    setThrDraft(null); setDaysDraft(null)
  })

  return (
    <div className="panel">
      <h3 className="panel-title">{t('Low-volume gate')}</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 12 }}>
        {t('Stops NEW entries while the signal bar\'s volume is far below what is normal for the pair/timeframe — thin trading (weekends, holidays, dead hours) is easy to push around. Four ways to read "thin" (all: higher = more liquid, the entry is blocked below the threshold): bar — this bar vs the baseline median; smooth — the last 3 bars; thin — price move per unit of volume; how — vs the same hour of the week. Weekends and holidays show up low on their own, no calendar. Blocked signals are not lost: they are tracked as shadow records so you can check what they would have done. Open trades are not affected.')}
      </p>

      <div className="toggle-row">
        <span className="row-label" title={t('Off by default. Pinned per-pair thresholds do nothing while this is off.')}>{t('Block entries on low volume')}</span>
        <label className="switch">
          <input type="checkbox" checked={!!gate.enabled} disabled={busy}
                 onChange={(e) => act(() => api.setVolumeGate({ enabled: e.target.checked }))} />
          <span className="switch-track" />
        </label>
      </div>

      <div style={{ display: 'flex', gap: 14, alignItems: 'flex-end', flexWrap: 'wrap', margin: '10px 0' }}>
        <div className="field" style={{ marginBottom: 0 }}>
          <label title={t('How "thin" is measured by default (wherever no pair/timeframe rule is pinned): {help}', { help: METRIC_HELP[gate.metric] ? t(METRIC_HELP[gate.metric]) : '' })}>{t('Default metric')}</label>
          <select value={gate.metric || 'bar'} disabled={busy}
                  onChange={(e) => act(() => api.setVolumeGate({ metric: e.target.value }))}>
            {METRICS.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </div>
        <div className="field" style={{ marginBottom: 0 }}>
          <label title={t('Default minimum ratio, used wherever no pair/timeframe threshold is pinned. 0.5 = block when the bar\'s volume is under half of normal.')}>{t('Default min value')}</label>
          <input type="number" step="0.05" min="0" max="5" value={thrValue} disabled={busy}
                 onChange={(e) => setThrDraft(e.target.value)} style={{ width: 90 }} />
        </div>
        <div className="field" style={{ marginBottom: 0 }}>
          <label title={t('How many prior days make up \'normal\' volume. Changing it changes the ratios, so re-run the auto-tune afterwards.')}>{t('Baseline (days)')}</label>
          <input type="number" step="1" min="3" max="60" value={daysValue} disabled={busy}
                 onChange={(e) => setDaysDraft(e.target.value)} style={{ width: 90 }} />
        </div>
        <button className="btn" disabled={busy || !dirty} onClick={save}>{t('Save')}</button>
      </div>

      <LiveRatios live={live} loading={liveLoading} error={liveError} onCheck={checkLive} />

      <div style={{ fontSize: 13, fontWeight: 600, margin: '16px 0 6px' }}>{t('Auto-tune thresholds')}</div>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 10 }}>
        {t('Backtests every pair × timeframe, records all four volume readings of every signal and tries thresholds 0.30–0.90 for each on top of the current filter set, scoring net PnL (after fees). The more metrics are compared, the higher the noise bar, and only the single best one is tested on the newest 40% (no second chances). A rule is picked on the older 60% of history and kept only if it beats noise, blocks enough trades to be measurable, sits on a plateau (neighbouring thresholds gain too) and is confirmed on the newest 40%. If nothing proves itself the gate stays off for that pair/timeframe — a normal result.')}
      </p>
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', fontSize: 12, marginBottom: 8 }}>
        <span style={{ color: 'var(--text-faint)' }}>{t('Compare:')}</span>
        {METRICS.map((m) => (
          <label key={m} title={t(METRIC_HELP[m])} style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
            <input type="checkbox" checked={fitMetrics.includes(m)} disabled={sim.running}
                   onChange={(e) => setFitMetrics((cur) => (e.target.checked ? METRICS.filter((x) => x === m || cur.includes(x)) : cur.filter((x) => x !== m)))} />
            {m}
          </label>
        ))}
      </div>
      <SimScopeFields
        pairs={config.pairs} timeframes={config.timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={
          <>
            <button onClick={() => start(true)} disabled={sim.running}>{sim.running ? t('Running…') : t('Tune & apply')}</button>
            <button className="btn" onClick={() => start(false)} disabled={sim.running}>{t('Preview only')}</button>
          </>
        }
      />
      <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, marginBottom: 10 }}>
        <input type="checkbox" checked={skipOos} onChange={(e) => setSkipOos(e.target.checked)} />
        {t('Skip the out-of-sample check (maximises past PnL, overfits more)')}
      </label>
      <SimStatus sim={sim} />
      {localError && <p style={{ color: 'var(--short)', fontSize: 13 }}>{t('Failed: {error}', { error: localError })}</p>}
      {result && !sim.running && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          <p style={{ fontSize: 12, marginBottom: 8 }}>
            {t('Net of {fee}% fee per trade · baseline {days} days · {state}', { fee: result.fee_pct, days: result.baseline_days, state: result.applied ? t('applied') : t('nothing applied') })}
          </p>
          {result.applied && !result.gate_enabled && (
            <p style={{ fontSize: 12, color: 'var(--accent)', marginBottom: 8 }}>
              {t('⚠️ The gate is off — the pinned rules do nothing until you switch it on above.')}
            </p>
          )}
          {(result.skipped || []).map((sk) => (
            <p key={`${sk.ticker}-${sk.tf}`} style={{ fontSize: 12, color: 'var(--short)', marginBottom: 6 }}>
              {t('❌ {ticker} {tf}: skipped — {reason}', { ticker: sk.ticker, tf: sk.tf, reason: sk.reason })}
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
        {t('Pinned rules win over the default for that pair/timeframe (off = no gate there). Also available in Discord: !volume, !volfit.')}
      </p>
    </div>
  )
}
