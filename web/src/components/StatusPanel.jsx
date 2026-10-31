import React, { useEffect, useState, useRef } from 'react'
import { api } from '../api'

// 🆕 FIX: App keeps `lastEvent` alive across tab switches, but this panel is
// unmounted on every switch — so on coming back its effect ran again on the
// SAME old event: a TP1 banner that had been dismissed (or was hours old) popped
// up again, and the "Latest signal" card disappeared as soon as the next
// scan_tick replaced lastEvent. What the panel has already handled, and what it
// should keep showing, lives at module level so it survives the remount.
const persisted = { seenEvent: null, tp1Banner: null, latestSignal: null }

function fmtPrice(v) {
  if (v === undefined || v === null) return '—'
  return typeof v === 'number' ? v.toLocaleString(undefined, { maximumFractionDigits: 6 }) : v
}

// Full trade card: side, entry, TP1/TP2, SL, and a TP1-hit indicator —
// not just a flat/long/short tag, so you can see targets without opening the chart.
function TradeCard({ trade, label }) {
  if (!trade) {
    return (
      <div className="row">
        <span className="row-label">{label}</span>
        <span className="tag flat">flat</span>
      </div>
    )
  }
  return (
    <div style={{ padding: '6px 0', borderBottom: '1px solid var(--border-soft)' }}>
      <div className="row" style={{ borderBottom: 'none', padding: '2px 0' }}>
        <span className="row-label">{label}</span>
        <span className={`tag ${trade.side}`}>{trade.side}</span>
      </div>
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', fontSize: 12, marginTop: 2 }}>
        <span><span style={{ color: 'var(--text-faint)' }}>Entry </span><span className="mono">{fmtPrice(trade.entry)}</span></span>
        {trade.tp1 && trade.tp1 !== trade.tp && (
          <span>
            <span style={{ color: 'var(--text-faint)' }}>TP1 </span>
            <span className="mono" style={{ color: trade.tp1_hit ? 'var(--long)' : 'var(--text)' }}>
              {fmtPrice(trade.tp1)}{trade.tp1_hit ? ' ✓' : ''}
            </span>
          </span>
        )}
        <span><span style={{ color: 'var(--text-faint)' }}>TP2 </span><span className="mono" style={{ color: 'var(--long)' }}>{fmtPrice(trade.tp)}</span></span>
        <span>
          <span style={{ color: 'var(--text-faint)' }}>SL </span>
          <span className="mono" style={{ color: 'var(--short)' }}>{fmtPrice(trade.sl)}</span>
          {trade.tp1_hit && <span style={{ color: 'var(--text-faint)' }}> (BE)</span>}
        </span>
      </div>
    </div>
  )
}

export default function StatusPanel({ lastEvent }) {
  const [status, setStatus] = useState(null)
  const [error, setError] = useState(null)
  const [tp1Banner, setTp1BannerState] = useState(persisted.tp1Banner)
  const [latestSignal, setLatestSignal] = useState(persisted.latestSignal)
  const statusSeq = useRef(0)

  const setTp1Banner = (v) => { persisted.tp1Banner = v; setTp1BannerState(v) }

  // 🆕 FIX: only the newest request may set the state (a slow reply from the
  // interval poll could land after a fresher one triggered by an event), and a
  // successful load clears an earlier error — it used to be sticky: one failed
  // poll (a phone switching networks) replaced the whole panel with an error
  // banner until the page was reloaded, even though later polls succeeded.
  const loadStatus = () => {
    const seq = ++statusSeq.current
    return api.getStatus()
      .then((d) => { if (seq === statusSeq.current) { setStatus(d); setError(null) } })
      .catch((e) => { if (seq === statusSeq.current) setError(e.message) })
  }

  useEffect(() => {
    loadStatus()
    const id = setInterval(() => { if (!document.hidden) loadStatus() }, 30000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // scanner tick or new signal — refresh right away instead of waiting for the interval
  useEffect(() => {
    if (!lastEvent || lastEvent === persisted.seenEvent) return
    persisted.seenEvent = lastEvent
    if (['scan_tick', 'signal', 'tp1_hit'].includes(lastEvent.type)) {
      if (!document.hidden) loadStatus()
    }
    if (lastEvent.type === 'tp1_hit') setTp1Banner(lastEvent)
    if (lastEvent.type === 'signal') { persisted.latestSignal = lastEvent; setLatestSignal(lastEvent) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastEvent])

  if (!status) return error ? <div className="error-banner">{error}</div> : <div className="empty-state">Loading…</div>

  const pairs = Object.entries(status.pairs || {})
  const shocks = status.market_shocks || []
  const anomaly = status.onchain_anomaly

  return (
    <div>
      {error && <div className="error-banner">{error}</div>}
      {(shocks.length > 0 || (anomaly && anomaly.active)) && (
        <div className="panel" style={{ borderColor: 'var(--short)' }}>
          <h3 className="panel-title" style={{ color: 'var(--short)' }}>⚠️ Abnormal market conditions</h3>
          {shocks.map((s) => (
            <div className="row" key={`${s.ticker}-${s.timeframe}`}>
              <span className="row-label">{s.ticker} · {s.timeframe} market shock</span>
              <span className="row-value">
                {s.move_pct > 0 ? '+' : ''}{s.move_pct}% in {s.bars} bars ({s.move_atr}× ATR)
              </span>
            </div>
          ))}
          {anomaly && anomaly.active && (anomaly.events || []).map((ev) => (
            <div className="row" key={`${ev.exchange}-${ev.detected_ts}`}>
              <span className="row-label">{ev.exchange} wallet {ev.kind}</span>
              <span className="row-value">
                {Math.round(ev.prev_eth).toLocaleString()} → {Math.round(ev.curr_eth).toLocaleString()} ETH ({ev.change_pct}%)
              </span>
            </div>
          ))}
          <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 8 }}>
            {status.shock_blocks_entries && shocks.length > 0
              ? 'New entries are blocked while the shock lasts. '
              : 'The A/U tracks are not built for news-driven cascades, so FLAT here can simply mean "no valid trigger". '}
            {anomaly && anomaly.active ? 'On-chain flow is held neutral and leverage is reduced.' : ''}
          </p>
        </div>
      )}
      {tp1Banner && (
        <div className="panel" style={{ borderColor: 'var(--accent)' }}>
          <h3 className="panel-title" style={{ color: 'var(--accent)' }}>🎯 TP1 Hit</h3>
          <div className="row">
            <span className="row-label">
              {tp1Banner.ticker} · {tp1Banner.tf} · {tp1Banner.track === 'a' ? 'Andean' : 'UT Bot'}
            </span>
            <span className={`tag ${tp1Banner.side}`}>{tp1Banner.side}</span>
          </div>
          <div className="row">
            <span className="row-label">Entry → TP1</span>
            <span className="row-value">{fmtPrice(tp1Banner.entry)} → {fmtPrice(tp1Banner.tp1)}</span>
          </div>
          <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 8 }}>
            Close 50% of the position and move SL to {(tp1Banner.sl_mode || 'breakeven').replace(/_/g, ' ')} ({fmtPrice(tp1Banner.new_sl ?? tp1Banner.entry)}) — same as the Discord alert.
          </p>
          <button className="btn" style={{ marginTop: 8 }} onClick={() => setTp1Banner(null)}>Dismiss</button>
        </div>
      )}

      <div className="panel">
        <h3 className="panel-title">Scanner</h3>
        <div className="row">
          <span className="row-label">Total scans</span>
          <span className="row-value">{status.scan_stats?.total_scans ?? '—'}</span>
        </div>
        <div className="row">
          <span className="row-label">Signals generated</span>
          <span className="row-value">{status.scan_stats?.signals_generated ?? '—'}</span>
        </div>
        <div className="row">
          <span className="row-label">Mode</span>
          <span className="row-value">{status.market_mode}</span>
        </div>
        <div className="row">
          <span className="row-label">HTF Bias</span>
          <span className="row-value">{status.htf_bias}</span>
        </div>
      </div>

      {pairs.map(([ticker, tfs]) => (
        <div className="panel" key={ticker}>
          <h3 className="panel-title">{ticker}</h3>
          {Object.entries(tfs).map(([tf, data]) => (
            <div key={tf} style={{ marginBottom: 8 }}>
              <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-faint)', marginBottom: 4 }}>{tf}</div>
              <TradeCard trade={data.a_active_trade} label="Andean (A)" />
              <TradeCard trade={data.u_active_trade} label="UT Bot (U)" />
            </div>
          ))}
        </div>
      ))}

      {latestSignal && (
        <div className="panel">
          <h3 className="panel-title">Latest signal</h3>
          <div className="row">
            <span className="row-label">{latestSignal.ticker} · {latestSignal.tf}</span>
            <span className={`tag ${latestSignal.sig_type?.includes('SHORT') ? 'short' : 'long'}`}>
              {latestSignal.sig_type}
            </span>
          </div>
          <div className="row">
            <span className="row-label">Entry / TP / SL</span>
            <span className="row-value">
              {latestSignal.price} / {latestSignal.tp} / {latestSignal.sl}
            </span>
          </div>
        </div>
      )}
    </div>
  )
}
