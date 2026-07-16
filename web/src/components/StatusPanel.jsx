import React, { useEffect, useState } from 'react'
import { api } from '../api'

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
  const [tp1Banner, setTp1Banner] = useState(null)

  useEffect(() => {
    const load = () => api.getStatus().then(setStatus).catch((e) => setError(e.message))
    load()
    const id = setInterval(load, 30000)
    return () => clearInterval(id)
  }, [])

  // scanner tick or new signal — refresh right away instead of waiting for the interval
  useEffect(() => {
    if (!lastEvent) return
    if (lastEvent.type === 'scan_tick' || lastEvent.type === 'signal' || lastEvent.type === 'tp1_hit') {
      api.getStatus().then(setStatus).catch(() => {})
    }
    if (lastEvent.type === 'tp1_hit') setTp1Banner(lastEvent)
  }, [lastEvent])

  if (error) return <div className="error-banner">{error}</div>
  if (!status) return <div className="empty-state">Loading…</div>

  const pairs = Object.entries(status.pairs || {})

  return (
    <div>
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
            Close 50% of the position and move SL to breakeven ({fmtPrice(tp1Banner.entry)}) — same as the Discord alert.
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

      {lastEvent?.type === 'signal' && (
        <div className="panel">
          <h3 className="panel-title">Latest signal</h3>
          <div className="row">
            <span className="row-label">{lastEvent.ticker} · {lastEvent.tf}</span>
            <span className={`tag ${lastEvent.sig_type?.includes('SHORT') ? 'short' : 'long'}`}>
              {lastEvent.sig_type}
            </span>
          </div>
          <div className="row">
            <span className="row-label">Entry / TP / SL</span>
            <span className="row-value">
              {lastEvent.price} / {lastEvent.tp} / {lastEvent.sl}
            </span>
          </div>
        </div>
      )}
    </div>
  )
}
