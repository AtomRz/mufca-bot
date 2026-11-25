import React, { useState } from 'react'
import { api } from '../api'
import { useI18n } from '../i18n'
import { SimScopeFields, SimStatus, BarCoverageWarning, useSimJob, barsError } from './InsightsPanel'

const faint = { color: 'var(--text-faint)' }
const th = { textAlign: 'left', fontWeight: 500, padding: '2px 8px 2px 0', color: 'var(--text-faint)' }
const td = { padding: '2px 8px 2px 0', verticalAlign: 'top' }
const signed = (v, d = 3) => `${v >= 0 ? '+' : ''}${v.toFixed(d)}%`
const share = (v) => `${Math.round(v * 100)}%`

const GRADE = { both: '✅', turn: '🟦', trade: '🟨', none: '▫️' }

function StateTable({ rows }) {
  const { t } = useI18n()
  const shown = rows.filter((c) => c.grade !== 'none').slice(0, 8)
  if (shown.length === 0) return <div style={faint}>{t('no state stands out')}</div>
  return (
    <table style={{ borderCollapse: 'collapse', marginTop: 2 }}>
      <thead>
        <tr>
          <th style={th}></th><th style={th}>{t('state')}</th><th style={th}>{t('swings (no trigger)')}</th>
          <th style={th}>{t('turn lift')}</th><th style={th}>{t('early / late')}</th>
          <th style={th}>{t('bracket avg')}</th><th style={th}>{t('early / late')}</th>
        </tr>
      </thead>
      <tbody>
        {shown.map((c) => (
          <tr key={`${c.feature}-${c.value}`}>
            <td style={td}>{GRADE[c.grade]}</td>
            <td style={td}>{c.feature.split(' & ').map((f, k) => (
              <div key={f}>{f} = <b>{c.value.split(' & ')[k]}</b></div>
            ))}</td>
            <td style={td}>{c.n_legs} {t('({none}, Σ leg size {pct}%)', { none: c.n_none, pct: c.none_leg_pct.toFixed(0) })}</td>
            <td style={td}>×{c.all.lift.toFixed(1)} (z={c.all.z.toFixed(1)})</td>
            <td style={td}>×{c.early.lift.toFixed(1)} / ×{c.late.lift.toFixed(1)}</td>
            <td style={td}>{signed(c.all.avg_net)} {t('(t={t}, win {win})', { t: c.all.t.toFixed(1), win: share(c.all.win) })}</td>
            <td style={td}>{signed(c.early.avg_net)} n={c.early.ctrl} / {signed(c.late.avg_net)} n={c.late.ctrl}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function PairBlock({ ticker, tf, r }) {
  const { t } = useI18n()
  const { legs, baseline: bs, bracket: br, grades: g } = r
  return (
    <div style={{ fontSize: 12, marginBottom: 16 }}>
      <div style={{ fontWeight: 600 }}>{ticker} {tf}</div>
      <div style={faint}>
        {t('{total} swings ≥ {mult}×ATR: {caught} caught, {filtered} filtered, {busy} busy,', { total: legs.total, mult: r.pivot_atr_mult, caught: legs.caught, filtered: legs.filtered, busy: legs.busy })}{' '}
        <b>{t('{n} no trigger', { n: legs.none })}</b> · {t('control = {n} bars (every {stride}th, both sides)', { n: bs.all.ctrl, stride: r.stride })}
        <br />
        {t('base: a bar starts a turn {rate}% of the time; a plain {sl}/{tp}×ATR bracket wins {win}, avg {avg} net', { rate: (bs.all.turn_rate * 100).toFixed(2), sl: br.sl_atr, tp: br.tp_atr, win: share(bs.all.win), avg: signed(bs.all.avg_net) })}
        <br />
        {t('{n} states tested ({singles} single, {pairs} pairs): ✅ {both} · 🟦 turn only {turn} · 🟨 trade only {trade}', { n: r.n_tested, singles: r.n_singles, pairs: r.n_pairs, both: g.both, turn: g.turn, trade: g.trade })}
      </div>
      <div style={{ marginTop: 6 }}><StateTable rows={r.candidates} /></div>
      <div style={{ marginTop: 4 }}>
        → {g.both
          ? t('candidates to forward-test (not a finding)')
          : t('nothing holds — no new signal type is justified by these features on this pair')}
      </div>
    </div>
  )
}

export default function MissedSimPanel({ config }) {
  const { t } = useI18n()
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(3000)
  const [swing, setSwing] = useState(3)
  const sim = useSimJob('missedsim')
  const result = sim.job?.status === 'done' ? sim.job.result : null
  const ranBars = sim.job?.params?.num_bars ?? numBars

  const start = () => {
    const bad = barsError(numBars)
    if (bad) { sim.setError(bad); return }
    if (!(swing >= 1 && swing <= 10)) { sim.setError(t('Swing size must be between 1 and 10 ATRs')); return }
    sim.start(() => api.runMissedSim({ ticker, tf, numBars, swing }))
  }

  const rows = []
  Object.entries(result?.results || {}).forEach(([tk, perTf]) => {
    Object.entries(perTf).forEach(([f, r]) => rows.push({ ticker: tk, tf: f, r }))
  })

  return (
    <div className="panel">
      <h3 className="panel-title">{t('Missed-reversal structure')}</h3>
      <p style={{ fontSize: 12, ...faint, marginBottom: 12 }}>
        {t('Most of the loss is not on filters or stops but on reversals where no trigger fired. This reads the market state two bars after every swing of at least the chosen number of ATRs (using data up to that bar only): length and size of the trend it reverses, ATR percentile and direction, volume spike / dry-up, a sweep of the 100-bar extreme, position against the volume-profile band and POC, MFI / Andean divergence, a CHoCH-like break, a fair-value gap, the session, and the usual indicator states. The same states are read on ordinary bars (control) and a plain bracket trade (stop = target = 1.5×ATR) is simulated from each. ✅ = the state marks turns more often than the control AND the bracket trade paid net of fee, in BOTH the older 60% and the newest 40% of the history. 🟦 = marks turns but did not pay (a turn leaves traces in the bars after it, so this alone is weak), 🟨 = paid but does not mark turns. Pairs of states are tried only for the strongest singles. "Nothing holds" is a valid result. Measurement only: nothing is applied.')}
      </p>
      <SimScopeFields
        pairs={config.pairs} timeframes={config.timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={
          <>
            <div>
              <label style={{ display: 'block', fontSize: 11, ...faint, marginBottom: 4 }}>{t('Swing (× ATR)')}</label>
              <input type="number" value={swing} min={1} max={10} step={0.5}
                     onChange={(e) => setSwing(Number(e.target.value))} style={{ width: 70 }} />
            </div>
            <button onClick={start} disabled={sim.running}>{sim.running ? t('Running…') : t('Run study')}</button>
          </>
        }
      />
      <SimStatus sim={sim} />
      {result && !sim.running && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          <p style={{ fontSize: 12, marginBottom: 8 }}>
            {t('Net of {fee}% fee per trade · early window = oldest {pct}% of the bars · nothing applied', { fee: result.fee_pct, pct: Math.round(result.train_fraction * 100) })}
          </p>
          {(result.skipped || []).map((sk) => (
            <p key={`${sk.ticker}-${sk.tf}`} style={{ fontSize: 12, color: 'var(--short)', marginBottom: 6 }}>
              {t('❌ {ticker} {tf}: skipped — {reason}', { ticker: sk.ticker, tf: sk.tf, reason: sk.reason })}
            </p>
          ))}
          {rows.length === 0
            ? <p style={{ fontSize: 12, ...faint }}>{t('No pair/timeframe produced a result.')}</p>
            : rows.map(({ ticker: tk, tf: f, r }) => <PairBlock key={`${tk}-${f}`} ticker={tk} tf={f} r={r} />)}
          {(result.consistent || []).length > 0 && (
            <div style={{ fontSize: 12, marginBottom: 10 }}>
              <div>{t('🔗 Holds on every pair of the same timeframe')}</div>
              {result.consistent.map((c) => (
                <div key={`${c.tf}-${c.feature}-${c.value}`}>✅ {c.tf}: {c.feature} = <b>{c.value}</b> <span style={faint}>({c.pairs.join(', ')})</span></div>
              ))}
            </div>
          )}
          <p style={{ fontSize: 11, ...faint }}>
            {t('Dozens of states are tested per pair, so a few will look good by chance — forward-test before building anything. Also in Discord: !missedsim.')}
          </p>
        </>
      )}
    </div>
  )
}
