import React, { useEffect, useState, useRef, useCallback } from 'react'
import { api } from '../api'
import { OriginSwitch, useOrigin } from './HistoryPanel'
import { useI18n } from '../i18n'

// Translate a sentence that embeds markup: {name} placeholders in the translated text are
// replaced by the given React nodes, so word order stays free for every language.
function trNodes(t, key, nodes) {
  return t(key).split(/(\{\w+\})/).map((part, i) => {
    const m = /^\{(\w+)\}$/.exec(part)
    return m && m[1] in nodes ? <React.Fragment key={i}>{nodes[m[1]]}</React.Fragment> : part
  })
}

// 🆕 Merged tab (external review follow-up): Onchain and Spread used to be
// separate top-level tabs, and there was nowhere at all to see whether the
// new relative_strength/volume_profile confidence components actually
// correlate with outcomes (previously Discord-only via !components). All
// three are "context that shapes signal quality or execution" rather than
// core trading views like Chart/History, so they're grouped here under one
// tab with a sub-nav instead of competing for space in the main tab bar.

function fmtAge(lastFetch, t) {
  if (!lastFetch) return '—'
  const mins = Math.floor((Date.now() / 1000 - lastFetch) / 60)
  if (mins < 1) return t('just now')
  if (mins < 60) return t('{m}m ago', { m: mins })
  const hrs = Math.floor(mins / 60)
  return t('{h}h {m}m ago', { h: hrs, m: mins % 60 })
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

  return { data, error, reload: load }
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
  const { t } = useI18n()
  // value is -15..+15 — render as a centered bar, green right / red left
  const pct = Math.min(100, Math.abs(value) / 15 * 100)
  const positive = value >= 0
  return (
    <div style={{ marginBottom: 10 }}>
      <div className="row" style={{ borderBottom: 'none', padding: '0 0 4px' }}>
        <span className="row-label">{t(label)}</span>
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
  const { t } = useI18n()
  return (
    <div className="row">
      <span className="row-label">{t(label)}</span>
      <span className="row-value">
        <span style={{ color: 'var(--long)' }}>{t('L {v}×', { v: long })}</span>
        {'  '}
        <span style={{ color: 'var(--short)' }}>{t('S {v}×', { v: short })}</span>
      </span>
    </div>
  )
}

// =====================================================================
// 🔗  BIAS (on-chain + derivatives) — ported unchanged from the old
// OnchainPanel.jsx, just renamed to a sub-view instead of a whole tab.
// =====================================================================
function BiasView({ lastEvent, pairs }) {
  const { t, locale } = useI18n()
  const [derivTicker, setDerivTicker] = useSelectedPair(pairs)
  const { data, error: loadError } = usePolled(() => api.getOnchain(), 'onchain', 60000, lastEvent)
  const { data: deriv, error: derivError } = usePolled(
    () => api.getDerivatives(derivTicker), derivTicker, 60000, lastEvent, { enabled: !!derivTicker },
  )
  const error = data ? null : loadError // with data on screen a failed refresh must not hide it

  if (error) {
    return <div className="panel"><p style={{ color: 'var(--short)' }}>{t('Failed to load: {error}', { error })}</p></div>
  }
  if (!data) {
    return <div className="panel"><p style={{ color: 'var(--text-faint)' }}>{t('Loading…')}</p></div>
  }
  if (!data.enabled) {
    return (
      <div className="panel">
        <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>
          {trNodes(t, 'Disabled — set {k1} and {k2} in {env} to enable on-chain bias (ETH exchange flow, Fear & Greed, BTC dominance) and its effect on confidence/TP/SL/leverage.', {
            k1: <code>ETHERSCAN_API_KEY</code>, k2: <code>COINGECKO_API_KEY</code>, env: <code>.env</code>,
          })}
        </p>
      </div>
    )
  }

  const bias = data.bias
  if (!bias) {
    return (
      <div className="panel">
        <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>
          {t('Enabled, first refresh not completed yet — checks in hourly, this fills in on the next cycle.')}
        </p>
      </div>
    )
  }

  const flow = bias.flow_data || {}
  const cg = bias.cg_data || {}

  return (
    <div>
      <div className="panel">
        <h3 className="panel-title">{t('On-chain bias')}</h3>
        <p style={{ fontSize: 13, color: 'var(--text)', marginBottom: 14 }}>{bias.summary}</p>
        <BiasBar label="Long bias" value={bias.bias_long ?? 0} />
        <BiasBar label="Short bias" value={bias.bias_short ?? 0} />
        <div className="row">
          <span className="row-label">{t('Leverage adjustment')}</span>
          <span className="row-value">{bias.lev_delta > 0 ? `+${bias.lev_delta}` : bias.lev_delta}</span>
        </div>
        <div className="row">
          <span className="row-label">{t('Last refreshed')}</span>
          <span className="row-value">{fmtAge(data.last_fetch, t)}</span>
        </div>
      </div>

      <div className="panel">
        <h3 className="panel-title">{t('ETH exchange flow')}</h3>
        <div className="row">
          <span className="row-label">{t('Direction')}</span>
          <span className={`tag ${flow.flow === 'outflow' ? 'long' : flow.flow === 'inflow' ? 'short' : 'flat'}`}>
            {flow.flow === 'outflow' ? t('🟢 Outflow') : flow.flow === 'inflow' ? t('🔴 Inflow') : t('⚪ Neutral')}
          </span>
        </div>
        <div className="row">
          <span className="row-label" title={t('Change in exchange ETH balances since the previous refresh (the window follows the on-chain interval in Settings)')}>{t('Delta (since last refresh)')}</span>
          <span className="row-value">{flow.delta_eth != null ? `${flow.delta_eth > 0 ? '+' : ''}${Math.round(flow.delta_eth).toLocaleString(locale)} ETH` : '—'}</span>
        </div>
        {flow.note === 'fetch_failed' && (
          <p style={{ fontSize: 12, color: 'var(--accent)', margin: '6px 0' }}>
            {t('⚠ Etherscan returned no balances this cycle — flow is unknown (not zero); the baseline is kept and it retries shortly.')}
          </p>
        )}
        {flow.note === 'first_run' && (
          <p style={{ fontSize: 12, color: 'var(--text-faint)', margin: '6px 0' }}>
            {t('Baseline just recorded — the first real delta appears on the next refresh.')}
          </p>
        )}
        <div className="row">
          <span className="row-label">{t('Strength')}</span>
          <span className="row-value">{flow.strength || '—'}</span>
        </div>
        {flow.per_exchange && Object.keys(flow.per_exchange).length > 0 && (
          <div style={{ marginTop: 8, paddingTop: 8, borderTop: '1px solid var(--border-soft)' }}>
            {Object.entries(flow.per_exchange).map(([name, delta]) => (
              <div className="row" key={name} style={{ fontSize: 12, padding: '4px 0' }}>
                <span className="row-label">{name}</span>
                <span className="row-value" style={{ color: delta > 0 ? 'var(--short)' : delta < 0 ? 'var(--long)' : undefined }}>
                  {delta > 0 ? '+' : ''}{delta.toLocaleString(locale)} ETH
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      <WalletTracePanel />

      <div className="panel">
        <h3 className="panel-title">{t('Market sentiment')}</h3>
        <div className="row">
          <span className="row-label">{t('Fear & Greed')}</span>
          <span className="row-value">{bias.fear_and_greed ?? cg.fear_and_greed ?? '—'} ({bias.fg_label || cg.fg_label || '—'})</span>
        </div>
        <div className="row">
          <span className="row-label">{t('BTC dominance')}</span>
          <span className="row-value">{cg.btc_dominance != null ? `${cg.btc_dominance.toFixed(2)}%` : '—'}</span>
        </div>
      </div>

      <div className="panel">
        <h3 className="panel-title">{t('Applied multipliers')}</h3>
        <MultRow label="Take Profit" long={bias.tp_mult_long} short={bias.tp_mult_short} />
        <MultRow label="Stop Loss" long={bias.sl_mult_long} short={bias.sl_mult_short} />
        <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
          {trNodes(t, 'Applied to newly opened positions only — see {code}. SL widening is capped regardless of these multipliers.', {
            code: <code>apply_onchain_with_safety()</code>,
          })}
        </p>
      </div>

      <DerivativesSection pairs={pairs} ticker={derivTicker} onTickerChange={setDerivTicker} deriv={deriv} error={deriv ? null : derivError} />
    </div>
  )
}

function DerivativesSection({ pairs, ticker, onTickerChange, deriv, error }) {
  const { t } = useI18n()
  return (
    <div className="panel">
      <h3 className="panel-title">{t('Derivatives (Futures)')}</h3>
      {pairs?.length > 1 && (
        <div className="seg" style={{ marginBottom: 12 }}>
          {pairs.map((p) => (
            <button key={p} className={ticker === p ? 'active' : ''} onClick={() => onTickerChange(p)}>
              {p}
            </button>
          ))}
        </div>
      )}

      {error && <p style={{ color: 'var(--short)', fontSize: 13 }}>{t('Failed to load: {error}', { error })}</p>}

      {!error && !deriv && <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>{t('Loading…')}</p>}

      {!error && deriv && !deriv.enabled && (
        <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>
          {deriv.note === 'spot_mode'
            ? t('Only applies in Futures mode — currently in Spot mode.')
            : t('Disabled — enable it from the Settings panel to see funding rate and open interest bias here.')}
        </p>
      )}

      {!error && deriv?.enabled && !deriv.bias && (
        <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>
          {t('First refresh not completed yet — this fills in on the next cycle.')}
        </p>
      )}

      {!error && deriv?.enabled && deriv.bias && (
        <>
          <div className="row">
            <span className="row-label">{t('Funding rate')}</span>
            <span className="row-value">
              {deriv.bias.funding_rate != null ? `${(deriv.bias.funding_rate * 100).toFixed(4)}%` : '—'}
            </span>
          </div>
          <div className="row">
            <span className="row-label">{t('Open interest')}</span>
            <span className={`tag ${deriv.bias.oi_direction === 'rising' ? 'long' : deriv.bias.oi_direction === 'falling' ? 'short' : 'flat'}`}>
              {deriv.bias.oi_note === 'first_run' || deriv.bias.oi_note === 'fetch_failed' || deriv.bias.oi_note === 'error'
                ? (deriv.bias.oi_note === 'first_run' ? t('⚪ Baseline just set') : t('⚪ Unavailable'))
                : deriv.bias.oi_direction === 'rising' ? t('📈 Rising') : deriv.bias.oi_direction === 'falling' ? t('📉 Falling') : t('⚪ Flat')}
              {deriv.bias.oi_note === 'ok' && deriv.bias.oi_delta_pct != null ? ` (${deriv.bias.oi_delta_pct > 0 ? '+' : ''}${(deriv.bias.oi_delta_pct * 100).toFixed(1)}%)` : ''}
            </span>
          </div>
          <BiasBar label="Long bias" value={deriv.bias.bias_long ?? 0} />
          <BiasBar label="Short bias" value={deriv.bias.bias_short ?? 0} />
          <div className="row">
            <span className="row-label">{t('Leverage adjustment')}</span>
            <span className="row-value">{deriv.bias.lev_delta > 0 ? `+${deriv.bias.lev_delta}` : deriv.bias.lev_delta}</span>
          </div>
          <p style={{ fontSize: 13, color: 'var(--text)', marginTop: 10 }}>{deriv.bias.summary}</p>
          <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
            {trNodes(t, 'Combined with the on-chain bias above before being applied to new positions — see {code}.', {
              code: <code>derivatives.combine_biases()</code>,
            })}
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
  const { t } = useI18n()
  const [ticker, setTicker] = useSelectedPair(pairs)
  // Spread moves faster than funding/OI/on-chain data, so it's polled on a
  // shorter interval than the rest of this tab.
  const { data, error: loadError, reload } = usePolled(
    () => api.getSpread(ticker), ticker, 20000, lastEvent, { enabled: !!ticker, minGapMs: 15000 },
  )
  const error = data ? null : loadError

  const filterOn = data?.filter_enabled
  const snap = data?.snapshot

  // Reset of this pair's warm-up history, same as Discord `!spread <pair> reset yes`.
  // Two clicks on purpose (window.confirm is unreliable inside an Android WebView).
  const [armed, setArmed] = useState(false)
  const [resetBusy, setResetBusy] = useState(false)
  const [resetMsg, setResetMsg] = useState(null)   // { ok: boolean, text: string }
  useEffect(() => { setArmed(false); setResetMsg(null) }, [ticker])   // never carry a half-done reset to another pair

  const doReset = async () => {
    const pair = ticker
    setArmed(false)
    setResetBusy(true)
    setResetMsg(null)
    try {
      const r = await api.resetSpread(pair)
      setResetMsg({ ok: true, text: t('Cleared {n} sample(s) for {pair} — warm-up starts over.', { n: r.cleared_samples, pair }) })
      reload()
    } catch (e) {
      setResetMsg({ ok: false, text: e.message })
    } finally {
      setResetBusy(false)
    }
  }

  return (
    <div>
      <div className="panel">
        <h3 className="panel-title">{t('Order book spread')}</h3>

        {pairs?.length > 1 && (
          <div className="seg" style={{ marginBottom: 12 }}>
            {pairs.map((p) => (
              <button key={p} className={ticker === p ? 'active' : ''} onClick={() => setTicker(p)}>
                {p}
              </button>
            ))}
          </div>
        )}

        {error && <p style={{ color: 'var(--short)', fontSize: 13 }}>{t('Failed to load: {error}', { error })}</p>}

        {!error && !data && <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>{t('Loading…')}</p>}

        {!error && data && (
          <>
            <div className="row">
              <span className="row-label">{t('Filter')}</span>
              <span className={`tag ${filterOn ? 'long' : 'flat'}`}>
                {filterOn ? t('🟢 ON') : t('⚪ OFF (warm-up/logging only)')}
              </span>
            </div>
            {snap && (
              <>
                <div className="row">
                  <span className="row-label">{t('Bid / Ask')}</span>
                  <span className="row-value">{snap.bid} / {snap.ask}</span>
                </div>
                <div className="row">
                  <span className="row-label">{t('Current spread')}</span>
                  <span className="row-value">{(snap.spread_pct * 100).toFixed(3)}%</span>
                </div>
                <div className="row">
                  <span className="row-label">{t('Rolling median')}</span>
                  <span className="row-value">
                    {snap.rolling_median != null ? `${(snap.rolling_median * 100).toFixed(3)}%` : t('n/a')}
                  </span>
                </div>
                <div className="row">
                  <span className="row-label">{t('Warm-up')}</span>
                  <span className={`tag ${data.warmed_up ? 'long' : 'flat'}`}>
                    {data.warmed_up
                      ? t('{n}/{min} samples — warmed up', { n: snap.sample_count, min: data.min_samples_for_anomaly })
                      : t('{n}/{min} samples', { n: snap.sample_count, min: data.min_samples_for_anomaly })}
                  </span>
                </div>
              </>
            )}
            <div style={{ marginTop: 12, display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              {armed ? (
                <>
                  <button className="btn" disabled={resetBusy} onClick={doReset}
                    style={{ borderColor: 'var(--short)', color: 'var(--short)' }}>
                    {t('Confirm: clear {n} sample(s) of {pair}', { n: snap ? snap.sample_count : '', pair: ticker })}
                  </button>
                  <button className="btn" onClick={() => setArmed(false)}>{t('Cancel')}</button>
                </>
              ) : (
                <button className="btn" disabled={resetBusy || !ticker} onClick={() => { setResetMsg(null); setArmed(true) }}>
                  {resetBusy ? t('Resetting…') : t('Reset history')}
                </button>
              )}
            </div>
            {armed && (
              <p style={{ fontSize: 12, color: 'var(--text-faint)', margin: '8px 0 0' }}>
                {t('Warm-up starts over for {pair}: the "3x the rolling median" condition is skipped until {n} new samples are collected. The "15% of SL distance" condition keeps working.', { pair: ticker, n: data.min_samples_for_anomaly })}
              </p>
            )}
            {resetMsg && (
              <p style={{ fontSize: 12, margin: '8px 0 0', color: resetMsg.ok ? 'var(--long)' : 'var(--short)' }}>
                {resetMsg.text}
              </p>
            )}
          </>
        )}
      </div>

      <div className="panel">
        <h3 className="panel-title">{t('How this filter works')}</h3>
        <p style={{ fontSize: 13, color: 'var(--text)', marginBottom: 10 }}>
          {t('Two relative conditions, either one blocks a signal once the toggle in Settings is on:')}
        </p>
        <ul style={{ fontSize: 13, color: 'var(--text)', margin: 0, paddingLeft: 20, lineHeight: 1.6 }}>
          <li>{trNodes(t, 'Spread eats more than 15% of {em} — no warm-up needed, works from the first reading.', { em: <em>{t("this signal's own SL distance")}</em> })}</li>
          <li>{trNodes(t, "Spread reads more than 3x wider than {em} — needs 30 samples before it activates; until then it's simply skipped, not treated as a block.", { em: <em>{t("this pair's own recent rolling median")}</em> })}</li>
        </ul>
        <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 12 }}>
          {t("Not backtestable — order book depth isn't part of OHLCV history, so this only ever gates the live scan path, never the backtest. A flat percent-of-price cutoff was deliberately avoided — a major like BTC and a thin alt don't share a normal spread range, so both conditions above are relative instead of absolute. Collection runs every scan tick for every tracked pair regardless of the toggle, so flipping it on later starts from an already-warmed-up baseline. Toggle it on from Settings → Signal Filters → \"Order book spread\".")}
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
  const { t } = useI18n()
  return (
    <div className="row" style={{ alignItems: 'flex-start' }}>
      <span className="row-label" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span className={`tag ${row.side === 'long' ? 'long' : 'short'}`} style={{ textTransform: 'uppercase' }}>
          {t(row.side)}
        </span>
        {t('value {v}', { v: row.value > 0 ? `+${row.value}` : row.value })}
        {(row.track || row.regime || row.ticker) && (
          <span style={{ color: 'var(--text-dim)', fontSize: 11 }}>
            {row.ticker ? `${row.ticker} ` : ''}{row.track ? `track=${row.track}` : ''}{row.track && row.regime ? ' ' : ''}{row.regime ? `regime=${row.regime}` : ''}
          </span>
        )}
        {row.src === 'shadow' && (
          <span
            style={{ color: 'var(--accent)', fontSize: 11 }}
            title={t('Entry was switched off in Settings, so no trade was opened — this is the outcome it WOULD have had, replayed on the bars that followed (closed-bar model, not a real fill).')}
          >
            {t('shadow')}
          </span>
        )}
        {!row.enough_samples && (
          <span style={{ color: 'var(--accent)', fontSize: 11 }} title={t('Only {n}/{min} — not enough yet to trust this row', { n: row.n, min: minSamples })}>
            ⚠ {row.n}/{minSamples}
          </span>
        )}
      </span>
      <span className="row-value" style={{ textAlign: 'right', fontSize: 12, lineHeight: 1.6 }}>
        <div>n={row.n} · win_rate={pctWhole(row.win_rate)} · avg_mfe={f2(row.avg_mfe)}%</div>
        <div style={{ color: 'var(--text-dim)' }}>
          {row.avg_raw_mfe != null
            ? `raw_mfe=${f2(row.avg_raw_mfe)}% (n=${row.raw_mfe_n})`
            : t('raw_mfe=n/a yet')}
          {'  '}
          [{Object.entries(row.exit_counts || {}).map(([et, c]) => `${et}=${c}`).join(' ')}]
        </div>
      </span>
    </div>
  )
}

const VERDICT_STYLE = {
  supported: { icon: '✅', color: 'var(--long)', text: 'supported' },
  mixed: { icon: '🟡', color: 'var(--accent)', text: 'mixed' },
  not_supported: { icon: '❌', color: 'var(--short)', text: 'not supported' },
  too_few: { icon: '⚠️', color: 'var(--text-faint)', text: 'too few trades' },
}
const fmtN = (v, d = 2, na = 'n/a') => (v === null || v === undefined ? na : `${v > 0 ? '+' : ''}${Number(v).toFixed(d)}`)

// Would an entry filter "only structure-confirmed trades" have paid in NET PnL? Read-only.
function StructCheckPanel({ origin }) {
  const { t } = useI18n()
  const fmt = (v, d = 2) => fmtN(v, d, t('n/a'))
  const [side, setSide] = useState('long')
  const [trendOnly, setTrendOnly] = useState(false)
  const [rep, setRep] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => {
    let alive = true
    setErr(null)
    api.getStructCheck(side, origin, trendOnly ? 'TREND' : '')
      .then((r) => { if (alive) setRep(r) })
      .catch((e) => { if (alive) { setRep(null); setErr(String(e.message || e)) } })
    return () => { alive = false }
  }, [side, origin, trendOnly])

  return (
    <div className="panel">
      <h3 className="panel-title">{t('Structure confirm — would it have paid?')}</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 8 }}>
        {t('Net PnL (after the assumed fee) of the trades a rule would keep vs reject. A rule is “supported” only when the bootstrap interval of the difference is above 0, the older and the newer half of the history both agree and most pairs agree. Backtest records need their components filled first (Backup tab). Changes nothing.')}
      </p>
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'center', marginBottom: 10, fontSize: 13 }}>
        <select value={side} onChange={(e) => setSide(e.target.value)}>
          <option value="long">{t('long')}</option>
          <option value="short">{t('short')}</option>
        </select>
        <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <input type="checkbox" checked={trendOnly} onChange={(e) => setTrendOnly(e.target.checked)} />
          {t('TREND only')}
        </label>
      </div>
      {err && <p style={{ color: 'var(--short)' }}>{t('Failed to load: {error}', { error: err })}</p>}
      {!err && !rep && <p style={{ color: 'var(--text-faint)' }}>{t('Loading…')}</p>}
      {rep && (
        <div>
          <p style={{ fontSize: 12, color: 'var(--text)', marginBottom: 8 }}>
            {t('{n} closed {side}s with components (skipped {cancelled} cancelled, {nocomp} without components) · baseline total {total}% · avg {avg}% · fee {fee}%', {
              n: rep.n, side: t(rep.side), cancelled: rep.skipped.cancelled, nocomp: rep.skipped.no_components,
              total: fmt(rep.baseline.total), avg: fmt(rep.baseline.avg, 3), fee: rep.fee_pct,
            })}
          </p>
          {rep.rules.map((r) => {
            const v = VERDICT_STYLE[r.verdict] || VERDICT_STYLE.too_few
            return (
              <div className="row" key={r.rule} style={{ alignItems: 'flex-start' }}>
                <span className="row-label"><code>{r.rule}</code> <span style={{ color: v.color, fontSize: 11 }}>{v.icon} {t(v.text)}</span></span>
                <span style={{ fontSize: 12 }}>
                  {t('keeps {share} · kept n={kn} avg {kavg} total {ktotal} · rejected n={rn} avg {ravg}', {
                    share: r.kept_share === null ? t('n/a') : `${Math.round(r.kept_share * 100)}%`,
                    kn: r.kept.n, kavg: fmt(r.kept.avg, 3), ktotal: fmt(r.kept.total), rn: r.rejected.n, ravg: fmt(r.rejected.avg, 3),
                  })}
                  <br />
                  {t('diff {diff} CI95 {ci} · {halves} · pairs positive {pos}/{n}', {
                    diff: fmt(r.diff_avg, 3),
                    ci: r.ci95 ? `[${fmt(r.ci95[0])}, ${fmt(r.ci95[1])}]` : t('n/a'),
                    halves: r.halves.map((h) => `${h.half} ${fmt(h.diff)}`).join(' / '),
                    pos: r.pairs_pos, n: r.pairs_n,
                  })}
                </span>
              </div>
            )
          })}
          <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 8 }}>
            {t('Pairs share one market phase and A/U trades often share a bar, so the intervals are optimistic — a floor on the uncertainty, not a guarantee.')}
          </p>
        </div>
      )}
    </div>
  )
}

function ComponentsView({ lastEvent }) {
  const { t } = useI18n()
  const minSamples = 30
  const [split, setSplit] = useState(false)
  const [shadow, setShadow] = useState(false)
  const [byTicker, setByTicker] = useState(false)
  const [origin, setOrigin] = useOrigin()
  // This only changes when a trade closes, not every scan tick — a slow poll is enough.
  const { data: report, error: loadError } = usePolled(
    () => api.getComponents(minSamples, split, shadow, origin, byTicker),
    `components-${split}-${shadow}-${origin}-${byTicker}`,
    60000,
    lastEvent,
  )
  const error = report ? null : loadError

  if (error) {
    return <div className="panel"><p style={{ color: 'var(--short)' }}>{t('Failed to load: {error}', { error })}</p></div>
  }
  if (!report) {
    return <div className="panel"><p style={{ color: 'var(--text-faint)' }}>{t('Loading…')}</p></div>
  }

  return (
    <div>
      <div className="panel">
        <h3 className="panel-title">{t('Confidence component analysis')}</h3>
        <p style={{ fontSize: 13, color: 'var(--text)', marginBottom: 4 }}>
          {origin === 'live'
            ? t('{n} closed live records total.', { n: report.total_closed })
            : origin === 'backtest'
              ? t('{n} closed backtest records total.', { n: report.total_closed })
              : t('{n} closed records total.', { n: report.total_closed })}
          {' '}
          {trNodes(t, 'Does relative_strength / volume_profile (see {code}) actually correlate with outcome, or just add noise?', {
            code: <code>calc_confidence()</code>,
          })}
        </p>
        <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
          {trNodes(t, "raw_mfe keeps tracking price after a trade closes (see {code}) — it's not truncated by whatever TP/SL policy closed the trade, so it's the number worth trusting once it has enough samples of its own. win_rate/avg_mfe are only a first look.", {
            code: <code>update_raw_outcome()</code>,
          })}
        </p>
        <div style={{ marginTop: 10 }}><OriginSwitch origin={origin} onChange={setOrigin} /></div>
        {report.components_in_score === false && (
          <p style={{ fontSize: 11, color: 'var(--accent)', marginTop: 6 }}>
            {t('These two components no longer add to the displayed confidence score (no effect that held up). They are still calculated and stored on every record, so this analysis keeps working.')}
          </p>
        )}
        {origin === 'backtest' && (
          <p style={{ fontSize: 11, color: 'var(--accent)', marginTop: 6 }}>
            {t('Backtest records get raw_mfe from the bars after entry (the same MAX_HOLD_BARS window live tracks). Older stored ones are filled from the Backup tab ("raw_mfe for backtest records"), and their confidence components from "Components for backtest records" — until then the component sections below stay empty and raw_mfe reads n/a.')}
          </p>
        )}
        <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', marginTop: 10, fontSize: 13 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6 }} title={t('Bucket every row by track (A/U) and regime (TREND/NORMAL) as well — the same split !compsim uses. Live samples per bucket are small, so expect many ⚠ rows.')}>
            <input type="checkbox" checked={split} onChange={(e) => setSplit(e.target.checked)} />
            {t('Split by track / regime')}
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6 }} title={t('Bucket every row by pair as well. relative_strength is never counted for BTC/USDT (it is the benchmark, so its 0 means n/a, not neutral).')}>
            <input type="checkbox" checked={byTicker} onChange={(e) => setByTicker(e.target.checked)} />
            {t('Split by pair')}
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6 }} title={t('Adds rows for signals an Entry switch (Settings) stopped from opening, replayed on the bars that followed. Shown separately, never pooled with real trades.')}>
            <input type="checkbox" checked={shadow} onChange={(e) => setShadow(e.target.checked)} />
            {report.shadow_closed
              ? t('Include shadow (blocked entries) — {n} closed', { n: report.shadow_closed })
              : t('Include shadow (blocked entries)')}
          </label>
        </div>
      </div>

      {['relative_strength', 'volume_profile'].map((compName) => {
        const rows = report.components[compName] || []
        return (
          <div className="panel" key={compName}>
            <h3 className="panel-title">{compName}</h3>
            {rows.length === 0 ? (
              <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>{t('No closed records carry this component yet.')}</p>
            ) : (
              rows.map((row) => (
                <ComponentRow
                  key={`${row.side}-${row.value}-${row.track ?? ''}-${row.regime ?? ''}-${row.src ?? ''}-${row.ticker ?? ''}`}
                  row={row}
                  minSamples={minSamples}
                />
              ))
            )}
          </div>
        )
      })}

      <StructCheckPanel origin={origin} />

      <div className="panel">
        <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
          {trNodes(t, "Rule of thumb: trust a row once n ≥ {min} {and} raw_mfe's own n is close to it, not lagging far behind.", {
            min: minSamples, and: <em>{t('and')}</em>,
          })}
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
export const barsError = (n) =>
  Number.isFinite(n) && n >= SIM_MIN_BARS && n <= SIM_MAX_BARS && Number.isInteger(n)
    ? null
    : `Bars must be a whole number between ${SIM_MIN_BARS} and ${SIM_MAX_BARS}` // translated for display in SimStatus

export function SimScopeFields({ pairs, timeframes, ticker, setTicker, tf, setTf, numBars, setNumBars, extra }) {
  const { t } = useI18n()
  return (
    <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-end', marginBottom: 14 }}>
      <div>
        <label style={{ display: 'block', fontSize: 11, color: 'var(--text-faint)', marginBottom: 4 }}>{t('Pair')}</label>
        <select value={ticker} onChange={(e) => setTicker(e.target.value)}>
          <option value="">{t('All tracked pairs')}</option>
          {pairs?.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
      </div>
      <div>
        <label style={{ display: 'block', fontSize: 11, color: 'var(--text-faint)', marginBottom: 4 }}>{t('Timeframe')}</label>
        <select value={tf} onChange={(e) => setTf(e.target.value)}>
          <option value="">{t('All timeframes')}</option>
          {(timeframes && timeframes.length ? timeframes : SIM_TIMEFRAMES).map((f) => <option key={f} value={f}>{f}</option>)}
        </select>
      </div>
      <div>
        <label style={{ display: 'block', fontSize: 11, color: 'var(--text-faint)', marginBottom: 4 }}>{t('Bars')}</label>
        <input type="number" value={numBars} min={SIM_MIN_BARS} max={SIM_MAX_BARS} step={500}
               onChange={(e) => setNumBars(Number(e.target.value))} style={{ width: 90 }} />
      </div>
      {extra}
    </div>
  )
}

export function BarCoverageWarning({ coverage, requested }) {
  const { t } = useI18n()
  const short = (coverage || []).filter((c) => c.got < requested)
  if (short.length === 0) return null
  return (
    <p style={{ fontSize: 11, color: 'var(--accent)', marginBottom: 10 }}>
      {t('⚠️ Bars requested vs. received (may reflect real available history):')}{' '}
      {short.map((c) => t('{ticker} {tf}: got {got}/{req}', { ticker: c.ticker, tf: c.tf, got: c.got, req: requested })).join(' · ')}
    </p>
  )
}

// 🆕 Simulations run as background jobs on the server (see sim_jobs.py): starting one
// returns a job id immediately and this hook polls it every 2 s. A single long HTTP
// request used to be dropped by the Cloudflare Tunnel (~100 s limit) once a run got
// big (e.g. 6000 bars x several pairs), so the page just never showed a result.
// The last job id per kind lives at module level so a finished result (or a running
// job) is still there after switching sub-tabs, which unmounts this view.
const simMemory = { tp1sim: null, compsim: null, autofilter: null, indoptim: null, autopsy: null, missedsim: null, smcsim: null, volfit: null, reconcile: null, rawfill: null, compfill: null }

export function useSimJob(kind) {
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
  const { t } = useI18n()
  const { done, total, message } = job.progress
  return (
    <div style={{ marginBottom: 10 }}>
      <p style={{ color: 'var(--text-faint)', fontSize: 13, marginBottom: 6 }}>
        {t('Running… {done}/{total} backtests · {message} · {sec}s', { done, total: total || '?', message, sec: Math.round(job.elapsed_seconds) })}
        {' '}<button className="btn" onClick={onCancel} style={{ marginLeft: 8 }}>{t('Cancel')}</button>
      </p>
      <progress value={done} max={Math.max(total, 1)} style={{ width: '100%' }} />
    </div>
  )
}

// Frontend-composed sim errors are English strings (stored in state / returned by barsError);
// translate the known ones at display time, pass backend messages through untouched.
function simErrorText(t, msg) {
  const m = /^Bars must be a whole number between (\d+) and (\d+)$/.exec(msg || '')
  if (m) return t('Bars must be a whole number between {min} and {max}', { min: m[1], max: m[2] })
  if (msg === 'The simulation result expired on the server — run it again.') {
    return t('The simulation result expired on the server — run it again.')
  }
  return msg
}

export function SimStatus({ sim }) {
  const { t } = useI18n()
  const { job, error, otherRunning } = sim
  return (
    <>
      {error && <p style={{ color: 'var(--short)', fontSize: 13 }}>{t('Failed: {error}', { error: simErrorText(t, error) })}</p>}
      {job && (job.status === 'error' || job.status === 'cancelled') && (
        <p style={{ color: 'var(--short)', fontSize: 13 }}>
          {job.status === 'cancelled' ? t('Cancelled.') : t('Failed: {error}', { error: job.error })}
        </p>
      )}
      {otherRunning && !sim.running && (
        <p style={{ color: 'var(--text-faint)', fontSize: 13 }}>
          {t('Another simulation ({kind}) is running — {done}/{total} done. Only one runs at a time.', {
            kind: otherRunning.kind, done: otherRunning.progress.done, total: otherRunning.progress.total,
          })}
        </p>
      )}
      {sim.running && <SimProgress job={job} onCancel={sim.cancel} />}
    </>
  )
}

function Tp1SimSection({ pairs, timeframes }) {
  const { t } = useI18n()
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
      <h3 className="panel-title">{t('TP1-mode comparison')}</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 12 }}>
        {t('Compares breakeven / quarter / half (current) / three-quarter SL-after-TP1 on real historical data. Dry-run — nothing is written to history or changes the live setting.')}
      </p>
      <SimScopeFields
        pairs={pairs} timeframes={timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={<button onClick={run} disabled={sim.running}>{sim.running ? t('Running…') : t('Run')}</button>}
      />
      <SimStatus sim={sim} />
      {result && !sim.running && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          {Object.entries(result.modes).map(([modeName, rows]) => (
            <div key={modeName} style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>{modeName}</div>
              {rows.length === 0 ? (
                <p style={{ fontSize: 12, color: 'var(--text-faint)' }}>{t('No TP1-hit trades in this sample.')}</p>
              ) : (
                rows.map((row, i) => (
                  <div className="row" key={i} style={{ fontSize: 12 }}>
                    <span className="row-label">track={row.track} regime={row.regime}</span>
                    <span className="row-value">
                      n={row.n}{row.n_timeout ? ` ${t('({n} timed-out)', { n: row.n_timeout })}` : ''} · win_rate={pctWhole(row.win_rate)} · avg_pnl={pctSigned(row.avg_pnl)}
                      {row.n < 5 && <span style={{ color: 'var(--accent)' }}> {t('⚠ tiny sample')}</span>}
                    </span>
                  </div>
                ))
              )}
            </div>
          ))}
          <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
            {t('n_timeout = reached TP1 but neither TP2 nor the moved SL resolved within MAX_HOLD_BARS of entry — counted in avg_pnl, not counted as a win. A big gap in n_timeout between modes for the same track/regime means the comparison itself is lopsided, not just the underlying trades.')}
          </p>
        </>
      )}
    </div>
  )
}

function CompSimSection({ pairs, timeframes }) {
  const { t } = useI18n()
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
      <h3 className="panel-title">{t('Component backtest')}</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 12 }}>
        {t('Replays real historical data with relative_strength/volume_profile reconstructed point-in-time, instead of waiting for the live bot to accumulate enough closed signals. Dry-run — nothing written to history.')}
      </p>
      <SimScopeFields
        pairs={pairs} timeframes={timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={<button onClick={run} disabled={sim.running}>{sim.running ? t('Running…') : t('Run')}</button>}
      />
      <SimStatus sim={sim} />
      {result && !sim.running && (
        <>
          <p style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 8 }}>{t('{n} closed records simulated.', { n: result.total_closed })}</p>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          {['relative_strength', 'volume_profile'].map((compName) => {
            const rows = result.components[compName] || []
            return (
              <div key={compName} style={{ marginBottom: 12 }}>
                <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>{compName}</div>
                {rows.length === 0 ? (
                  <p style={{ fontSize: 12, color: 'var(--text-faint)' }}>{t('No records carry this component.')}</p>
                ) : (
                  rows.map((row, i) => (
                    <ComponentRow key={i} row={row} minSamples={ranMinSamples} />
                  ))
                )}
              </div>
            )
          })}
          <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
            {t('onchain_bias has no historical equivalent and is simply absent from these records — everything else (regime, track, htf, frama, signal_confluence) is reconstructed point-in-time.')}
          </p>
        </>
      )}
    </div>
  )
}

// 🆕 SMC event study — web twin of Discord's !smcsim (research only, trades nothing).
const smcPp = (v) => (Number.isFinite(v) ? `${v > 0 ? '+' : ''}${(v * 100).toFixed(0)}pp` : '')

// A cell is highlighted only when the event clearly beats (or trails) the any-bar baseline on BOTH
// measures with enough samples — the same rule as the 💡 / 🔻 marks in Discord.
function smcTone(h) {
  if (h.n < 30 || !Number.isFinite(h.z_ret) || !Number.isFinite(h.z_win)) return null
  if (h.z_ret >= 3 && h.z_win >= 2) return 'var(--long)'
  if (h.z_ret <= -3 && h.z_win <= -2) return 'var(--short)'
  return null
}

function SmcResultTable({ r, horizons }) {
  const { t } = useI18n()
  const baseLine = (key) =>
    r.baseline[key]
      .filter((b) => Number.isFinite(b.avg_ret))
      .map((b) => `${b.h}b ${pctSigned(b.avg_ret)} w${pctWhole(b.win_rate)}`)
      .join(' · ')
  return (
    <div style={{ marginBottom: 18 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
        {r.ticker} {r.tf} <span style={{ fontWeight: 400, color: 'var(--text-faint)' }}>{t('— {n} closed bars', { n: r.bars })}</span>
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-faint)', marginBottom: 6 }}>
        <div>{t('baseline long (every bar): {line}', { line: baseLine('long') })}</div>
        <div>{t('baseline short (every bar): {line}', { line: baseLine('short') })}</div>
      </div>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>{t('Event')}</th>
              <th>{t('Side')}</th>
              <th>n</th>
              {horizons.map((h) => <th key={h}>{t('{h} bars', { h })}</th>)}
            </tr>
          </thead>
          <tbody>
            {r.events.map((ev) => (
              <tr key={ev.name} style={{ cursor: 'default' }} title={ev.meaning}>
                <td>{ev.name}</td>
                <td><span className={`tag ${ev.direction > 0 ? 'long' : 'short'}`}>{ev.direction > 0 ? t('long') : t('short')}</span></td>
                <td>
                  {ev.count}
                  {ev.count < 30 && <span style={{ color: 'var(--accent)' }} title={t('tiny sample')}> ⚠</span>}
                </td>
                {horizons.map((hz) => {
                  const h = ev.horizons.find((x) => x.h === hz)
                  if (!h || h.n === 0 || !Number.isFinite(h.d_ret)) return <td key={hz} style={{ color: 'var(--text-faint)' }}>—</td>
                  const tone = smcTone(h)
                  return (
                    <td key={hz} style={tone ? { color: tone } : undefined}>
                      Δ{pctSigned(h.d_ret)}{Number.isFinite(h.z_ret) ? ` (z${h.z_ret.toFixed(1)})` : ''}
                      <div style={{ fontSize: 11, opacity: 0.85 }}>
                        w{pctWhole(h.win_rate)} {smcPp(h.d_win)}
                      </div>
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function SmcSimSection({ pairs, timeframes }) {
  const { t } = useI18n()
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(6000)
  const sim = useSimJob('smcsim')
  const result = sim.job?.status === 'done' ? sim.job.result : null
  const ranBars = sim.job?.params?.num_bars ?? numBars

  const run = () => {
    const bad = barsError(numBars)
    if (bad) { sim.setError(bad); return }
    sim.start(() => api.runSmcSim({ ticker, tf, numBars }))
  }

  return (
    <div className="panel">
      <h3 className="panel-title">SMC event study</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', marginBottom: 12 }}>
        {t('Smart Money Concepts on real history: liquidity sweeps, break of structure (BOS), change of character (CHoCH), fair value gaps (FVG) and "sweep then structure shift". After each event: does price do better than after an arbitrary bar? Research only — nothing here trades, filters or changes a setting, and nothing is written to history. Fast (no backtest). Parameters are fixed on purpose.')}
      </p>
      <SimScopeFields
        pairs={pairs} timeframes={timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={<button onClick={run} disabled={sim.running}>{sim.running ? t('Running…') : t('Run')}</button>}
      />
      <SimStatus sim={sim} />
      {result && !sim.running && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          {(result.skipped || []).map((s, i) => (
            <p key={i} style={{ fontSize: 11, color: 'var(--accent)', marginBottom: 6 }}>
              {t('⚠️ {ticker} {tf} skipped: {reason}', { ticker: s.ticker, tf: s.tf, reason: s.reason })}
            </p>
          ))}
          <p style={{ fontSize: 11, color: 'var(--text-faint)', marginBottom: 10 }}>
            {t('Fixed params: swing {swing}/{swing} bars · sweep→shift window {mss} bars · FVG ≥ {fvg} ATR · entry = {entry} · race = ±{race} ATR · same event closer than {cooldown} bars is dropped. Hover an event name for its definition.', {
              swing: result.params.swing_n, mss: result.params.mss_window, fvg: result.params.fvg_min_atr,
              entry: result.params.entry, race: result.params.race_atr, cooldown: result.params.event_cooldown,
            })}
          </p>
          {result.results.map((r) => (
            <SmcResultTable key={`${r.ticker}-${r.tf}`} r={r} horizons={result.params.horizons} />
          ))}
          <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
            {t('Δ = average directional return after the event minus the average for ANY bar in the same direction and horizon (a rising market is not an edge). w = win rate of the ±1 ATR race, with the difference vs baseline in points. Green / red = at least 3 se on return AND 2 z on win rate (n ≥ 30) better / worse than baseline. A full run tests ~170 numbers, so a lone green cell can be luck — look for the SAME event holding on both pairs and both timeframes. Events within 24 bars of each other share price moves, so the real sample is smaller than n.')}
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
      <SmcSimSection pairs={pairs} timeframes={timeframes} />
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

// =====================================================================
// 🔎  FIND NEW EXCHANGE WALLETS — web twin of Discord's !ethtrace.
// On demand only (≈23 Etherscan calls, 15–30 s), never polled.
// =====================================================================
const TRACE_EXCHANGES = ['Binance', 'Coinbase', 'Kraken', 'OKX']  // keep in sync with EXCHANGE_ETH_ADDRESSES (onchain.py)

function fmtAgeLong(ts, t) {
  if (!ts) return t('never')
  const s = Math.max(0, Date.now() / 1000 - ts)
  if (s < 3600) return t('{m}m ago', { m: Math.floor(s / 60) })
  if (s < 86400) return t('{h}h ago', { h: Math.floor(s / 3600) })
  return t('{d}d ago', { d: Math.floor(s / 86400) })
}

// status / verdict → tag look (stale = remove candidate, add = worth tracking)
const WALLET_TAG = { active: 'long', conduit: 'flat', holding: 'flat', quiet: 'flat', stale: 'short', unknown: 'flat' }
const CANDIDATE_TAG = { add: 'long', review: 'flat', skip: 'flat' }

// Result of the "Verify wallets" button: health of the tracked set + classified
// new candidates. Verdicts are heuristics — hence the Etherscan link on every row.
function WalletVerifyResult({ res, copy, copied }) {
  const { t, locale } = useI18n()
  const s = res.summary || {}
  const [acts, setActs] = useState({})     // address -> { state: 'busy' | 'done' | 'error', kind: 'add' | 'remove', msg? }
  const [armed, setArmed] = useState(null) // address waiting for its confirming second click

  const act = async (kind, address, label) => {
    setArmed(null)
    setActs((a) => ({ ...a, [address]: { state: 'busy', kind } }))
    try {
      if (kind === 'add') await api.addOnchainWallet(res.exchange, address, label)
      else await api.removeOnchainWallet(res.exchange, address)
      setActs((a) => ({ ...a, [address]: { state: 'done', kind } }))
    } catch (e) {
      setActs((a) => ({ ...a, [address]: { state: 'error', kind, msg: e.message } }))
    }
  }

  // Add / Remove / Undo of one row. Two clicks on purpose: this changes the inputs of the
  // flow signal, and window.confirm is unreliable inside an Android WebView.
  const rowActions = (address, label, kind, note) => {
    const a = acts[address]
    if (a?.state === 'busy') return <span style={{ fontSize: 12, color: 'var(--text-faint)' }}>{t('working…')}</span>
    if (a?.state === 'done') {
      return (
        <>
          <span style={{ fontSize: 12, color: 'var(--long)' }}>
            {a.kind === 'remove' ? t('removed ✓ (applies on the next on-chain refresh)') : t('added ✓ (applies on the next on-chain refresh)')}
          </span>
          <button className="btn" onClick={() => act(a.kind === 'remove' ? 'add' : 'remove', address, label)}>{t('Undo')}</button>
        </>
      )
    }
    return (
      <>
        {armed === address ? (
          <>
            <button className="btn" onClick={() => act(kind, address, label)}
              style={kind === 'remove' ? { borderColor: 'var(--short)', color: 'var(--short)' } : undefined}>
              {kind === 'remove'
                ? (note ? t('Confirm remove ({note})', { note }) : t('Confirm remove'))
                : (note ? t('Confirm add ({note})', { note }) : t('Confirm add'))}
            </button>
            <button className="btn" onClick={() => setArmed(null)}>{t('Cancel')}</button>
          </>
        ) : (
          <button className="btn" onClick={() => setArmed(address)}>{kind === 'remove' ? t('Remove') : t('Add')}</button>
        )}
        {a?.state === 'error' && <span style={{ fontSize: 12, color: 'var(--short)' }}>{a.msg}</span>}
      </>
    )
  }
  const removedDone = (addr) => acts[addr]?.state === 'done' && acts[addr]?.kind === 'remove'
  const attention = res.wallets.filter((w) => w.status === 'stale' || w.status === 'unknown')
  const healthy = res.wallets.filter((w) => w.status !== 'stale' && w.status !== 'unknown')
  const rowStyle = { padding: '8px 0', borderTop: '1px solid var(--border-soft)', fontSize: 12 }
  const walletRow = (w) => (
    <div key={w.address} style={{ ...rowStyle, opacity: removedDone(w.address) ? 0.55 : 1 }}>
      <div>
        <span className={`tag ${WALLET_TAG[w.status] || 'flat'}`}>{t(w.status)}</span>{' '}
        <b>{w.label}</b>
        {w.custom && <span className="tag flat" style={{ marginLeft: 6 }} title={t('added from this page, not from the defaults in onchain.py')}>{t('custom')}</span>}
      </div>
      <div className="mono" style={{ wordBreak: 'break-all', marginTop: 2 }}>{w.address}</div>
      <div style={{ color: 'var(--text-faint)', marginTop: 2 }}>
        {w.balance_eth == null ? t('balance n/a') : `${Number(w.balance_eth).toLocaleString(locale)} ETH`}
        {' · '}{t('last tx {age}', { age: fmtAgeLong(w.last_ts, t) })}{w.reason ? ` · ${w.reason}` : ''}
      </div>
      <div style={{ marginTop: 4, display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        <a href={`https://etherscan.io/address/${w.address}`} target="_blank" rel="noreferrer">Etherscan ↗</a>
        <button className="btn" onClick={() => copy(w.address)}>{copied === w.address ? t('Copied') : t('Copy')}</button>
        {rowActions(w.address, w.label, 'remove',
          w.balance_eth == null ? null : `${Number(w.balance_eth).toLocaleString(locale)} ETH`)}
      </div>
    </div>
  )
  const candRow = (c) => (
    <div key={c.address} style={rowStyle}>
      <div>
        <span className={`tag ${CANDIDATE_TAG[c.verdict] || 'flat'}`}
          style={c.verdict === 'review' ? { color: 'var(--accent)' } : undefined}>{t(c.verdict)}</span>
      </div>
      <div className="mono" style={{ wordBreak: 'break-all', marginTop: 2 }}>{c.address}</div>
      <div style={{ color: 'var(--text-faint)', marginTop: 2 }}>
        {trNodes(t, 'out {out} / in {inn} ETH · {txs} tx · last {age} · via {via}', {
          out: <b>{Math.round(c.out_eth).toLocaleString(locale)}</b>, inn: <b>{Math.round(c.in_eth).toLocaleString(locale)}</b>,
          txs: c.txs, age: fmtAgeLong(c.last_ts, t), via: c.via.join(', '),
        })}
      </div>
      <div style={{ marginTop: 2 }}>{c.reason}</div>
      <div style={{ marginTop: 4, display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        <a href={`https://etherscan.io/address/${c.address}`} target="_blank" rel="noreferrer">Etherscan ↗</a>
        <button className="btn" onClick={() => copy(c.address)}>{copied === c.address ? t('Copied') : t('Copy')}</button>
        {c.verdict !== 'skip' && rowActions(c.address, undefined, 'add', null)}
      </div>
    </div>
  )
  const subTitle = { fontSize: 12, fontWeight: 600, margin: '12px 0 2px' }

  return (
    <div style={{ marginTop: 12 }}>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', margin: '0 0 6px' }}>
        {t('{exchange}: {n} tracked wallets', { exchange: res.exchange, n: res.wallets.length })}
        {['active', 'conduit', 'holding', 'quiet', 'stale', 'unknown'].filter((k) => s[k]).map((k) => ` · ${s[k]} ${t(k)}`)}
        {res.failed?.length > 0 && ` · ${t('{n} fetch failed: {list}', { n: res.failed.length, list: res.failed.join(', ') })}`}
        {'. '}{t('Verdicts are heuristics from on-chain activity, confirm on Etherscan before changing the list.')}
        {' '}{t('Add / Remove are saved on the server and apply from the next on-chain refresh (Undo reverts them).')}
      </p>

      <div style={subTitle}>{t('Remove? / re-run ({n})', { n: attention.length })}</div>
      {attention.length === 0 && <p style={{ fontSize: 13, margin: '4px 0' }}>{t('Every tracked wallet looks alive.')}</p>}
      {attention.map(walletRow)}

      <div style={subTitle}>{t('New candidates ≥ {min} ETH ({total})', { min: Number(res.min_eth).toLocaleString(locale), total: res.total_candidates })}</div>
      {res.candidates.length === 0 && (
        <p style={{ fontSize: 13, margin: '4px 0' }}>{t('Nothing found, try a lower threshold (e.g. 200).')}</p>
      )}
      {res.candidates.map(candRow)}
      {res.total_candidates > res.candidates.length && (
        <p style={{ fontSize: 12, color: 'var(--text-faint)' }}>
          {t('Showing top {n} of {total}.', { n: res.candidates.length, total: res.total_candidates })}
        </p>
      )}
      {res.snippet && (
        <div style={{ marginTop: 8 }}>
          <div style={subTitle}>{t('Or make the "add" ones permanent defaults: paste into {target}', { target: `EXCHANGE_ETH_ADDRESSES["${res.exchange}"]` })}</div>
          <pre className="mono" style={{ fontSize: 11, overflowX: 'auto', margin: '4px 0' }}>{res.snippet}</pre>
          <button className="btn" onClick={() => copy(res.snippet)}>{copied === res.snippet ? t('Copied') : t('Copy snippet')}</button>
        </div>
      )}

      <details style={{ marginTop: 12 }}>
        <summary style={{ fontSize: 12, cursor: 'pointer' }}>{t('Healthy wallets ({n})', { n: healthy.length })}</summary>
        {healthy.map(walletRow)}
      </details>
    </div>
  )
}

function WalletTracePanel() {
  const { t, locale } = useI18n()
  const [exchange, setExchange] = useState('Binance')
  const [minEth, setMinEth] = useState('1000')
  const [busy, setBusy] = useState(false)   // false | 'trace' | 'verify'
  const [kind, setKind] = useState('trace') // which result is shown
  const [res, setRes] = useState(null)
  const [vres, setVres] = useState(null)
  const [err, setErr] = useState(null)
  const [copied, setCopied] = useState(null)
  // Manual add by address: Trace/Verify only surface counterparties of wallets that are
  // already tracked, so an exchange whose tracked wallets all went dead (Coinbase) never
  // yields a candidate to press "Add" on. Seed it by hand from an Etherscan label page.
  const [manAddr, setManAddr] = useState('')
  const [manLabel, setManLabel] = useState('')
  const [manState, setManState] = useState(null)   // null | 'busy' | { ok, msg }

  const addManual = async () => {
    const address = manAddr.trim()
    if (!/^0x[0-9a-fA-F]{40}$/.test(address)) { setManState({ ok: false, msg: t('Address must be 0x followed by 40 hex characters') }); return }
    setManState('busy')
    try {
      await api.addOnchainWallet(exchange, address, manLabel.trim() || undefined)
      setManState({ ok: true, msg: t('added to {exchange} ✓ — press Verify wallets to check it is active (applies on the next on-chain refresh)', { exchange }) })
      setManAddr('')
      setManLabel('')
    } catch (e) {
      setManState({ ok: false, msg: e.message })
    }
  }

  const run = async (mode) => {
    const v = parseFloat(minEth)
    if (!Number.isFinite(v) || v < 0) { setErr(t('Min ETH must be a non-negative number')); return }
    setBusy(mode)
    setErr(null)
    try {
      if (mode === 'verify') setVres(await api.verifyOnchain(exchange, v))
      else setRes(await api.traceOnchain(exchange, v))
      setKind(mode)
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  const copy = (addr) => {
    // clipboard can be missing/blocked in an Android WebView — the address is
    // also selectable text and has an Etherscan link, so a failure is harmless
    try {
      navigator.clipboard.writeText(addr).then(() => {
        setCopied(addr)
        setTimeout(() => setCopied(null), 1500)
      }).catch(() => {})
    } catch { /* ignore */ }
  }

  return (
    <div className="panel">
      <h3 className="panel-title">{t('Find new exchange wallets')}</h3>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', margin: '0 0 10px' }}>
        {t("If an exchange's flow sits at 0, its active wallets may have rotated. This reads the latest 100 transactions of every tracked wallet and lists big counterparties the bot doesn't track yet (takes 15–30 s, uses Etherscan).")}
        {' '}{trNodes(t, '{verify} goes further: it checks balance and last activity of every tracked wallet (keep / remove?) and sorts the new addresses into add / review / skip (25–45 s), with Add / Remove buttons so the list can be changed without editing {code}.', {
          verify: <b>{t('Verify wallets')}</b>, code: <code>onchain.py</code>,
        })}
      </p>
      <div className="field">
        <label>{t('Exchange')}</label>
        <select value={exchange} disabled={busy} onChange={(e) => setExchange(e.target.value)}>
          {TRACE_EXCHANGES.map((x) => <option key={x} value={x}>{x}</option>)}
        </select>
      </div>
      <div className="field">
        <label>{t('Min ETH (total per address)')}</label>
        <input type="number" min={0} step={100} value={minEth} disabled={busy}
          onChange={(e) => setMinEth(e.target.value)} />
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <button className="btn primary" disabled={!!busy} onClick={() => run('trace')}>
          {busy === 'trace' ? t('Tracing… (up to 30 s)') : t('Trace')}
        </button>
        <button className="btn" disabled={!!busy} onClick={() => run('verify')}>
          {busy === 'verify' ? t('Verifying… (up to 45 s)') : t('Verify wallets')}
        </button>
      </div>
      <div style={{ marginTop: 14, paddingTop: 10, borderTop: '1px solid var(--border-soft)' }}>
        <p style={{ fontSize: 12, color: 'var(--text-faint)', margin: '0 0 8px' }}>
          {trNodes(t, "{addw} to {ex} (e.g. from the exchange's Etherscan label page, sorted by recent activity). Use this when Trace finds nothing because every tracked wallet is dead.", {
            addw: <b>{t('Add a wallet by address')}</b>, ex: <b>{exchange}</b>,
          })}
        </p>
        <div className="field">
          <label>{t('Address')}</label>
          <input type="text" placeholder="0x…" value={manAddr} spellCheck={false} autoCapitalize="off"
            disabled={manState === 'busy'} onChange={(e) => setManAddr(e.target.value)} />
        </div>
        <div className="field">
          <label>{t('Label (optional)')}</label>
          <input type="text" placeholder={t('{exchange} hot wallet', { exchange })} value={manLabel} maxLength={40}
            disabled={manState === 'busy'} onChange={(e) => setManLabel(e.target.value)} />
        </div>
        <button className="btn" disabled={manState === 'busy' || !manAddr.trim()} onClick={addManual}>
          {manState === 'busy' ? t('Adding…') : t('Add wallet')}
        </button>
        {manState && manState !== 'busy' && (
          <p style={{ fontSize: 12, marginTop: 8, color: manState.ok ? 'var(--long)' : 'var(--short)' }}>{manState.msg}</p>
        )}
      </div>
      {err && <p style={{ fontSize: 12, color: 'var(--short)', marginTop: 10 }}>{err}</p>}
      {kind === 'verify' && vres && <WalletVerifyResult res={vres} copy={copy} copied={copied} />}
      {kind === 'trace' && res && (
        <div style={{ marginTop: 12 }}>
          <p style={{ fontSize: 12, color: 'var(--text-faint)', margin: '0 0 6px' }}>
            {t('{exchange}: checked {n} wallets', { exchange: res.exchange, n: res.wallets_checked })}
            {res.failed?.length > 0 && ` ${t('({n} failed: {list})', { n: res.failed.length, list: res.failed.join(', ') })}`}
            {' · '}≥ {Number(res.min_eth).toLocaleString(locale)} ETH · {trNodes(t, '{out} = sent by a tracked wallet to it, {inn} = sent by it to a tracked wallet', {
              out: <code>out</code>, inn: <code>in</code>,
            })}
          </p>
          {res.counterparties.length === 0 && (
            <p style={{ fontSize: 13 }}>{t('Nothing found — try a lower threshold (e.g. 200).')}</p>
          )}
          {res.counterparties.map((r) => (
            <div key={r.address} style={{ padding: '8px 0', borderTop: '1px solid var(--border-soft)', fontSize: 12 }}>
              <div className="mono" style={{ wordBreak: 'break-all' }}>{r.address}</div>
              <div style={{ color: 'var(--text-faint)', marginTop: 2 }}>
                {trNodes(t, 'out {out} / in {inn} ETH · {txs} tx · last {age} · via {via}', {
                  out: <b>{Math.round(r.out_eth).toLocaleString(locale)}</b>, inn: <b>{Math.round(r.in_eth).toLocaleString(locale)}</b>,
                  txs: r.txs, age: fmtAge(r.last_ts, t), via: r.via.join(', '),
                })}
              </div>
              <div style={{ marginTop: 4, display: 'flex', gap: 10, alignItems: 'center' }}>
                <a href={`https://etherscan.io/address/${r.address}`} target="_blank" rel="noreferrer">Etherscan ↗</a>
                <button className="btn" onClick={() => copy(r.address)}>{copied === r.address ? t('Copied') : t('Copy')}</button>
              </div>
            </div>
          ))}
          {res.total_matches > res.counterparties.length && (
            <p style={{ fontSize: 12, color: 'var(--text-faint)' }}>
              {t('Showing top {n} of {total}.', { n: res.counterparties.length, total: res.total_matches })}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

export default function InsightsPanel({ lastEvent, pairs, timeframes }) {
  const { t } = useI18n()
  const [view, setView] = useState('bias')

  return (
    <div>
      <div className="seg" style={{ marginBottom: 14 }}>
        {VIEWS.map((v) => (
          <button key={v.id} className={view === v.id ? 'active' : ''} onClick={() => setView(v.id)}>
            {t(v.label)}
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
