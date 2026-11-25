import React, { useState } from 'react'
import { api } from '../api'
import { useI18n } from '../i18n'
import { SimScopeFields, SimStatus, BarCoverageWarning, useSimJob, barsError } from './InsightsPanel'

const pct = (v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
const share = (v) => `${Math.round(v * 100)}%`
const faint = { color: 'var(--text-faint)' }
const th = { textAlign: 'left', fontWeight: 500, padding: '2px 8px 2px 0', color: 'var(--text-faint)' }
const td = { padding: '2px 8px 2px 0' }

// "frama_dir against, mfi_zone chasing, ..." - what the indicators showed on one bar.
const snapText = (snap) =>
  Object.entries(snap || {}).filter(([, v]) => v !== 'na').map(([k, v]) => `${k} ${v}`).join(' · ')

const reasonText = (m, t) =>
  m.status === 'none' ? t('no trigger') : m.status === 'busy' ? t('track busy') : t('filter {list}', { list: m.blockers.join('+') })

function LoserTable({ lf }) {
  const { t } = useI18n()
  const rows = lf.candidates.filter((c) => c.lift > 1 && c.skip_gain > 0).slice(0, 6)
  return (
    <div style={{ marginTop: 6 }}>
      <div>{t('📉 States that lost more')} <span style={faint}>{t('(average signal loses {rate}; {n} states tested)', { rate: share(lf.base_loss_rate), n: lf.n_tested })}</span></div>
      {rows.length === 0 ? <div style={faint}>{t('nothing stands out')}</div> : (
        <table style={{ borderCollapse: 'collapse', marginTop: 2 }}>
          <thead>
            <tr><th style={th}></th><th style={th}>{t('state')}</th><th style={th}>n</th><th style={th}>{t('loses')}</th>
              <th style={th}>{t('skipping it')}</th><th style={th}>{t('early / late')}</th></tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={`${c.feature}-${c.value}`}>
                <td style={td}>{c.holds ? '✅' : '▫️'}</td>
                <td style={td}>{c.feature} = <b>{c.value}</b></td>
                <td style={td}>{c.n}</td>
                <td style={td}>{share(c.loss_rate)} (×{c.lift.toFixed(1)}, z={c.z.toFixed(1)})</td>
                <td style={td}>{pct(c.skip_gain)}</td>
                <td style={td}>{pct(-c.early.net)} n={c.early.n} / {pct(-c.late.net)} n={c.late.n}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function MissedTable({ mf }) {
  const { t } = useI18n()
  const rows = mf.candidates.filter((c) => c.lift > 1).slice(0, 6)
  return (
    <div style={{ marginTop: 6 }}>
      <div>{t('🔄 States over-represented at missed reversals')} <span style={faint}>{t('(on average {share} are missed)', { share: share(mf.base_missed_share) })}</span></div>
      {rows.length === 0 ? <div style={faint}>{t('nothing stands out')}</div> : (
        <table style={{ borderCollapse: 'collapse', marginTop: 2 }}>
          <thead>
            <tr><th style={th}></th><th style={th}>{t('state')}</th><th style={th}>{t('missed / all')}</th><th style={th}>{t('lift')}</th></tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={`${c.feature}-${c.value}`}>
                <td style={td}>{c.holds ? '✅' : '▫️'}</td>
                <td style={td}>{c.feature} = <b>{c.value}</b></td>
                <td style={td}>{c.missed} / {c.n}</td>
                <td style={td}>×{c.lift.toFixed(1)} (z={c.z.toFixed(1)})</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

const VERDICT = {
  off_helps: '✅ off would help in both windows',
  keeps_value: '🛡️ earns its keep (worse off in both)',
  mixed: '▫️ windows disagree',
  no_effect: '— no effect',
}

// "What if ONE filter were off": the same triggers replayed with that filter alone disabled.
function FilterTable({ rows }) {
  const { t } = useI18n()
  if (!rows || rows.length === 0) return null
  const signed = (v, d = 1) => `${v >= 0 ? '+' : ''}${v.toFixed(d)}%`
  return (
    <div style={{ marginTop: 6 }}>
      <div>{t('🎚️ What if one filter were off')} <span style={faint}>{t('(same triggers, one position at a time, net of fee)')}</span></div>
      <table style={{ borderCollapse: 'collapse', marginTop: 2 }}>
        <thead>
          <tr><th style={th}>{t('filter')}</th><th style={th}>{t('trades')}</th><th style={th}>{t('net change')}</th>
            <th style={th}>{t('early')}</th><th style={th}>{t('late')}</th><th style={th}></th></tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.filter}>
              <td style={td}><b>{c.filter}</b></td>
              <td style={td}>{c.extra_trades >= 0 ? '+' : ''}{c.extra_trades}</td>
              <td style={td}>{signed(c.delta)}</td>
              <td style={td}>{signed(c.early.delta)} ({c.early.extra >= 0 ? '+' : ''}{c.early.extra})</td>
              <td style={td}>{signed(c.late.delta)} ({c.late.extra >= 0 ? '+' : ''}{c.late.extra})</td>
              <td style={td}>{VERDICT[c.verdict] ? t(VERDICT[c.verdict]) : undefined}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

const signedPct = (v, d = 1) => `${v >= 0 ? '+' : ''}${v.toFixed(d)}%`

// How the taken trades ended: exit mix, the pure losers (stopped before TP1), the winners' dips and
// the tighter-stop what-if. Everything here is measured from the history, nothing is applied.
function ExitBlock({ ex }) {
  const { t } = useI18n()
  if (!ex) return null
  if (!ex.available) return <div style={{ marginTop: 6, ...faint }}>{t('🚪 exits: not available (re-run with the current version)')}</div>
  const nv = ex.never_tp1
  const h = ex.winners_heat
  return (
    <div style={{ marginTop: 6 }}>
      <div>{t('🚪 How the trades ended')} <span style={faint}>{t('(net of fee)')}</span></div>
      <div style={faint}>
        {Object.entries(ex.mix).filter(([, v]) => v.n > 0).map(([k, v]) => `${k} ×${v.n} (${signedPct(v.net)})`).join(' · ')}
      </div>
      {nv.n > 0 && (
        <div style={{ marginTop: 4 }}>
          {t('❌ Stopped before TP1:')} <b>{nv.n}</b> {t('({share} of trades, net {net}, avg stop {stop}%)', { share: share(nv.share_of_trades), net: signedPct(nv.net), stop: nv.avg_sl_pct.toFixed(2) })}
          <div style={faint}>
            {t('median {median} bars; stopped within {bars} bars: {share}', { median: nv.median_bars, bars: nv.fast_stop.bars, share: share(nv.fast_stop.share) })}<br />
            {t('how far they got toward TP1: {list} — at least half way: {share} (early {early} | late {late})', {
              list: Object.entries(nv.progress_to_tp1).map(([k, v]) => `${k} ×${v}`).join(', '),
              share: share(nv.near_miss.share), early: share(nv.near_miss.by_window.early), late: share(nv.near_miss.by_window.late),
            })}<br />
            {t('price reached TP1 after the stop: {share} (early {early} | late {late}), TP2: {share2} — an upper bound for "the stop was too tight", the path after the stop is not replayed', {
              share: share(nv.shakeout_tp1.share), early: share(nv.shakeout_tp1.by_window.early), late: share(nv.shakeout_tp1.by_window.late),
              share2: share(nv.shakeout_tp2.share),
            })}
          </div>
        </div>
      )}
      {h.n > 0 && (
        <div style={{ marginTop: 4 }}>
          {t('🔥 Of the {n} trades that reached TP1, {a} dipped ≥50% of the way to the stop and {b} ≥75%', { n: h.n, a: share(h.dip_ge_50), b: share(h.dip_ge_75) })}
          <span style={faint}> {t('(median {m})', { m: share(h.median_dip) })}</span>
        </div>
      )}
      {ex.tighter_exact && (
        <div style={{ marginTop: 6 }}>
          {t('✂️ Stop distance changed (tighter and wider), re-played bar by bar on the real highs/lows')}
          {ex.tighter_exact.check.mismatch > 0 && (
            <b> {t('⚠️ the replay does not reproduce {m} of {n} recorded trades — do not trust these numbers', { m: ex.tighter_exact.check.mismatch, n: ex.tighter_exact.check.n })}</b>
          )}
          <table style={{ borderCollapse: 'collapse', marginTop: 4 }}>
            <thead>
              <tr><th style={th}>{t('stop at')}</th><th style={th}>{t("PnL change (bot's same-bar rule)")}</th><th style={th}>{t('early / late')}</th>
                <th style={th}>{t('strict (same-bar = stop)')}</th><th style={th}>{t('early / late')}</th>
                <th style={th}>{t('trades better / worse')}</th><th style={th}>{t('net after')}</th></tr>
            </thead>
            <tbody>
              {ex.tighter_exact.factors.map((fx) => (
                <tr key={fx.factor}>
                  <td style={td}>{t('{factor}× distance', { factor: fx.factor })}</td>
                  <td style={td}>{signedPct(fx.bot_rule.all)}</td>
                  <td style={td}>{signedPct(fx.bot_rule.early)} / {signedPct(fx.bot_rule.late)}</td>
                  <td style={td}>{signedPct(fx.strict.all)}</td>
                  <td style={td}>{signedPct(fx.strict.early)} / {signedPct(fx.strict.late)}</td>
                  <td style={td}>{fx.saved} / {fx.cut_winners} {t('(of {n}, {amb} same-bar)', { n: fx.n, amb: fx.ambiguous })}</td>
                  <td style={td}>{signedPct(fx.net_after)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={faint}>{t('same trades assumed — an earlier exit could free the slot for a signal that was skipped (not modelled)')}</div>
        </div>
      )}
      {!ex.tighter_exact && <table style={{ borderCollapse: 'collapse', marginTop: 4 }}>
        <thead>
          <tr><th style={th}>{t('stop at')}</th><th style={th}>{t('never reached TP1 (exact)')}</th><th style={th}>{t('early / late')}</th>
            <th style={th}>{t('deep dips after TP1')}</th><th style={th}>{t('total between')}</th><th style={th}>{t('worst case early / late')}</th></tr>
        </thead>
        <tbody>
          {ex.tighter_sl.map((fx) => (
            <tr key={fx.factor}>
              <td style={td}>{t('{factor}× distance', { factor: fx.factor })}</td>
              <td style={td}>{signedPct(fx.exact.all)} {t('({n} stopped earlier)', { n: fx.stopped_never_tp1 })}</td>
              <td style={td}>{signedPct(fx.exact.early)} / {signedPct(fx.exact.late)}</td>
              <td style={td}>{fx.deep_dips_after_tp1} {t('({pct} if all before TP1)', { pct: signedPct(fx.worst_extra.all) })}</td>
              <td style={td}>{signedPct(fx.worst_case.all)} … {signedPct(fx.exact.all)}</td>
              <td style={td}>{signedPct(fx.worst_case.early)} / {signedPct(fx.worst_case.late)}</td>
            </tr>
          ))}
        </tbody>
      </table>}
    </div>
  )
}

function ResultRows({ result }) {
  const { t } = useI18n()
  const rows = []
  Object.entries(result.results || {}).forEach(([ticker, perTf]) => {
    Object.entries(perTf).forEach(([tf, r]) => rows.push({ ticker, tf, r }))
  })
  if (rows.length === 0) return <p style={{ fontSize: 12, ...faint }}>{t('No pair/timeframe produced a result.')}</p>
  return (
    <div style={{ marginBottom: 10 }}>
      {rows.map(({ ticker, tf, r }) => {
        const { trades, legs, examples } = r
        const reasons = Object.entries(legs.reasons || {}).slice(0, 5)
        const worst = examples.worst_trades[0]
        const miss = examples.biggest_missed[0]
        return (
          <div key={`${ticker}-${tf}`} style={{ fontSize: 12, marginBottom: 16 }}>
            <div style={{ fontWeight: 600 }}>{ticker} {tf}</div>
            <div style={faint}>
              {t('{n} signals taken ({won} won / {lost} lost, net {net})', { n: trades.n, won: trades.winners, lost: trades.losers, net: pct(trades.total_net) })} ·{' '}
              {t('{total} swings ≥ {mult}×ATR: {caught} caught, {missed} missed', { total: legs.total, mult: legs.pivot_atr_mult, caught: legs.caught, missed: legs.missed })}
              {reasons.length > 0 && <><br />{t('missed because: {list}', { list: reasons.map(([k, v]) => `${k} ×${v}`).join('; ') })}</>}
            </div>
            <FilterTable rows={r.filter_cf} />
            <ExitBlock ex={r.exits} />
            <LoserTable lf={r.loser_features} />
            <MissedTable mf={r.missed_features} />
            {r.skipped_features?.length > 0 && (
              <div style={{ marginTop: 4, ...faint }}>
                {t('not tested (an enabled filter already decides them): {list}', { list: r.skipped_features.join(', ') })}
              </div>
            )}
            {(worst || miss) && (
              <div style={{ marginTop: 6, ...faint }}>
                {worst && <div>{t('worst signal: {ts} {kind} {net} — {snap}', { ts: worst.ts, kind: `${worst.track.toUpperCase()}-${worst.side}`, net: pct(worst.net), snap: snapText(worst.snap) })}</div>}
                {miss && <div>{t('biggest miss: {ts} {side} {pct}% over {bars} bars ({reason}) — {snap}', { ts: miss.ts, side: miss.side, pct: miss.leg_pct.toFixed(1), bars: miss.bars, reason: reasonText(miss, t), snap: snapText(miss.snap) })}</div>}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

export default function AutopsyPanel({ config }) {
  const { t } = useI18n()
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(3000)
  const [swing, setSwing] = useState(3)
  const sim = useSimJob('autopsy')
  const result = sim.job?.status === 'done' ? sim.job.result : null
  const ranBars = sim.job?.params?.num_bars ?? numBars

  const start = () => {
    const bad = barsError(numBars)
    if (bad) { sim.setError(bad); return }
    if (!(swing >= 1 && swing <= 10)) { sim.setError(t('Swing size must be between 1 and 10 ATRs')); return }
    sim.start(() => api.runAutopsy({ ticker, tf, numBars, swing }))
  }

  return (
    <div className="panel">
      <h3 className="panel-title">{t('Signal autopsy')}</h3>
      <p style={{ fontSize: 12, ...faint, marginBottom: 12 }}>
        {t("Replays the history with each pair's current filters and shows what FRAMA / MFI / Andean / UT Bot / Hurst / CHOP / regime / HTF showed (1) on the signals that lost, and (2) at reversals that no signal caught — swings of at least the chosen number of ATRs — and why they were missed (no trigger, a filter, or the track was busy). A state marked ✅ lost money (or was over-represented among missed turns) in BOTH the older 60% and the newest 40% of the history. A ✅ also needs enough trades and a clearly above-average rate (z-score), so a handful of trades never earns one. It also replays the same triggers with each enabled filter switched off on its own, to show what that filter costs or saves, and looks at the exit side: how the trades ended, how far the stopped-out ones got toward TP1, and what a tighter stop would have done. Measurement only: nothing is applied.")}
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
            <button onClick={start} disabled={sim.running}>{sim.running ? t('Running…') : t('Run autopsy')}</button>
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
          <ResultRows result={result} />
          <p style={{ fontSize: 11, ...faint }}>
            {t('Dozens of states are tested per pair, so some will look good by chance — treat ✅ as a candidate to forward-test, not a finding. Also in Discord: !autopsy.')}
          </p>
        </>
      )}
    </div>
  )
}
