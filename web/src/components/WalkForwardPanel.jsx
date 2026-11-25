import React, { useState } from 'react'
import { api } from '../api'
import { useI18n } from '../i18n'
import { SimScopeFields, SimStatus, BarCoverageWarning, useSimJob, barsError } from './InsightsPanel'

const FILTER_ORDER = ['frama', 'slope', 'chop', 'atr', 'htf', 'fake_break', 'liq_sweep', 'hurst']

const describeSet = (flags, t) => {
  const off = FILTER_ORDER.filter((k) => !flags?.[k])
  return off.length === 0 ? t('all on') : t('off: {list}', { list: off.join(', ') })
}
const pct = (v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
const stat = (st, t) => t('n={n} total {total}', { n: st.n, total: pct(st.total) })

const VERDICTS = {
  robust: { icon: '✅', label: 'robust', color: 'var(--long)' },
  stable: { icon: '🟦', label: 'stable — nothing to re-tune', color: 'var(--text-faint)' },
  not_robust: { icon: '❌', label: 'not robust', color: 'var(--short)' },
  inconclusive: { icon: '🟨', label: 'inconclusive', color: 'var(--accent)' },
  no_data: { icon: '⬜', label: 'no data', color: 'var(--text-faint)' },
}

const AUDIT_STATUS = { holding: '✅', worse: '❌', no_difference: '➖', too_early: '⏳' }
const AUDIT_LABEL = { holding: 'holding up', worse: 'worse', no_difference: 'no difference', too_early: 'too early' }

function AuditRow({ name, w }) {
  const { t } = useI18n()
  if (!w) return null
  return (
    <div>
      {AUDIT_STATUS[w.status] || ''} {t('{name}: pinned {pinned} vs before {before} →', { name: t(name), pinned: stat(w.pinned, t), before: stat(w.reference, t) })}{' '}
      <b>{pct(w.gain.gain)}</b> {t('(noise {noise}%) — {status}', { noise: w.gain.noise.toFixed(2), status: t(AUDIT_LABEL[w.status] || w.status.replace('_', ' ')) })}
    </div>
  )
}

function PairResult({ ticker, tf, r }) {
  const { t } = useI18n()
  const wf = r.walk_forward
  const v = VERDICTS[wf.verdict] || { icon: '', label: wf.verdict, color: 'var(--text-faint)' }
  const agg = wf.aggregate
  const stab = wf.stability
  const thin = wf.folds.map((f, i) => (f.thin ? i + 1 : null)).filter(Boolean)
  return (
    <div style={{ fontSize: 12, marginBottom: 12 }}>
      <div style={{ fontWeight: 600 }}>
        {ticker} {tf} <span style={{ color: v.color }}>{v.icon} {t(v.label)}</span>
      </div>
      <div style={{ color: 'var(--text-faint)' }}>
        {wf.reason}<br />
        {r.current_pinned ? t('current (pinned): {set}', { set: describeSet(r.current, t) }) : t('current: {set}', { set: describeSet(r.current, t) })}<br />
        {t('out-of-sample, all folds — re-tuned {policy} · current {current} · all on {allOn} · all off {allOff}', {
          policy: stat(agg.policy, t), current: stat(agg.current, t), allOn: stat(agg.all_on, t), allOff: stat(agg.all_off, t),
        })}
        {agg.folds_switched > 0 && (
          <><br />{t('switched in {switched}/{folds} folds: saw {inSample} in training, got {outSample} on the next block', {
            switched: agg.folds_switched, folds: agg.folds, inSample: pct(agg.in_sample_gain), outSample: pct(agg.out_of_sample_gain),
          })}</>
        )}
        {stab.folds_scored > 0 && stab.modal_pick && (
          <><br />{t('stability: {picks} distinct pick(s) in {folds} folds; most common ({share}%): {set}', {
            picks: stab.distinct_picks, folds: stab.folds_scored, share: Math.round(stab.modal_share * 100), set: describeSet(stab.modal_pick, t),
          })}</>
        )}
        {thin.length > 0 && <><br />{t('⚠ thin test block(s): {list}', { list: thin.join(', ') })}</>}
        {r.audit && (
          <div style={{ marginTop: 4 }}>
            {t('📌 pinned-set audit vs {ref}:', { ref: r.audit.reference_kind === 'previous pin' ? t('the set before the pin') : t('the global toggles') })}
            <AuditRow name="since it was pinned" w={r.audit.since_pin} />
            <AuditRow name="newest bars" w={r.audit.recent} />
          </div>
        )}
        {r.current_pinned && <div>{t('(the current set was itself tuned on this history, so its numbers here are optimistic)')}</div>}
      </div>
    </div>
  )
}

export default function WalkForwardPanel({ config }) {
  const { t } = useI18n()
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
    if (!Number.isInteger(folds) || folds < 2 || folds > 12) { sim.setError(t('Folds must be a whole number between 2 and 12')); return }
    sim.start(() => api.runWalkForward({ ticker, tf, numBars, folds }))
  }

  const rows = []
  Object.entries(result?.results || {}).forEach(([tk, perTf]) => {
    Object.entries(perTf).forEach(([f, r]) => rows.push({ tk, f, r }))
  })

  return (
    <div className="panel">
      <h3 className="panel-title">{t('Walk-forward check')}</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 12 }}>
        {t('Auto-tune confirms its pick on one out-of-sample window. This cuts the history into several train → test folds, lets the tuner choose on each training part only and trades that choice on the next block it has never seen — so you see whether re-tuning keeps its edge or just fits noise, and how stable the chosen set is. Sets that are already pinned are also compared with the set they replaced on the bars after they were pinned. Dry run: nothing is applied.')}
      </p>
      <SimScopeFields
        pairs={config.pairs} timeframes={config.timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={
          <>
            <div>
              <label style={{ display: 'block', fontSize: 11, color: 'var(--text-faint)', marginBottom: 4 }}>{t('Folds')}</label>
              <input type="number" value={folds} min={2} max={12} step={1}
                     onChange={(e) => setFolds(Number(e.target.value))} style={{ width: 70 }} />
            </div>
            <button onClick={start} disabled={sim.running}>{sim.running ? t('Running…') : t('Run check')}</button>
          </>
        }
      />
      <SimStatus sim={sim} />
      {result && !sim.running && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          <p style={{ fontSize: 12, marginBottom: 8 }}>
            {t('{folds} test folds after the first {pct}% of the history · net of {fee}% fee per trade · nothing applied', {
              folds: result.folds, pct: Math.round(result.initial_fraction * 100), fee: result.fee_pct,
            })}
          </p>
          {(result.skipped || []).map((sk) => (
            <p key={`${sk.ticker}-${sk.tf}`} style={{ fontSize: 12, color: 'var(--short)', marginBottom: 6 }}>
              {t('❌ {ticker} {tf}: skipped — {reason}', { ticker: sk.ticker, tf: sk.tf, reason: sk.reason })}
            </p>
          ))}
          {rows.length === 0
            ? <p style={{ fontSize: 12, color: 'var(--text-faint)' }}>{t('No pair/timeframe produced a result.')}</p>
            : rows.map(({ tk, f, r }) => <PairResult key={`${tk}-${f}`} ticker={tk} tf={f} r={r} />)}
          <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
            {t('✅ robust = re-tuning gained out-of-sample beyond the noise; ❌ = in-sample gains did not carry over, keep the current sets. Small samples stay noisy — re-run as bars accumulate. Also in Discord: !walkfwd.')}
          </p>
        </>
      )}
    </div>
  )
}
