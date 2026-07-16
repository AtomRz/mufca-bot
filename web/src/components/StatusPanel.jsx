import React, { useEffect, useState } from 'react'
import { api } from '../api'

function TradeTag({ trade }) {
  if (!trade) return <span className="tag flat">flat</span>
  const side = trade.side || 'long'
  return <span className={`tag ${side}`}>{side}</span>
}

export default function StatusPanel({ lastEvent }) {
  const [status, setStatus] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    const load = () => api.getStatus().then(setStatus).catch((e) => setError(e.message))
    load()
    const id = setInterval(load, 30000)
    return () => clearInterval(id)
  }, [])

  // scanner tick or new signal — refresh right away instead of waiting for the interval
  useEffect(() => {
    if (!lastEvent) return
    if (lastEvent.type === 'scan_tick' || lastEvent.type === 'signal') {
      api.getStatus().then(setStatus).catch(() => {})
    }
  }, [lastEvent])

  if (error) return <div className="error-banner">{error}</div>
  if (!status) return <div className="empty-state">Loading…</div>

  const pairs = Object.entries(status.pairs || {})

  return (
    <div>
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
            <div className="row" key={tf}>
              <span className="row-label mono">{tf}</span>
              <span style={{ display: 'flex', gap: 8 }}>
                <span title="Andean track">
                  A <TradeTag trade={data.a_active_trade} />
                </span>
                <span title="UT Bot track">
                  U <TradeTag trade={data.u_active_trade} />
                </span>
              </span>
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
