import React, { useEffect, useState } from 'react'
import { api } from '../api'

export default function SpreadPanel({ lastEvent, pairs }) {
  const [ticker, setTicker] = useState(null)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!ticker && pairs?.length) setTicker(pairs[0])
  }, [pairs, ticker])

  useEffect(() => {
    if (!ticker) return
    const load = () => api.getSpread(ticker).then(setData).catch((e) => setError(e.message))
    load()
    // Spread moves faster than funding/OI/on-chain data, so it's polled on
    // a shorter interval than the rest of the dashboard.
    const id = setInterval(load, 20000)
    return () => clearInterval(id)
  }, [ticker])

  // Refresh right away on a scan tick instead of waiting up to 20s.
  useEffect(() => {
    if (lastEvent?.type === 'scan_tick' && ticker) {
      api.getSpread(ticker).then(setData).catch(() => {})
    }
  }, [lastEvent, ticker])

  const filterOn = data?.filter_enabled
  const snap = data?.snapshot

  return (
    <div>
      <div className="panel">
        <h3 className="panel-title">Order book spread</h3>

        {pairs?.length > 1 && (
          <div className="seg" style={{ marginBottom: 12 }}>
            {pairs.map((p) => (
              <button key={p} className={ticker === p ? 'active' : ''} onClick={() => setTicker(p)}>
                {p}
              </button>
            ))}
          </div>
        )}

        {error && <p style={{ color: 'var(--short)', fontSize: 13 }}>Failed to load: {error}</p>}

        {!error && !data && <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>Loading…</p>}

        {!error && data && (
          <>
            <div className="row">
              <span className="row-label">Filter</span>
              <span className={`tag ${filterOn ? 'long' : 'flat'}`}>
                {filterOn ? '🟢 ON' : '⚪ OFF (warm-up/logging only)'}
              </span>
            </div>
            {snap && (
              <>
                <div className="row">
                  <span className="row-label">Bid / Ask</span>
                  <span className="row-value">{snap.bid} / {snap.ask}</span>
                </div>
                <div className="row">
                  <span className="row-label">Current spread</span>
                  <span className="row-value">{(snap.spread_pct * 100).toFixed(3)}%</span>
                </div>
                <div className="row">
                  <span className="row-label">Rolling median</span>
                  <span className="row-value">
                    {snap.rolling_median != null ? `${(snap.rolling_median * 100).toFixed(3)}%` : 'n/a'}
                  </span>
                </div>
                <div className="row">
                  <span className="row-label">Warm-up</span>
                  <span className={`tag ${data.warmed_up ? 'long' : 'flat'}`}>
                    {snap.sample_count}/{data.min_samples_for_anomaly} samples
                    {data.warmed_up ? ' — warmed up' : ''}
                  </span>
                </div>
              </>
            )}
          </>
        )}
      </div>

      <div className="panel">
        <h3 className="panel-title">How this filter works</h3>
        <p style={{ fontSize: 13, color: 'var(--text)', marginBottom: 10 }}>
          Two relative conditions, either one blocks a signal once the toggle in Settings is on:
        </p>
        <ul style={{ fontSize: 13, color: 'var(--text)', margin: 0, paddingLeft: 20, lineHeight: 1.6 }}>
          <li>Spread eats more than 15% of <em>this signal's own SL distance</em> — no warm-up needed, works from the first reading.</li>
          <li>Spread reads more than 3x wider than <em>this pair's own recent rolling median</em> — needs 30 samples before it activates; until then it's simply skipped, not treated as a block.</li>
        </ul>
        <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 12 }}>
          Not backtestable — order book depth isn't part of OHLCV history, so this only ever gates the
          live scan path, never the backtest. A flat percent-of-price cutoff was deliberately avoided —
          a major like BTC and a thin alt don't share a normal spread range, so both conditions above are
          relative instead of absolute. Collection runs every scan tick for every tracked pair regardless
          of the toggle, so flipping it on later starts from an already-warmed-up baseline. Toggle it on
          from Settings → Signal Filters → "Order book spread".
        </p>
      </div>
    </div>
  )
}
