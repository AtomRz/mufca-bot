import React, { useEffect, useState, useRef, useCallback } from 'react'
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

// 🆕 Shared polling hook for the read-only views below. They each had their own
// copy of the same fetch pattern with the same problems:
//  * errors were sticky — one failed poll replaced the whole view with
//    "Failed to load" until the page was reloaded (later successes never cleared it);
//  * responses were applied in arrival order, so a slow reply for the previously
//    selected pair could overwrite the current one;
//  * switching pair kept showing the previous pair's numbers (and error) until
//    the new reply arrived;
//  * the scan_tick effect also fired on mount for the event left over from
//    before (a duplicate request), and polling went on in a hidden tab.
// `enabled=false` pauses it (e.g. until a pair is selected); `key` changes reset
// the data; a NEW scan_tick triggers a refresh at most every `minGapMs`.
function usePolled(fetcher, key, intervalMs, lastEvent, { enabled = true, minGapMs = 30000 } = {}) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const seq = useRef(0)
  const lastAt = useRef(0)
  const fetchRef = useRef(fetcher)
  fetchRef.current = fetcher
  const seenEvent = useRef(lastEvent) // the event present at mount is not "new"

  const load = useCallback(() => {
    const mine = ++seq.current
    lastAt.current = Date.now()
    return fetchRef.current()
      .then((d) => { if (mine === seq.current) { setData(d); setError(null) } })
      .catch((e) => { if (mine === seq.current) setError(e.message) })
  }, [])

  useEffect(() => {
    seq.current += 1 // drop anything still in flight for the previous key
    setData(null)
    setError(null)
    if (!enabled) return undefined
    load()
    const id = setInterval(() => { if (!document.hidden) load() }, intervalMs)
    const onVisible = () => { if (document.visibilityState === 'visible') load() }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      clearInterval(id)
      document.removeEventListener('visibilitychange', onVisible)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled, intervalMs])

  useEffect(() => {
    if (!lastEvent || lastEvent === seenEvent.current) return
    seenEvent.current = lastEvent
    if (lastEvent.type === 'scan_tick' && enabled && !document.hidden && Date.now() - lastAt.current >= minGapMs) load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastEvent])

  return { data, error }
}

// 🆕 null-safe number formatting: one missing field used to throw
// "toFixed of null" and the error boundary blanked the whole tab.
const f2 = (v) => (Number.isFinite(v) ? v.toFixed(2) : '—')
const pctSigned = (v, d = 2) => (Number.isFinite(v) ? `${v > 0 ? '+' : ''}${v.toFixed(d)}%` : '—')
const pctWhole = (v) => (Number.isFinite(v) ? `${(v * 100).toFixed(0)}%` : '—')

// the pair selection must follow the live pair list (a removed pair would 404 forever)
function useSelectedPair(pairs) {
  const [ticker, setTicker] = useState(null)
  useEffect(() => {
    if (!pairs) return
    if (!ticker || !pairs.includes(ticker)) setTicker(pairs[0] ?? null)
  }, [pairs, ticker])
  return [ticker, setTicker]
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
  const [derivTicker, setDerivTicker] = useSelectedPair(pairs)
  const { data, error: loadError } = usePolled(() => api.getOnchain(), 'onchain', 60000, lastEvent)
  const { data: deriv, error: derivError } = usePolled(
    () => api.getDerivatives(derivTicker), derivTicker, 60000, lastEvent, { enabled: !!derivTicker },
  )
  const error = data ? null : loadError // with data on screen a failed refresh must not hide it

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
          <span className="row-label" title="Change in exchange ETH balances since the previous refresh (the window follows the on-chain interval in Settings)">Delta (since last refresh)</span>
          <span className="row-value">{flow.delta_eth != null ? `${flow.delta_eth > 0 ? '+' : ''}${Math.round(flow.delta_eth).toLocaleString()} ETH` : '—'}</span>
        </div>
        {flow.note === 'fetch_failed' && (
          <p style={{ fontSize: 12, color: 'var(--accent)', margin: '6px 0' }}>
            ⚠ Etherscan returned no balances this cycle — flow is unknown (not zero); the baseline is kept and it retries shortly.
          </p>
        )}
        {flow.note === 'first_run' && (
          <p style={{ fontSize: 12, color: 'var(--text-faint)', margin: '6px 0' }}>
            Baseline just recorded — the first real delta appears on the next refresh.
          </p>
        )}
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

      <DerivativesSection pairs={pairs} ticker={derivTicker} onTickerChange={setDerivTicker} deriv={deriv} error={deriv ? null : derivError} />
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
              {deriv.bias.oi_note === 'first_run' || deriv.bias.oi_note === 'fetch_failed' || deriv.bias.oi_note === 'error'
                ? (deriv.bias.oi_note === 'first_run' ? '⚪ Baseline just set' : '⚪ Unavailable')
                : deriv.bias.oi_direction === 'rising' ? '📈 Rising' : deriv.bias.oi_direction === 'falling' ? '📉 Falling' : '⚪ Flat'}
              {deriv.bias.oi_note === 'ok' && deriv.bias.oi_delta_pct != null ? ` (${deriv.bias.oi_delta_pct > 0 ? '+' : ''}${(deriv.bias.oi_delta_pct * 100).toFixed(1)}%)` : ''}
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
  const [ticker, setTicker] = useSelectedPair(pairs)
  // Spread moves faster than funding/OI/on-chain data, so it's polled on a
  // shorter interval than the rest of this tab.
  const { data, error: loadError } = usePolled(
    () => api.getSpread(ticker), ticker, 20000, lastEvent, { enabled: !!ticker, minGapMs: 15000 },
  )
  const error = data ? null : loadError

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
        {(row.track || row.regime) && (
          <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>
            {row.track ? `track=${row.track}` : ''}{row.track && row.regime ? ' ' : ''}{row.regime ? `regime=${row.regime}` : ''}
          </span>
        )}
        {row.src === 'shadow' && (
          <span
            style={{ color: 'var(--accent)', fontSize: 11 }}
            title="Entry was switched off in Settings, so no trade was opened — this is the outcome it WOULD have had, replayed on the bars that followed (closed-bar model, not a real fill)."
          >
            shadow
          </span>
        )}
        {!row.enough_samples && (
          <span style={{ color: 'var(--accent)', fontSize: 11 }} title={`Only ${row.n}/${minSamples} — not enough yet to trust this row`}>
            ⚠ {row.n}/{minSamples}
          </span>
        )}
      </span>
      <span className="row-value" style={{ textAlign: 'right', fontSize: 12, lineHeight: 1.6 }}>
        <div>n={row.n} · win_rate={pctWhole(row.win_rate)} · avg_mfe={f2(row.avg_mfe)}%</div>
        <div style={{ color: 'var(--text-dim)' }}>
          {row.avg_raw_mfe != null
            ? `raw_mfe=${f2(row.avg_raw_mfe)}% (n=${row.raw_mfe_n})`
            : 'raw_mfe=n/a yet'}
          {'  '}
          [{Object.entries(row.exit_counts || {}).map(([et, c]) => `${et}=${c}`).join(' ')}]
        </div>
      </span>
    </div>
  )
}

function ComponentsView({ lastEvent }) {
  const minSamples = 30
  const [split, setSplit] = useState(false)
  const [shadow, setShadow] = useState(false)
  // This only changes when a trade closes, not every scan tick — a slow poll is enough.
  const { data: report, error: loadError } = usePolled(
    () => api.getComponents(minSamples, split, shadow),
    `components-${split}-${shadow}`,
    60000,
    lastEvent,
  )
  const error = report ? null : loadError

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
        <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', marginTop: 10, fontSize: 13 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6 }} title="Bucket every row by track (A/U) and regime (TREND/NORMAL) as well — the same split !compsim uses. Live samples per bucket are small, so expect many ⚠ rows.">
            <input type="checkbox" checked={split} onChange={(e) => setSplit(e.target.checked)} />
            Split by track / regime
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6 }} title="Adds rows for signals an Entry switch (Settings) stopped from opening, replayed on the bars that followed. Shown separately, never pooled with real trades.">
            <input type="checkbox" checked={shadow} onChange={(e) => setShadow(e.target.checked)} />
            Include shadow (blocked entries){report.shadow_closed ? ` — ${report.shadow_closed} closed` : ''}
          </label>
        </div>
      </div>

      {['relative_strength', 'volume_profile'].map((compName) => {
        const rows = report.components[compName] || []
        return (
          <div className="panel" key={compName}>
            <h3 className="panel-title">{compName}</h3>
            {rows.length === 0 ? (
              <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>No closed records carry this component yet.</p>
            ) : (
              rows.map((row) => (
                <ComponentRow
                  key={`${row.side}-${row.value}-${row.track ?? ''}-${row.regime ?? ''}-${row.src ?? ''}`}
                  row={row}
                  minSamples={minSamples}
                />
              ))
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

const SIM_MIN_BARS = 300 // matches the server's bound (num_bars: 300..20000)
const SIM_MAX_BARS = 20000
const barsError = (n) =>
  Number.isFinite(n) && n >= SIM_MIN_BARS && n <= SIM_MAX_BARS && Number.isInteger(n)
    ? null
    : `Bars must be a whole number between ${SIM_MIN_BARS} and ${SIM_MAX_BARS}`

function SimScopeFields({ pairs, timeframes, ticker, setTicker, tf, setTf, numBars, setNumBars, extra }) {
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
          {(timeframes && timeframes.length ? timeframes : SIM_TIMEFRAMES).map((f) => <option key={f} value={f}>{f}</option>)}
        </select>
      </div>
      <div>
        <label style={{ display: 'block', fontSize: 11, color: 'var(--text-faint)', marginBottom: 4 }}>Bars</label>
        <input type="number" value={numBars} min={SIM_MIN_BARS} max={SIM_MAX_BARS} step={500}
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

// 🆕 Simulations run as background jobs on the server (see sim_jobs.py): starting one
// returns a job id immediately and this hook polls it every 2 s. A single long HTTP
// request used to be dropped by the Cloudflare Tunnel (~100 s limit) once a run got
// big (e.g. 6000 bars x several pairs), so the page just never showed a result.
// The last job id per kind lives at module level so a finished result (or a running
// job) is still there after switching sub-tabs, which unmounts this view.
const simMemory = { tp1sim: null, compsim: null }

function useSimJob(kind) {
  const [job, setJob] = useState(null)
  const [error, setError] = useState(null)
  const [otherRunning, setOtherRunning] = useState(null)
  const timer = useRef(null)
  const alive = useRef(true)

  const stopPolling = () => {
    if (timer.current) { clearTimeout(timer.current); timer.current = null }
  }

  const poll = (id) => {
    stopPolling()
    timer.current = setTimeout(async () => {
      if (!alive.current) return
      if (document.hidden) { poll(id); return } // don't wake the radio for a hidden tab
      try {
        const j = await api.getSimJob(id)
        if (!alive.current) return
        setJob(j)
        if (j.status === 'running') poll(id)
      } catch (e) {
        if (!alive.current) return
        if (/unknown or expired/i.test(e.message)) {
          simMemory[kind] = null
          setError('The simulation result expired on the server — run it again.')
        } else {
          poll(id) // transient network error (e.g. phone switching networks): keep trying
        }
      }
    }, 2000)
  }

  useEffect(() => {
    alive.current = true
    ;(async () => {
      let adopted = false
      try {
        const { job: active } = await api.getActiveSim()
        if (!alive.current) return
        if (active && active.kind === kind) {
          simMemory[kind] = active.id
          setJob(active)
          poll(active.id)
          adopted = true
        } else if (active) {
          setOtherRunning(active)
        }
      } catch (_) { /* fall through to the remembered job */ }
      if (adopted || !simMemory[kind]) return
      try {
        const j = await api.getSimJob(simMemory[kind])
        if (!alive.current) return
        setJob(j)
        if (j.status === 'running') poll(j.id)
      } catch (_) {
        simMemory[kind] = null
      }
    })()
    return () => { alive.current = false; stopPolling() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const start = async (startRequest) => {
    setError(null)
    setOtherRunning(null)
    try {
      const j = await startRequest()
      simMemory[kind] = j.id
      setJob(j)
      poll(j.id)
    } catch (e) {
      setError(e.message)
    }
  }

  const cancel = async () => {
    if (!job) return
    try { await api.cancelSimJob(job.id) } catch (e) { setError(e.message) }
  }

  return { job, error, setError, otherRunning, start, cancel, running: job?.status === 'running' }
}

function SimProgress({ job, onCancel }) {
  const { done, total, message } = job.progress
  return (
    <div style={{ marginBottom: 10 }}>
      <p style={{ color: 'var(--text-faint)', fontSize: 13, marginBottom: 6 }}>
        Running… {done}/{total || '?'} backtests · {message} · {Math.round(job.elapsed_seconds)}s
        {' '}<button className="btn" onClick={onCancel} style={{ marginLeft: 8 }}>Cancel</button>
      </p>
      <progress value={done} max={Math.max(total, 1)} style={{ width: '100%' }} />
    </div>
  )
}

function SimStatus({ sim }) {
  const { job, error, otherRunning } = sim
  return (
    <>
      {error && <p style={{ color: 'var(--short)', fontSize: 13 }}>Failed: {error}</p>}
      {job && (job.status === 'error' || job.status === 'cancelled') && (
        <p style={{ color: 'var(--short)', fontSize: 13 }}>
          {job.status === 'cancelled' ? 'Cancelled.' : `Failed: ${job.error}`}
        </p>
      )}
      {otherRunning && !sim.running && (
        <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>
          Another simulation ({otherRunning.kind}) is running — {otherRunning.progress.done}/{otherRunning.progress.total} done.
          Only one runs at a time.
        </p>
      )}
      {sim.running && <SimProgress job={job} onCancel={sim.cancel} />}
    </>
  )
}

function Tp1SimSection({ pairs, timeframes }) {
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(3000)
  const sim = useSimJob('tp1sim')
  const result = sim.job?.status === 'done' ? sim.job.result : null
  const ranBars = sim.job?.params?.num_bars ?? numBars // bars of the run that produced `result`, not of the input now

  const run = () => {
    const bad = barsError(numBars)
    if (bad) { sim.setError(bad); return }
    sim.start(() => api.runTp1Sim({ ticker, tf, numBars }))
  }

  return (
    <div className="panel">
      <h3 className="panel-title">TP1-mode comparison</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 12 }}>
        Compares breakeven / quarter / half (current) / three-quarter SL-after-TP1 on real historical data.
        Dry-run — nothing is written to history or changes the live setting.
      </p>
      <SimScopeFields
        pairs={pairs} timeframes={timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={<button onClick={run} disabled={sim.running}>{sim.running ? 'Running…' : 'Run'}</button>}
      />
      <SimStatus sim={sim} />
      {result && !sim.running && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
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
                      n={row.n}{row.n_timeout ? ` (${row.n_timeout} timed-out)` : ''} · win_rate={pctWhole(row.win_rate)} · avg_pnl={pctSigned(row.avg_pnl)}
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

function CompSimSection({ pairs, timeframes }) {
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(3000)
  const [minSamples, setMinSamples] = useState(30)
  const sim = useSimJob('compsim')
  const result = sim.job?.status === 'done' ? sim.job.result : null
  const ranBars = sim.job?.params?.num_bars ?? numBars
  const ranMinSamples = sim.job?.params?.min_samples ?? minSamples

  const run = () => {
    const bad = barsError(numBars)
    if (bad) { sim.setError(bad); return }
    sim.start(() => api.runCompSim({ ticker, tf, numBars, minSamples }))
  }

  return (
    <div className="panel">
      <h3 className="panel-title">Component backtest</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 12 }}>
        Replays real historical data with relative_strength/volume_profile reconstructed point-in-time,
        instead of waiting for the live bot to accumulate enough closed signals. Dry-run — nothing written to history.
      </p>
      <SimScopeFields
        pairs={pairs} timeframes={timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={<button onClick={run} disabled={sim.running}>{sim.running ? 'Running…' : 'Run'}</button>}
      />
      <SimStatus sim={sim} />
      {result && !sim.running && (
        <>
          <p style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 8 }}>{result.total_closed} closed records simulated.</p>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          {['relative_strength', 'volume_profile'].map((compName) => {
            const rows = result.components[compName] || []
            return (
              <div key={compName} style={{ marginBottom: 12 }}>
                <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>{compName}</div>
                {rows.length === 0 ? (
                  <p style={{ fontSize: 12, color: 'var(--text-faint)' }}>No records carry this component.</p>
                ) : (
                  rows.map((row, i) => (
                    <ComponentRow key={i} row={row} minSamples={ranMinSamples} />
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

function SimulateView({ pairs, timeframes }) {
  return (
    <div>
      <Tp1SimSection pairs={pairs} timeframes={timeframes} />
      <CompSimSection pairs={pairs} timeframes={timeframes} />
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

export default function InsightsPanel({ lastEvent, pairs, timeframes }) {
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
      {view === 'simulate' && <SimulateView pairs={pairs} timeframes={timeframes} />}
    </div>
  )
}
