import React, { useEffect, useState } from 'react'
import { api } from '../api'

function fmtAge(lastFetch) {
  if (!lastFetch) return '—'
  const mins = Math.floor((Date.now() / 1000 - lastFetch) / 60)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  return `${hrs}h ${mins % 60}m ago`
}

function BiasBar({ label, value }) {
  // value is -15..+15 — render as a centered bar, green right / red left
  const pct = Math.min(100, Math.abs(value) / 15 * 100)
  const positive = value >= 0
  return (
    <div style={{ marginBottom: 10 }}>
      <div className="row" style={{ borderBottom: 'none', padding: '0 0 4px' }}>
        <span className="row-label">{label}</span>
        <span className="row-value" style={{ color: positive ? 'var(--long)' : 'var(--short)' }}>
          {value > 0 ? `+${value}` : value}
        </span>
      </div>
      <div style={{ position: 'relative', height: 6, background: 'var(--bg-panel-raised)', borderRadius: 3, overflow: 'hidden' }}>
        <div
          style={{
            position: 'absolute',
            top: 0, bottom: 0,
            left: positive ? '50%' : `${50 - pct / 2}%`,
            width: `${pct / 2}%`,
            background: positive ? 'var(--long)' : 'var(--short)',
          }}
        />
        <div style={{ position: 'absolute', left: '50%', top: 0, bottom: 0, width: 1, background: 'var(--border-soft)' }} />
      </div>
    </div>
  )
}

function MultRow({ label, long, short }) {
  return (
    <div className="row">
      <span className="row-label">{label}</span>
      <span className="row-value">
        <span style={{ color: 'var(--long)' }}>L {long}×</span>
        {'  '}
        <span style={{ color: 'var(--short)' }}>S {short}×</span>
      </span>
    </div>
  )
}

export default function OnchainPanel({ lastEvent, pairs }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [derivTicker, setDerivTicker] = useState(null)
  const [deriv, setDeriv] = useState(null)
  const [derivError, setDerivError] = useState(null)

  useEffect(() => {
    const load = () => api.getOnchain().then(setData).catch((e) => setError(e.message))
    load()
    const id = setInterval(load, 60000)
    return () => clearInterval(id)
  }, [])

  // Default the derivatives pair selector to the first tracked pair once
  // the pair list arrives — independent of whatever's selected on the
  // Chart tab, since Onchain has its own dedicated selector for this
  // per-ticker section (unlike the rest of this tab, which is global).
  useEffect(() => {
    if (!derivTicker && pairs?.length) setDerivTicker(pairs[0])
  }, [pairs, derivTicker])

  useEffect(() => {
    if (!derivTicker) return
    const load = () => api.getDerivatives(derivTicker).then(setDeriv).catch((e) => setDerivError(e.message))
    load()
    const id = setInterval(load, 60000)
    return () => clearInterval(id)
  }, [derivTicker])

  // A fresh hourly refresh on the backend broadcasts an onchain-tagged event —
  // reload right away instead of waiting up to a minute for the poll interval.
  useEffect(() => {
    if (lastEvent?.type === 'scan_tick') {
      api.getOnchain().then(setData).catch(() => {})
      if (derivTicker) api.getDerivatives(derivTicker).then(setDeriv).catch(() => {})
    }
  }, [lastEvent, derivTicker])

  if (error) {
    return <div className="panel"><p style={{ color: 'var(--short)' }}>Failed to load: {error}</p></div>
  }
  if (!data) {
    return <div className="panel"><p style={{ color: 'var(--text-faint)' }}>Loading…</p></div>
  }
  if (!data.enabled) {
    return (
      <div className="panel">
        <h3 className="panel-title">On-chain</h3>
        <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>
          Disabled — set <code>ETHERSCAN_API_KEY</code> and <code>COINGECKO_API_KEY</code> in
          <code>.env</code> to enable on-chain bias (ETH exchange flow, Fear &amp; Greed, BTC
          dominance) and its effect on confidence/TP/SL/leverage.
        </p>
      </div>
    )
  }

  const bias = data.bias
  if (!bias) {
    return (
      <div className="panel">
        <h3 className="panel-title">On-chain</h3>
        <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>
          Enabled, first refresh not completed yet — checks in hourly, this fills in on the next cycle.
        </p>
      </div>
    )
  }

  const flow = bias.flow_data || {}
  const cg = bias.cg_data || {}

  return (
    <div>
      <div className="panel">
        <h3 className="panel-title">On-chain bias</h3>
        <p style={{ fontSize: 13, color: 'var(--text)', marginBottom: 14 }}>{bias.summary}</p>
        <BiasBar label="Long bias" value={bias.bias_long ?? 0} />
        <BiasBar label="Short bias" value={bias.bias_short ?? 0} />
        <div className="row">
          <span className="row-label">Leverage adjustment</span>
          <span className="row-value">{bias.lev_delta > 0 ? `+${bias.lev_delta}` : bias.lev_delta}</span>
        </div>
        <div className="row">
          <span className="row-label">Last refreshed</span>
          <span className="row-value">{fmtAge(data.last_fetch)}</span>
        </div>
      </div>

      <div className="panel">
        <h3 className="panel-title">ETH exchange flow</h3>
        <div className="row">
          <span className="row-label">Direction</span>
          <span className={`tag ${flow.flow === 'outflow' ? 'long' : flow.flow === 'inflow' ? 'short' : 'flat'}`}>
            {flow.flow === 'outflow' ? '🟢 Outflow' : flow.flow === 'inflow' ? '🔴 Inflow' : '⚪ Neutral'}
          </span>
        </div>
        <div className="row">
          <span className="row-label">Delta (1h)</span>
          <span className="row-value">{flow.delta_eth != null ? `${flow.delta_eth > 0 ? '+' : ''}${Math.round(flow.delta_eth).toLocaleString()} ETH` : '—'}</span>
        </div>
        <div className="row">
          <span className="row-label">Strength</span>
          <span className="row-value">{flow.strength || '—'}</span>
        </div>
        {flow.per_exchange && Object.keys(flow.per_exchange).length > 0 && (
          <div style={{ marginTop: 8, paddingTop: 8, borderTop: '1px solid var(--border-soft)' }}>
            {Object.entries(flow.per_exchange).map(([name, delta]) => (
              <div className="row" key={name} style={{ fontSize: 12, padding: '4px 0' }}>
                <span className="row-label">{name}</span>
                <span className="row-value" style={{ color: delta > 0 ? 'var(--short)' : delta < 0 ? 'var(--long)' : undefined }}>
                  {delta > 0 ? '+' : ''}{delta.toLocaleString()} ETH
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="panel">
        <h3 className="panel-title">Market sentiment</h3>
        <div className="row">
          <span className="row-label">Fear &amp; Greed</span>
          <span className="row-value">{bias.fear_and_greed ?? cg.fear_and_greed ?? '—'} ({bias.fg_label || cg.fg_label || '—'})</span>
        </div>
        <div className="row">
          <span className="row-label">BTC dominance</span>
          <span className="row-value">{cg.btc_dominance != null ? `${cg.btc_dominance.toFixed(2)}%` : '—'}</span>
        </div>
      </div>

      <div className="panel">
        <h3 className="panel-title">Applied multipliers</h3>
        <MultRow label="Take Profit" long={bias.tp_mult_long} short={bias.tp_mult_short} />
        <MultRow label="Stop Loss" long={bias.sl_mult_long} short={bias.sl_mult_short} />
        <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
          Applied to newly opened positions only — see <code>apply_onchain_with_safety()</code>.
          SL widening is capped regardless of these multipliers.
        </p>
      </div>

      <DerivativesSection pairs={pairs} ticker={derivTicker} onTickerChange={setDerivTicker} deriv={deriv} error={derivError} />
    </div>
  )
}

function DerivativesSection({ pairs, ticker, onTickerChange, deriv, error }) {
  return (
    <div className="panel">
      <h3 className="panel-title">Derivatives (Futures)</h3>
      {pairs?.length > 1 && (
        <div className="seg" style={{ marginBottom: 12 }}>
          {pairs.map((p) => (
            <button key={p} className={ticker === p ? 'active' : ''} onClick={() => onTickerChange(p)}>
              {p}
            </button>
          ))}
        </div>
      )}

      {error && <p style={{ color: 'var(--short)', fontSize: 13 }}>Failed to load: {error}</p>}

      {!error && !deriv && <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>Loading…</p>}

      {!error && deriv && !deriv.enabled && (
        <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>
          {deriv.note === 'spot_mode'
            ? 'Only applies in Futures mode — currently in Spot mode.'
            : 'Disabled — enable it from the Settings panel to see funding rate and open interest bias here.'}
        </p>
      )}

      {!error && deriv?.enabled && !deriv.bias && (
        <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>
          First refresh not completed yet — this fills in on the next cycle.
        </p>
      )}

      {!error && deriv?.enabled && deriv.bias && (
        <>
          <div className="row">
            <span className="row-label">Funding rate</span>
            <span className="row-value">
              {deriv.bias.funding_rate != null ? `${(deriv.bias.funding_rate * 100).toFixed(4)}%` : '—'}
            </span>
          </div>
          <div className="row">
            <span className="row-label">Open interest</span>
            <span className={`tag ${deriv.bias.oi_direction === 'rising' ? 'long' : deriv.bias.oi_direction === 'falling' ? 'short' : 'flat'}`}>
              {deriv.bias.oi_direction === 'rising' ? '📈 Rising' : deriv.bias.oi_direction === 'falling' ? '📉 Falling' : '⚪ Flat'}
              {deriv.bias.oi_delta_pct != null ? ` (${deriv.bias.oi_delta_pct > 0 ? '+' : ''}${(deriv.bias.oi_delta_pct * 100).toFixed(1)}%)` : ''}
            </span>
          </div>
          <BiasBar label="Long bias" value={deriv.bias.bias_long ?? 0} />
          <BiasBar label="Short bias" value={deriv.bias.bias_short ?? 0} />
          <div className="row">
            <span className="row-label">Leverage adjustment</span>
            <span className="row-value">{deriv.bias.lev_delta > 0 ? `+${deriv.bias.lev_delta}` : deriv.bias.lev_delta}</span>
          </div>
          <p style={{ fontSize: 13, color: 'var(--text)', marginTop: 10 }}>{deriv.bias.summary}</p>
          <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
            Combined with the on-chain bias above before being applied to new positions — see <code>derivatives.combine_biases()</code>.
          </p>
        </>
      )}
    </div>
  )
}
