import React, { useEffect, useState } from 'react'
import { api } from '../api'

// 🆕 Merged tab (external review follow-up): Onchain and Spread used to be
// separate top-level tabs, and there was nowhere at all to see whether the
// new relative_strength/volume_profile confidence components actually
// correlate with outcomes (previously Discord-only via !components). All
// three are "context that shapes signal quality or execution" rather than
// core trading views like Chart/History, so they're grouped here under one
// tab with a sub-nav instead of competing for space in the main tab bar.

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

// =====================================================================
// 🔗  BIAS (on-chain + derivatives) — ported unchanged from the old
// OnchainPanel.jsx, just renamed to a sub-view instead of a whole tab.
// =====================================================================
function BiasView({ lastEvent, pairs }) {
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

  useEffect(() => {
    if (lastEvent?.type === 'scan_tick') {
      api.getOnchain().then(setData).catch(() => {})
      if (derivTicker) {
        api.getDerivatives(derivTicker).then(setDeriv).catch(() => {})
      }
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

// =====================================================================
// 📏  SPREAD — ported unchanged from the old SpreadPanel.jsx.
// =====================================================================
function SpreadView({ lastEvent, pairs }) {
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
    // a shorter interval than the rest of this tab.
    const id = setInterval(load, 20000)
    return () => clearInterval(id)
  }, [ticker])

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

// =====================================================================
// 🧪  COMPONENTS — new: relative_strength/volume_profile outcome
// correlation, backed by /api/components (state.analyze_confidence_components()
// on the backend — the same function the Discord !components command uses,
// so the two can never disagree with each other).
// =====================================================================
function ComponentRow({ row, minSamples }) {
  return (
    <div className="row" style={{ alignItems: 'flex-start' }}>
      <span className="row-label" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span className={`tag ${row.side === 'long' ? 'long' : 'short'}`} style={{ textTransform: 'uppercase' }}>
          {row.side}
        </span>
        value {row.value > 0 ? `+${row.value}` : row.value}
        {!row.enough_samples && (
          <span style={{ color: 'var(--accent)', fontSize: 11 }} title={`Only ${row.n}/${minSamples} — not enough yet to trust this row`}>
            ⚠ {row.n}/{minSamples}
          </span>
        )}
      </span>
      <span className="row-value" style={{ textAlign: 'right', fontSize: 12, lineHeight: 1.6 }}>
        <div>n={row.n} · win_rate={(row.win_rate * 100).toFixed(0)}% · avg_mfe={row.avg_mfe.toFixed(2)}%</div>
        <div style={{ color: 'var(--text-dim)' }}>
          {row.avg_raw_mfe != null
            ? `raw_mfe=${row.avg_raw_mfe.toFixed(2)}% (n=${row.raw_mfe_n})`
            : 'raw_mfe=n/a yet'}
          {'  '}
          [{Object.entries(row.exit_counts).map(([et, c]) => `${et}=${c}`).join(' ')}]
        </div>
      </span>
    </div>
  )
}

function ComponentsView({ lastEvent }) {
  const [report, setReport] = useState(null)
  const [error, setError] = useState(null)
  const minSamples = 30

  useEffect(() => {
    const load = () => api.getComponents(minSamples).then(setReport).catch((e) => setError(e.message))
    load()
    // This only changes when a trade closes, not every scan tick — a slow
    // poll is enough, no need for spread's 20s cadence here.
    const id = setInterval(load, 60000)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    if (lastEvent?.type === 'scan_tick') {
      api.getComponents(minSamples).then(setReport).catch(() => {})
    }
  }, [lastEvent])

  if (error) {
    return <div className="panel"><p style={{ color: 'var(--short)' }}>Failed to load: {error}</p></div>
  }
  if (!report) {
    return <div className="panel"><p style={{ color: 'var(--text-faint)' }}>Loading…</p></div>
  }

  return (
    <div>
      <div className="panel">
        <h3 className="panel-title">Confidence component analysis</h3>
        <p style={{ fontSize: 13, color: 'var(--text)', marginBottom: 4 }}>
          {report.total_closed} closed records total. Does relative_strength / volume_profile
          (see <code>calc_confidence()</code>) actually correlate with outcome, or just add noise?
        </p>
        <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
          raw_mfe keeps tracking price after a trade closes (see <code>update_raw_outcome()</code>) —
          it's not truncated by whatever TP/SL policy closed the trade, so it's the number worth
          trusting once it has enough samples of its own. win_rate/avg_mfe are only a first look.
        </p>
      </div>

      {['relative_strength', 'volume_profile'].map((compName) => {
        const rows = report.components[compName] || []
        return (
          <div className="panel" key={compName}>
            <h3 className="panel-title">{compName}</h3>
            {rows.length === 0 ? (
              <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>No closed records carry this component yet.</p>
            ) : (
              rows.map((row) => <ComponentRow key={`${row.side}-${row.value}`} row={row} minSamples={minSamples} />)
            )}
          </div>
        )
      })}

      <div className="panel">
        <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
          Rule of thumb: trust a row once n ≥ {minSamples} <em>and</em> raw_mfe's own n is close to it,
          not lagging far behind.
        </p>
      </div>
    </div>
  )
}

// =====================================================================
// 🧪  SIMULATE — new: run !tp1sim / !compsim-equivalent dry-run backtests
// from the web, not just Discord. Both are slow, user-triggered actions
// (not auto-polled like the views above), so this is a form + a button,
// not a useEffect poll.
// =====================================================================
const SIM_TIMEFRAMES = ['1h', '4h'] // matches config.py's TIMEFRAMES — small, fixed list, not worth a round trip

function SimScopeFields({ pairs, ticker, setTicker, tf, setTf, numBars, setNumBars, extra }) {
  return (
    <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-end', marginBottom: 14 }}>
      <div>
        <label style={{ display: 'block', fontSize: 11, color: 'var(--text-faint)', marginBottom: 4 }}>Pair</label>
        <select value={ticker} onChange={(e) => setTicker(e.target.value)}>
          <option value="">All tracked pairs</option>
          {pairs?.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
      </div>
      <div>
        <label style={{ display: 'block', fontSize: 11, color: 'var(--text-faint)', marginBottom: 4 }}>Timeframe</label>
        <select value={tf} onChange={(e) => setTf(e.target.value)}>
          <option value="">All timeframes</option>
          {SIM_TIMEFRAMES.map((f) => <option key={f} value={f}>{f}</option>)}
        </select>
      </div>
      <div>
        <label style={{ display: 'block', fontSize: 11, color: 'var(--text-faint)', marginBottom: 4 }}>Bars</label>
        <input type="number" value={numBars} min={100} step={500}
               onChange={(e) => setNumBars(Number(e.target.value))} style={{ width: 90 }} />
      </div>
      {extra}
    </div>
  )
}

function BarCoverageWarning({ coverage, requested }) {
  const short = (coverage || []).filter((c) => c.got < requested)
  if (short.length === 0) return null
  return (
    <p style={{ fontSize: 11, color: 'var(--accent)', marginBottom: 10 }}>
      ⚠️ Bars requested vs. received (may reflect real available history):{' '}
      {short.map((c) => `${c.ticker} ${c.tf}: got ${c.got}/${requested}`).join(' · ')}
    </p>
  )
}

function Tp1SimSection({ pairs }) {
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(3000)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  const run = async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await api.runTp1Sim({ ticker, tf, numBars })
      setResult(r)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="panel">
      <h3 className="panel-title">TP1-mode comparison</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 12 }}>
        Compares breakeven / quarter / half (current) / three-quarter SL-after-TP1 on real historical data.
        Dry-run — nothing is written to history or changes the live setting.
      </p>
      <SimScopeFields
        pairs={pairs} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={<button onClick={run} disabled={loading}>{loading ? 'Running…' : 'Run'}</button>}
      />
      {error && <p style={{ color: 'var(--short)', fontSize: 13 }}>Failed: {error}</p>}
      {loading && <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>Running backtests across pairs × timeframes × 4 modes — this is slow, sit tight…</p>}
      {result && !loading && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={numBars} />
          {Object.entries(result.modes).map(([modeName, rows]) => (
            <div key={modeName} style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>{modeName}</div>
              {rows.length === 0 ? (
                <p style={{ fontSize: 12, color: 'var(--text-faint)' }}>No TP1-hit trades in this sample.</p>
              ) : (
                rows.map((row, i) => (
                  <div className="row" key={i} style={{ fontSize: 12 }}>
                    <span className="row-label">track={row.track} regime={row.regime}</span>
                    <span className="row-value">
                      n={row.n}{row.n_timeout ? ` (${row.n_timeout} timed-out)` : ''} · win_rate={(row.win_rate * 100).toFixed(0)}% · avg_pnl={row.avg_pnl > 0 ? '+' : ''}{row.avg_pnl.toFixed(2)}%
                      {row.n < 5 && <span style={{ color: 'var(--accent)' }}> ⚠ tiny sample</span>}
                    </span>
                  </div>
                ))
              )}
            </div>
          ))}
          <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
            n_timeout = reached TP1 but neither TP2 nor the moved SL resolved within MAX_HOLD_BARS of entry —
            counted in avg_pnl, not counted as a win. A big gap in n_timeout between modes for the same
            track/regime means the comparison itself is lopsided, not just the underlying trades.
          </p>
        </>
      )}
    </div>
  )
}

function CompSimSection({ pairs }) {
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(3000)
  const [minSamples, setMinSamples] = useState(30)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  const run = async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await api.runCompSim({ ticker, tf, numBars, minSamples })
      setResult(r)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="panel">
      <h3 className="panel-title">Component backtest</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 12 }}>
        Replays real historical data with relative_strength/volume_profile reconstructed point-in-time,
        instead of waiting for the live bot to accumulate enough closed signals. Dry-run — nothing written to history.
      </p>
      <SimScopeFields
        pairs={pairs} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={<button onClick={run} disabled={loading}>{loading ? 'Running…' : 'Run'}</button>}
      />
      {error && <p style={{ color: 'var(--short)', fontSize: 13 }}>Failed: {error}</p>}
      {loading && <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>Running backtests (each also fetches BTC as the relative-strength benchmark) — this is slow, sit tight…</p>}
      {result && !loading && (
        <>
          <p style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 8 }}>{result.total_closed} closed records simulated.</p>
          <BarCoverageWarning coverage={result.bar_coverage} requested={numBars} />
          {['relative_strength', 'volume_profile'].map((compName) => {
            const rows = result.components[compName] || []
            return (
              <div key={compName} style={{ marginBottom: 12 }}>
                <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>{compName}</div>
                {rows.length === 0 ? (
                  <p style={{ fontSize: 12, color: 'var(--text-faint)' }}>No records carry this component.</p>
                ) : (
                  rows.map((row, i) => (
                    <ComponentRow key={i} row={row} minSamples={minSamples} />
                  ))
                )}
              </div>
            )
          })}
          <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
            onchain_bias has no historical equivalent and is simply absent from these records — everything
            else (regime, track, htf, frama, signal_confluence) is reconstructed point-in-time.
          </p>
        </>
      )}
    </div>
  )
}

function SimulateView({ pairs }) {
  return (
    <div>
      <Tp1SimSection pairs={pairs} />
      <CompSimSection pairs={pairs} />
    </div>
  )
}

// =====================================================================
// 🏠  TOP LEVEL — sub-nav between Bias / Spread / Components / Simulate.
// =====================================================================
const VIEWS = [
  { id: 'bias', label: 'Bias' },
  { id: 'spread', label: 'Spread' },
  { id: 'components', label: 'Components' },
  { id: 'simulate', label: 'Simulate' },
]

export default function InsightsPanel({ lastEvent, pairs }) {
  const [view, setView] = useState('bias')

  return (
    <div>
      <div className="seg" style={{ marginBottom: 14 }}>
        {VIEWS.map((v) => (
          <button key={v.id} className={view === v.id ? 'active' : ''} onClick={() => setView(v.id)}>
            {v.label}
          </button>
        ))}
      </div>

      {view === 'bias' && <BiasView lastEvent={lastEvent} pairs={pairs} />}
      {view === 'spread' && <SpreadView lastEvent={lastEvent} pairs={pairs} />}
      {view === 'components' && <ComponentsView lastEvent={lastEvent} />}
      {view === 'simulate' && <SimulateView pairs={pairs} />}
    </div>
  )
}
