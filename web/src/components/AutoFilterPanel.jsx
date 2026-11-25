import React, { useState } from 'react'
import { api } from '../api'
import { useI18n } from '../i18n'
import { SimScopeFields, SimStatus, BarCoverageWarning, useSimJob, barsError } from './InsightsPanel'

const FILTER_ORDER = ['frama', 'slope', 'chop', 'atr', 'htf', 'fake_break', 'liq_sweep', 'hurst']

const offList = (flags) => FILTER_ORDER.filter((k) => !flags?.[k])
const describeSet = (flags, t) => {
  const off = offList(flags)
  return off.length === 0 ? t('all on') : t('off: {list}', { list: off.join(', ') })
}
const pct = (v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
const statText = (st, t) => t('n={n} avg {avg} total {total}', { n: st.n, avg: pct(st.avg), total: pct(st.total) })

const FLIP_MARK = { helps: '✅', hurts: '❌', mixed: '▫️', no_effect: '—' }

// The checks a proposal had to pass: each changed filter flipped alone, then the walk-forward gate.
function CheckLines({ chk }) {
  const { t } = useI18n()
  if (!chk) return null
  const wf = chk.walk_forward
  return (
    <div style={{ color: 'var(--text-faint)' }}>
      {chk.flips.map((f) => (
        <div key={f.filter}>
          {t('{mark} {filter} → {state} alone: net {delta} [early {early} | late {late}] ({extra} trades) — {verdict}', {
            mark: FLIP_MARK[f.verdict] || '▫️', filter: f.filter, state: f.to ? t('on') : t('off'), delta: pct(f.delta),
            early: pct(f.early), late: pct(f.late), extra: `${f.extra_trades >= 0 ? '+' : ''}${f.extra_trades}`, verdict: t(String(f.verdict).replace('_', ' ')),
          })}
        </div>
      ))}
      {chk.trimmed && (
        <div>{t('✂️ trimmed to {filters}: net {delta} [early {early} | late {late}]', { filters: chk.trimmed.filters.join(', '), delta: pct(chk.trimmed.delta), early: pct(chk.trimmed.early), late: pct(chk.trimmed.late) })}</div>
      )}
      {wf && (
        <div>
          {t('{mark} walk-forward {verdict} ({mode}): switched in {switched}/{folds} folds, better in {better}; in-sample {ins} → out-of-sample {oos}', {
            mark: wf.passed ? '✅' : '⛔', verdict: t(String(wf.verdict).replace('_', ' ')), mode: wf.mode === 'not_worse' ? t('lenient') : t('strict'),
            switched: wf.folds_switched, folds: wf.folds, better: wf.folds_switched_positive,
            ins: pct(wf.in_sample_gain), oos: pct(wf.out_of_sample_gain),
          })}
        </div>
      )}
      {chk.decision === 'keep' && <div style={{ color: 'var(--short)' }}>{t('⛔ not applied ({by}): {reason}', { by: chk.blocked_by, reason: chk.reason })}</div>}
    </div>
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
      {rows.map(({ ticker, tf, r }) => (
        <div key={`${ticker}-${tf}`} style={{ fontSize: 12, marginBottom: 8 }}>
          <div style={{ fontWeight: 600 }}>
            {ticker} {tf}{' '}
            {r.changed
              ? <span style={{ color: 'var(--long)' }}>{result.applied ? t('✅ pinned') : t('✅ would pin')}: {describeSet(r.chosen, t)}</span>
              : <span style={{ color: 'var(--text-faint)' }}>{t('unchanged ({set})', { set: describeSet(r.current, t) })}</span>}
          </div>
          {r.changed ? (
            <div style={{ color: 'var(--text-faint)' }}>
              {t('now {now} · was {was} (was: {set})', { now: statText(r.best_stats.all, t), was: statText(r.current_stats.all, t), set: describeSet(r.current, t) })}
              {r.validated && <><br />{t('out-of-sample: now {now} · was {was}', { now: statText(r.best_stats.test, t), was: statText(r.current_stats.test, t) })}</>}
            </div>
          ) : (
            <div style={{ color: 'var(--text-faint)' }}>
              {r.reason}
              {r.proposed && <><br />{t('🔎 the tuner proposed {set}', { set: describeSet(r.proposed, t) })}</>}
            </div>
          )}
          <CheckLines chk={r.checks} />
        </div>
      ))}
    </div>
  )
}

function PinnedSets({ overrides, tracked, busy, onRevert, onClear, onRemove }) {
  const { t } = useI18n()
  const active = overrides?.active || {}
  const rows = []
  Object.keys(active).sort().forEach((ticker) => {
    Object.keys(active[ticker]).sort().forEach((tf) => rows.push({ ticker, tf, flags: active[ticker][tf] }))
  })
  const canRevert = rows.length > 0 || Object.keys(overrides?.previous || {}).length > 0
  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>{t('Pinned filter sets')}</div>
      {rows.length === 0 ? (
        <p style={{ fontSize: 12, color: 'var(--text-faint)' }}>{t('None — every pair/timeframe uses the global toggles above.')}</p>
      ) : (
        rows.map(({ ticker, tf, flags }) => (
          <div className="row" key={`${ticker}-${tf}`} style={{ fontSize: 12 }}>
            <span className="row-label">
              {ticker} {tf}
              {tracked && !tracked.includes(ticker) && <span style={{ color: 'var(--accent)' }}> · {t('pair no longer tracked')}</span>}
            </span>
            <span className="row-value">
              {describeSet(flags, t)}{' '}
              <button className="btn" disabled={busy} title={t('Remove this pin (the pair/timeframe goes back to the global toggles)')}
                      onClick={() => onRemove(ticker, tf)} style={{ marginLeft: 6, padding: '0 8px' }}>✕</button>
            </span>
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

const FILTER_LABELS = {
  frama: 'FRAMA trend', slope: 'FRAMA slope', chop: 'CHOP', atr: 'ATR', htf: 'HTF bias',
  fake_break: 'Fake breakout', liq_sweep: 'Liquidity sweep', hurst: 'Hurst',
}

// Hand-edit the filters of ONE pair/timeframe. Shows what is in force right now
// (the pinned set, else the global toggles); flipping a switch pins the pair.
function ManualPin({ config, busy, act }) {
  const { t } = useI18n()
  const pairs = config.pairs || []
  const tfs = config.timeframes || []
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const tk = ticker || pairs[0] || ''
  const f = tf || tfs[0] || ''
  const pinned = config.filter_overrides?.active?.[tk]?.[f]
  const flags = pinned || config.filter_toggles || {}
  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>{t('Set filters by hand')}</div>
      <div style={{ display: 'flex', gap: 10, marginBottom: 8 }}>
        <select value={tk} onChange={(e) => setTicker(e.target.value)}>
          {pairs.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <select value={f} onChange={(e) => setTf(e.target.value)}>
          {tfs.map((x) => <option key={x} value={x}>{x}</option>)}
        </select>
      </div>
      <p style={{ fontSize: 11, color: 'var(--text-faint)', marginBottom: 6 }}>
        {pinned ? t('Pinned for this pair/timeframe.') : t('Following the global toggles — flipping a switch pins this pair/timeframe.')}
      </p>
      {FILTER_ORDER.map((key) => (
        <div className="toggle-row" key={key}>
          <span className="row-label">{t(FILTER_LABELS[key])}</span>
          <label className="switch">
            <input type="checkbox" checked={!!flags[key]} disabled={busy || !tk || !f}
                   onChange={(e) => act(() => api.pinFilters(tk, f, { [key]: e.target.checked }))} />
            <span className="switch-track" />
          </label>
        </div>
      ))}
      <button className="btn" style={{ marginTop: 8 }} disabled={busy || !pinned}
              onClick={() => act(() => api.pinFilters(tk, f, null))}>
        {t('Remove pin (use global toggles)')}
      </button>
    </div>
  )
}

export default function AutoFilterPanel({ config, onChanged }) {
  const { t } = useI18n()
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
      <h3 className="panel-title">{t('Auto-tune filters')}</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 12 }}>
        {t('Backtests every pair × timeframe, tries all combinations of the 8 backtestable filters and pins the set with the best PnL (after fees) for each one — so e.g. CHOP can stay on for BTC and be off for ETH. The set is chosen on the older 60% of history and only replaces the current one if it also holds up on the newest 40%. A change is applied only if it also passes two more checks in the same run: every filter it touches, flipped on its own, must make more money in BOTH the older and the newer part of the history (a change that fails is dropped), and the rolling walk-forward of re-tuning that pair/timeframe (the same check as the Walk-forward panel) must be robust. Otherwise the pair keeps its filters and the result says which check stopped it. Spread is live-only and stays global.')}
      </p>
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
      <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, marginBottom: 6 }}>
        <input type="checkbox" checked={lenient} disabled={skipOos} onChange={(e) => setLenient(e.target.checked)} />
        {t('Lenient walk-forward: let a change through unless the walk-forward is "not robust" (inconclusive is enough)')}
      </label>
      <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, marginBottom: 10 }}>
        <input type="checkbox" checked={skipOos} onChange={(e) => setSkipOos(e.target.checked)} />
        {t('Skip ALL checks (no out-of-sample guard, no per-filter or walk-forward check — maximises past PnL, overfits more)')}
      </label>
      <SimStatus sim={sim} />
      {localError && <p style={{ color: 'var(--short)', fontSize: 13 }}>{t('Failed: {error}', { error: localError })}</p>}
      {result && !sim.running && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          <p style={{ fontSize: 12, marginBottom: 8 }}>
            {result.applied ? t('Net of {fee}% fee per trade · applied', { fee: result.fee_pct }) : t('Net of {fee}% fee per trade · nothing applied', { fee: result.fee_pct })}
          </p>
          {(result.skipped || []).map((sk) => (
            <p key={`${sk.ticker}-${sk.tf}`} style={{ fontSize: 12, color: 'var(--short)', marginBottom: 6 }}>
              {t('❌ {ticker} {tf}: skipped — {reason}', { ticker: sk.ticker, tf: sk.tf, reason: sk.reason })}
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
        {t('Pinned sets win over the global toggles for that pair/timeframe. Also available in Discord: !autofilter.')}
      </p>
    </div>
  )
}
