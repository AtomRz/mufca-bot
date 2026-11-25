import React, { useState } from 'react'
import { api } from '../api'
import { useI18n } from '../i18n'
import { SimScopeFields, SimStatus, BarCoverageWarning, useSimJob, barsError } from './InsightsPanel'

const PARAM_ORDER = ['FRAMA_LEN', 'FRAMA_MULT', 'MFI_LEN', 'AND_LEN', 'AND_SIG_LEN', 'UT_SENSITIVITY', 'UT_PERIOD']
const PARAM_LABELS = {
  FRAMA_LEN: 'FRAMA length', FRAMA_MULT: 'FRAMA mult', MFI_LEN: 'MFI length', AND_LEN: 'Andean length',
  AND_SIG_LEN: 'Andean signal', UT_SENSITIVITY: 'UT sensitivity', UT_PERIOD: 'UT period',
}

const pct = (v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
const statText = (st, t) => t('n={n} avg {avg} total {total}', { n: st.n, avg: pct(st.avg), total: pct(st.total) })

// Parameters whose value differs from the live ones, as "FRAMA length 22 → 34".
function changes(params, live) {
  return PARAM_ORDER.filter((k) => params?.[k] !== live?.[k]).map((k) => ({ k, from: live?.[k], to: params?.[k] }))
}

function ParamDiff({ params, live }) {
  const { t } = useI18n()
  const diff = changes(params, live)
  if (diff.length === 0) return <span>{t('same as live')}</span>
  return (
    <span>
      {diff.map(({ k, from, to }, i) => (
        <span key={k}>{i > 0 ? ' · ' : ''}{t(PARAM_LABELS[k])} {from} → <b>{to}</b></span>
      ))}
    </span>
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
        <div key={`${ticker}-${tf}`} style={{ fontSize: 12, marginBottom: 10 }}>
          <div style={{ fontWeight: 600 }}>
            {ticker} {tf}{' '}
            {r.changed
              ? <span style={{ color: 'var(--long)' }}>{t('✅ worth a forward test')}</span>
              : <span style={{ color: 'var(--text-faint)' }}>{t('unchanged')}</span>}
          </div>
          {r.changed ? (
            <div style={{ color: 'var(--text-faint)' }}>
              <ParamDiff params={r.chosen} live={r.current} /><br />
              {t('now {now} · was {was}', { now: statText(r.best_stats.all, t), was: statText(r.current_stats.all, t) })}<br />
              {t('out-of-sample: now {now} · was {was}', { now: statText(r.best_stats.test, t), was: statText(r.current_stats.test, t) })}
              {r.plateau?.share_better != null && (
                <><br />{t('stability: {pct}% of {n} neighbouring settings also beat the live ones', { pct: Math.round(r.plateau.share_better * 100), n: r.plateau.n })}</>
              )}
            </div>
          ) : (
            <div style={{ color: 'var(--text-faint)' }}>
              {r.reason}
              {r.best && changes(r.best, r.current).length > 0 && (
                <><br />{t('best raw candidate (rejected):')} <ParamDiff params={r.best} live={r.current} /></>
              )}
            </div>
          )}
          {r.n_failed > 0 && (
            <div style={{ color: 'var(--accent)' }}>{t('⚠ {failed} of {total} backtests failed and were left out', { failed: r.n_failed, total: r.n_trials + r.n_failed })}</div>
          )}
        </div>
      ))}
    </div>
  )
}

export default function IndicatorOptimizerPanel({ config }) {
  const { t } = useI18n()
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(3000)
  const [trials, setTrials] = useState(60)
  const sim = useSimJob('indoptim')
  const result = sim.job?.status === 'done' ? sim.job.result : null
  const ranBars = sim.job?.params?.num_bars ?? numBars

  const start = () => {
    const bad = barsError(numBars)
    if (bad) { sim.setError(bad); return }
    if (!Number.isInteger(trials) || trials < 8 || trials > 400) { sim.setError(t('Settings to try must be a whole number between 8 and 400')); return }
    sim.start(() => api.runIndicatorOptimize({ ticker, tf, numBars, trials }))
  }

  return (
    <div className="panel">
      <h3 className="panel-title">{t('Indicator optimizer')}</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 12 }}>
        {t('For each pair × timeframe, backtests a spread of FRAMA / MFI / Andean / UT Bot settings (the live ones are always included) with that pair\'s current filters and the fee deducted, and shows whether other settings would have done better. The best is picked on the older 60% of history, must beat the live settings by more than the noise, must confirm on the newest 40%, and its neighbouring settings must also do well — so most pairs will honestly say "unchanged". Preview only: nothing is applied, indicator settings stay global.')}
      </p>
      <SimScopeFields
        pairs={config.pairs} timeframes={config.timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={
          <>
            <div>
              <label style={{ display: 'block', fontSize: 11, color: 'var(--text-faint)', marginBottom: 4 }}>{t('Settings to try')}</label>
              <input type="number" value={trials} min={8} max={400} step={10}
                     onChange={(e) => setTrials(Number(e.target.value))} style={{ width: 80 }} />
            </div>
            <button onClick={start} disabled={sim.running}>{sim.running ? t('Running…') : t('Optimize')}</button>
          </>
        }
      />
      <SimStatus sim={sim} />
      {result && !sim.running && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          <p style={{ fontSize: 12, marginBottom: 8 }}>
            {t('Net of {fee}% fee per trade · ~{trials} settings per pair/timeframe · nothing applied', { fee: result.fee_pct, trials: result.trials })}
          </p>
          {(result.skipped || []).map((sk) => (
            <p key={`${sk.ticker}-${sk.tf}`} style={{ fontSize: 12, color: 'var(--short)', marginBottom: 6 }}>
              {t('❌ {ticker} {tf}: skipped — {reason}', { ticker: sk.ticker, tf: sk.tf, reason: sk.reason })}
            </p>
          ))}
          <ResultRows result={result} />
          <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
            {t('The best of dozens of tries is optimistic by construction — treat a ✅ as a candidate to forward-test, not a guarantee. Set the values in Settings → Indicators if you want to try them live. Also in Discord: !indoptim.')}
          </p>
        </>
      )}
    </div>
  )
}
