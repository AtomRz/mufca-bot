import React, { useState } from 'react'
import { api } from '../api'
import { SimScopeFields, SimStatus, BarCoverageWarning, useSimJob, barsError } from './InsightsPanel'

const pct = (v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
const share = (v) => `${Math.round(v * 100)}%`
const faint = { color: 'var(--text-faint)' }
const th = { textAlign: 'left', fontWeight: 500, padding: '2px 8px 2px 0', color: 'var(--text-faint)' }
const td = { padding: '2px 8px 2px 0' }

// "frama_dir against, mfi_zone chasing, ..." - what the indicators showed on one bar.
const snapText = (snap) =>
  Object.entries(snap || {}).filter(([, v]) => v !== 'na').map(([k, v]) => `${k} ${v}`).join(' · ')

const reasonText = (m) =>
  m.status === 'none' ? 'no trigger' : m.status === 'busy' ? 'track busy' : `filter ${m.blockers.join('+')}`

function LoserTable({ lf }) {
  const rows = lf.candidates.filter((c) => c.lift > 1 && c.skip_gain > 0).slice(0, 6)
  return (
    <div style={{ marginTop: 6 }}>
      <div>📉 States that lost more <span style={faint}>(average signal loses {share(lf.base_loss_rate)}; {lf.n_tested} states tested)</span></div>
      {rows.length === 0 ? <div style={faint}>nothing stands out</div> : (
        <table style={{ borderCollapse: 'collapse', marginTop: 2 }}>
          <thead>
            <tr><th style={th}></th><th style={th}>state</th><th style={th}>n</th><th style={th}>loses</th>
              <th style={th}>skipping it</th><th style={th}>early / late</th></tr>
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
  const rows = mf.candidates.filter((c) => c.lift > 1).slice(0, 6)
  return (
    <div style={{ marginTop: 6 }}>
      <div>🔄 States over-represented at missed reversals <span style={faint}>(on average {share(mf.base_missed_share)} are missed)</span></div>
      {rows.length === 0 ? <div style={faint}>nothing stands out</div> : (
        <table style={{ borderCollapse: 'collapse', marginTop: 2 }}>
          <thead>
            <tr><th style={th}></th><th style={th}>state</th><th style={th}>missed / all</th><th style={th}>lift</th></tr>
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
  if (!rows || rows.length === 0) return null
  const signed = (v, d = 1) => `${v >= 0 ? '+' : ''}${v.toFixed(d)}%`
  return (
    <div style={{ marginTop: 6 }}>
      <div>🎚️ What if one filter were off <span style={faint}>(same triggers, one position at a time, net of fee)</span></div>
      <table style={{ borderCollapse: 'collapse', marginTop: 2 }}>
        <thead>
          <tr><th style={th}>filter</th><th style={th}>trades</th><th style={th}>net change</th>
            <th style={th}>early</th><th style={th}>late</th><th style={th}></th></tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.filter}>
              <td style={td}><b>{c.filter}</b></td>
              <td style={td}>{c.extra_trades >= 0 ? '+' : ''}{c.extra_trades}</td>
              <td style={td}>{signed(c.delta)}</td>
              <td style={td}>{signed(c.early.delta)} ({c.early.extra >= 0 ? '+' : ''}{c.early.extra})</td>
              <td style={td}>{signed(c.late.delta)} ({c.late.extra >= 0 ? '+' : ''}{c.late.extra})</td>
              <td style={td}>{VERDICT[c.verdict]}</td>
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
  if (!ex) return null
  if (!ex.available) return <div style={{ marginTop: 6, ...faint }}>🚪 exits: not available (re-run with the current version)</div>
  const nv = ex.never_tp1
  const h = ex.winners_heat
  return (
    <div style={{ marginTop: 6 }}>
      <div>🚪 How the trades ended <span style={faint}>(net of fee)</span></div>
      <div style={faint}>
        {Object.entries(ex.mix).filter(([, v]) => v.n > 0).map(([k, v]) => `${k} ×${v.n} (${signedPct(v.net)})`).join(' · ')}
      </div>
      {nv.n > 0 && (
        <div style={{ marginTop: 4 }}>
          ❌ Stopped before TP1: <b>{nv.n}</b> ({share(nv.share_of_trades)} of trades, net {signedPct(nv.net)}, avg stop {nv.avg_sl_pct.toFixed(2)}%)
          <div style={faint}>
            median {nv.median_bars} bars; stopped within {nv.fast_stop.bars} bars: {share(nv.fast_stop.share)}<br />
            how far they got toward TP1: {Object.entries(nv.progress_to_tp1).map(([k, v]) => `${k} ×${v}`).join(', ')}
            {' '}— at least half way: {share(nv.near_miss.share)} (early {share(nv.near_miss.by_window.early)} | late {share(nv.near_miss.by_window.late)})<br />
            price reached TP1 after the stop: {share(nv.shakeout_tp1.share)} (early {share(nv.shakeout_tp1.by_window.early)} | late {share(nv.shakeout_tp1.by_window.late)}),
            TP2: {share(nv.shakeout_tp2.share)} — an upper bound for "the stop was too tight", the path after the stop is not replayed
          </div>
        </div>
      )}
      {h.n > 0 && (
        <div style={{ marginTop: 4 }}>
          🔥 Of the {h.n} trades that reached TP1, {share(h.dip_ge_50)} dipped ≥50% of the way to the stop and {share(h.dip_ge_75)} ≥75%
          <span style={faint}> (median {share(h.median_dip)})</span>
        </div>
      )}
      {ex.tighter_exact && (
        <div style={{ marginTop: 6 }}>
          ✂️ Tighter stop, re-played bar by bar on the real highs/lows
          {ex.tighter_exact.check.mismatch > 0 && (
            <b> ⚠️ the replay does not reproduce {ex.tighter_exact.check.mismatch} of {ex.tighter_exact.check.n} recorded trades — do not trust these numbers</b>
          )}
          <table style={{ borderCollapse: 'collapse', marginTop: 4 }}>
            <thead>
              <tr><th style={th}>stop at</th><th style={th}>PnL change (bot's same-bar rule)</th><th style={th}>early / late</th>
                <th style={th}>strict (same-bar = stop)</th><th style={th}>early / late</th>
                <th style={th}>trades saved / winners cut</th><th style={th}>net after</th></tr>
            </thead>
            <tbody>
              {ex.tighter_exact.factors.map((t) => (
                <tr key={t.factor}>
                  <td style={td}>{t.factor}× distance</td>
                  <td style={td}>{signedPct(t.bot_rule.all)}</td>
                  <td style={td}>{signedPct(t.bot_rule.early)} / {signedPct(t.bot_rule.late)}</td>
                  <td style={td}>{signedPct(t.strict.all)}</td>
                  <td style={td}>{signedPct(t.strict.early)} / {signedPct(t.strict.late)}</td>
                  <td style={td}>{t.saved} / {t.cut_winners} (of {t.n}, {t.ambiguous} same-bar)</td>
                  <td style={td}>{signedPct(t.net_after)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={faint}>same trades assumed — an earlier exit could free the slot for a signal that was skipped (not modelled)</div>
        </div>
      )}
      {!ex.tighter_exact && <table style={{ borderCollapse: 'collapse', marginTop: 4 }}>
        <thead>
          <tr><th style={th}>stop at</th><th style={th}>never reached TP1 (exact)</th><th style={th}>early / late</th>
            <th style={th}>deep dips after TP1</th><th style={th}>total between</th><th style={th}>worst case early / late</th></tr>
        </thead>
        <tbody>
          {ex.tighter_sl.map((t) => (
            <tr key={t.factor}>
              <td style={td}>{t.factor}× distance</td>
              <td style={td}>{signedPct(t.exact.all)} ({t.stopped_never_tp1} stopped earlier)</td>
              <td style={td}>{signedPct(t.exact.early)} / {signedPct(t.exact.late)}</td>
              <td style={td}>{t.deep_dips_after_tp1} ({signedPct(t.worst_extra.all)} if all before TP1)</td>
              <td style={td}>{signedPct(t.worst_case.all)} … {signedPct(t.exact.all)}</td>
              <td style={td}>{signedPct(t.worst_case.early)} / {signedPct(t.worst_case.late)}</td>
            </tr>
          ))}
        </tbody>
      </table>}
    </div>
  )
}

function ResultRows({ result }) {
  const rows = []
  Object.entries(result.results || {}).forEach(([ticker, perTf]) => {
    Object.entries(perTf).forEach(([tf, r]) => rows.push({ ticker, tf, r }))
  })
  if (rows.length === 0) return <p style={{ fontSize: 12, ...faint }}>No pair/timeframe produced a result.</p>
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
              {trades.n} signals taken ({trades.winners} won / {trades.losers} lost, net {pct(trades.total_net)}) ·{' '}
              {legs.total} swings ≥ {legs.pivot_atr_mult}×ATR: {legs.caught} caught, {legs.missed} missed
              {reasons.length > 0 && <><br />missed because: {reasons.map(([k, v]) => `${k} ×${v}`).join('; ')}</>}
            </div>
            <FilterTable rows={r.filter_cf} />
            <ExitBlock ex={r.exits} />
            <LoserTable lf={r.loser_features} />
            <MissedTable mf={r.missed_features} />
            {r.skipped_features?.length > 0 && (
              <div style={{ marginTop: 4, ...faint }}>
                not tested (an enabled filter already decides them): {r.skipped_features.join(', ')}
              </div>
            )}
            {(worst || miss) && (
              <div style={{ marginTop: 6, ...faint }}>
                {worst && <div>worst signal: {worst.ts} {worst.track.toUpperCase()}-{worst.side} {pct(worst.net)} — {snapText(worst.snap)}</div>}
                {miss && <div>biggest miss: {miss.ts} {miss.side} {miss.leg_pct.toFixed(1)}% over {miss.bars} bars ({reasonText(miss)}) — {snapText(miss.snap)}</div>}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

export default function AutopsyPanel({ config }) {
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
    if (!(swing >= 1 && swing <= 10)) { sim.setError('Swing size must be between 1 and 10 ATRs'); return }
    sim.start(() => api.runAutopsy({ ticker, tf, numBars, swing }))
  }

  return (
    <div className="panel">
      <h3 className="panel-title">Signal autopsy</h3>
      <p style={{ fontSize: 12, ...faint, marginBottom: 12 }}>
        Replays the history with each pair's current filters and shows what FRAMA / MFI / Andean / UT Bot / Hurst / CHOP /
        regime / HTF showed (1) on the signals that lost, and (2) at reversals that no signal caught — swings of at least
        the chosen number of ATRs — and why they were missed (no trigger, a filter, or the track was busy). A state marked
        ✅ lost money (or was over-represented among missed turns) in BOTH the older 60% and the newest 40% of the history.
        A ✅ also needs enough trades and a clearly above-average rate (z-score), so a handful of trades never earns one.
        It also replays the same triggers with each enabled filter switched off on its own, to show what that filter costs
        or saves, and looks at the exit side: how the trades ended, how far the stopped-out ones got toward TP1, and what a
        tighter stop would have done. Measurement only: nothing is applied.
      </p>
      <SimScopeFields
        pairs={config.pairs} timeframes={config.timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={
          <>
            <div>
              <label style={{ display: 'block', fontSize: 11, ...faint, marginBottom: 4 }}>Swing (× ATR)</label>
              <input type="number" value={swing} min={1} max={10} step={0.5}
                     onChange={(e) => setSwing(Number(e.target.value))} style={{ width: 70 }} />
            </div>
            <button onClick={start} disabled={sim.running}>{sim.running ? 'Running…' : 'Run autopsy'}</button>
          </>
        }
      />
      <SimStatus sim={sim} />
      {result && !sim.running && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          <p style={{ fontSize: 12, marginBottom: 8 }}>
            Net of {result.fee_pct}% fee per trade · early window = oldest {Math.round(result.train_fraction * 100)}% of the bars · nothing applied
          </p>
          {(result.skipped || []).map((sk) => (
            <p key={`${sk.ticker}-${sk.tf}`} style={{ fontSize: 12, color: 'var(--short)', marginBottom: 6 }}>
              ❌ {sk.ticker} {sk.tf}: skipped — {sk.reason}
            </p>
          ))}
          <ResultRows result={result} />
          <p style={{ fontSize: 11, ...faint }}>
            Dozens of states are tested per pair, so some will look good by chance — treat ✅ as a candidate to
            forward-test, not a finding. Also in Discord: !autopsy.
          </p>
        </>
      )}
    </div>
  )
}
