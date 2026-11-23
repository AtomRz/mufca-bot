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
                <td style={td}>{share(c.loss_rate)} (×{c.lift.toFixed(1)})</td>
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
                <td style={td}>×{c.lift.toFixed(1)}</td>
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
        It also replays the same triggers with each enabled filter switched off on its own, to show what that filter costs
        or saves. Measurement only: nothing is applied.
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
