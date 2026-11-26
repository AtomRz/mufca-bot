import React, { useEffect, useRef, useState, useCallback, useMemo } from 'react'
import { createChart, ColorType, LineStyle } from 'lightweight-charts'
import { useTheme } from '../theme'
import { api } from '../api'
import { useI18n } from '../i18n'
import { CHART_THEME, DEFAULT_COLORS, hexToRgba } from './ChartPanel'

// TA tab: daily candles + automatic trend lines + the forming pattern (backend: trendlines.py).
// INFORMATIONAL ONLY - nothing here feeds the signal path.

const REFRESH_MIN_MS = 60 * 1000 // scan ticks refresh the forming candle, at most once a minute
const DAY_S = 86400
const VIEW_BARS = 150 // bars shown on a fresh selection (the user can still scroll back / zoom out)

function fmtPrice(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const a = Math.abs(v)
  return v.toLocaleString('en-US', { maximumFractionDigits: a >= 1000 ? 1 : a >= 100 ? 2 : a >= 1 ? 3 : 6 })
}
function fmtPct(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`
}
function fmtDate(sec) {
  return new Date(sec * 1000).toISOString().slice(0, 10) // UTC, matches the exchange's daily candle
}

export default function TaPanel({ pairs, lastEvent, colors, onLoadingChange }) {
  const { t } = useI18n()
  const { theme } = useTheme()
  const CT = CHART_THEME[theme] || CHART_THEME.dark
  const C = useMemo(() => ({ ...DEFAULT_COLORS, ...(colors || {}) }), [colors])

  const [ticker, setTicker] = useState(null)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [showSR, setShowSR] = useState(false)

  const containerRef = useRef(null)
  const chartRef = useRef(null)
  const seriesRef = useRef({ candle: null, extras: [], priceLines: [] })
  const requestIdRef = useRef(0) // ignore stale responses when switching pairs quickly
  const lastLoadRef = useRef(0)
  const lastSelectionRef = useRef(null)
  const themeRef = useRef(theme)
  themeRef.current = theme

  // default / repaired selection
  useEffect(() => {
    if (!pairs || !pairs.length) return
    if (!ticker || !pairs.includes(ticker)) setTicker(pairs[0])
  }, [pairs, ticker])

  const load = useCallback(() => {
    if (!ticker) return
    const myId = ++requestIdRef.current
    lastLoadRef.current = Date.now()
    onLoadingChange?.(true)
    api
      .getTa(ticker)
      .then((d) => {
        if (myId !== requestIdRef.current) return
        setData(d)
        setError(null)
      })
      .catch((e) => {
        if (myId !== requestIdRef.current) return
        setError(e.message)
      })
      .finally(() => {
        if (myId === requestIdRef.current) onLoadingChange?.(false)
      })
  }, [ticker, onLoadingChange])

  const loadRef = useRef(load)
  useEffect(() => { loadRef.current = load }, [load])

  // new pair: drop the old pair's data right away and fetch
  useEffect(() => {
    if (!ticker) return undefined
    setData(null)
    setError(null)
    loadRef.current()
    return () => onLoadingChange?.(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ticker])

  // scanner tick: refresh the still-forming daily candle (lines only change on a daily close)
  useEffect(() => {
    if (!lastEvent || !ticker || document.hidden) return
    if (lastEvent.type === 'scan_tick' && Date.now() - lastLoadRef.current >= REFRESH_MIN_MS) loadRef.current()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastEvent])

  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === 'visible' && Date.now() - lastLoadRef.current >= REFRESH_MIN_MS) loadRef.current()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [])

  // create the chart once
  useEffect(() => {
    if (!containerRef.current || chartRef.current) return undefined
    const ct = CHART_THEME[themeRef.current] || CHART_THEME.dark
    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: ct.text,
        fontFamily: "'IBM Plex Mono', monospace",
        fontSize: 11,
      },
      grid: { vertLines: { color: ct.grid }, horzLines: { color: ct.grid } },
      rightPriceScale: { borderColor: ct.border },
      timeScale: { borderColor: ct.border, timeVisible: false, rightOffset: 4 },
      crosshair: { mode: 0 },
      height: 520,
    })
    chartRef.current = chart
    seriesRef.current.candle = chart.addCandlestickSeries({
      upColor: C.candle_up,
      downColor: C.candle_down,
      borderVisible: false,
      wickUpColor: C.candle_up,
      wickDownColor: C.candle_down,
    })

    const handleResize = () => {
      if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth })
    }
    window.addEventListener('resize', handleResize)
    handleResize()

    // Android client: tell the native side when a finger is on the chart so pull-to-refresh
    // doesn't fight a vertical pan (same bridge ChartPanel uses; a no-op in a regular browser)
    const notifyChartTouch = (active) => { window.AndroidChartBridge?.setChartTouching?.(active) }
    const el = containerRef.current
    const onTouchStart = () => notifyChartTouch(true)
    const onTouchEnd = () => notifyChartTouch(false)
    el.addEventListener('touchstart', onTouchStart, { passive: true })
    el.addEventListener('touchend', onTouchEnd, { passive: true })
    el.addEventListener('touchcancel', onTouchEnd, { passive: true })

    return () => {
      window.removeEventListener('resize', handleResize)
      el.removeEventListener('touchstart', onTouchStart)
      el.removeEventListener('touchend', onTouchEnd)
      el.removeEventListener('touchcancel', onTouchEnd)
      notifyChartTouch(false)
      chart.remove()
      chartRef.current = null
      seriesRef.current = { candle: null, extras: [], priceLines: [] }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // theme changed: restyle the chart chrome in place
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    chart.applyOptions({
      layout: { textColor: CT.text },
      grid: { vertLines: { color: CT.grid }, horzLines: { color: CT.grid } },
      rightPriceScale: { borderColor: CT.border },
      timeScale: { borderColor: CT.border },
    })
  }, [CT])

  // draw candles + lines + touch markers + optional S/R whenever data / colours / theme / switch change
  useEffect(() => {
    const chart = chartRef.current
    const s = seriesRef.current
    if (!chart || !s.candle) return

    // read the user's pan / zoom BEFORE touching any series (removing the projection series shifts it)
    const savedRange = chart.timeScale().getVisibleLogicalRange()

    // clear what the previous draw added
    s.extras.forEach((x) => { try { chart.removeSeries(x) } catch (_) { /* already gone */ } })
    s.extras = []
    s.priceLines.forEach((l) => { try { s.candle.removePriceLine(l) } catch (_) { /* already gone */ } })
    s.priceLines = []

    s.candle.applyOptions({
      upColor: C.candle_up, downColor: C.candle_down, wickUpColor: C.candle_up, wickDownColor: C.candle_down,
    })

    if (!data || data.symbol !== ticker || !data.candles?.length) {
      s.candle.setData([])
      s.candle.setMarkers([])
      return
    }

    const candles = data.candles
    s.candle.setData(candles.map((c) => {
      const base = { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close }
      if (!c.forming) return base
      // the still-forming daily candle is drawn faded: lines never use it
      const col = hexToRgba(c.close >= c.open ? C.candle_up : C.candle_down, 0.45)
      return { ...base, color: col, wickColor: col, borderColor: col }
    }))

    const lastTime = candles[candles.length - 1].time
    let maxProjTime = lastTime
    const markers = []
    const candleTimes = new Set(candles.map((c) => c.time))

    for (const ln of data.lines || []) {
      const color = ln.side === 'resistance' ? C.resistance : C.support
      const solid = chart.addLineSeries({
        color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      })
      solid.setData([
        { time: ln.start.time, value: ln.start.price },
        { time: ln.end.time, value: ln.end.price },
      ])
      s.extras.push(solid)
      if (ln.projection && ln.projection.time > ln.end.time) {
        const proj = chart.addLineSeries({
          color: hexToRgba(color, 0.7), lineWidth: 1, lineStyle: LineStyle.Dashed,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        })
        proj.setData([
          { time: ln.end.time, value: ln.end.price },
          { time: ln.projection.time, value: ln.projection.price },
        ])
        s.extras.push(proj)
        maxProjTime = Math.max(maxProjTime, ln.projection.time)
      }
      for (const tp of ln.touch_points || []) {
        if (!candleTimes.has(tp.time)) continue
        markers.push({
          time: tp.time,
          position: ln.side === 'resistance' ? 'aboveBar' : 'belowBar',
          color,
          shape: 'circle',
          size: 1,
        })
      }
    }
    markers.sort((a, b) => a.time - b.time)
    s.candle.setMarkers(markers)

    if (showSR) {
      const add = (price, base) => {
        s.priceLines.push(s.candle.createPriceLine({
          price, color: hexToRgba(base, 0.55), lineWidth: 1, lineStyle: LineStyle.Dotted,
          axisLabelVisible: true, title: '',
        }))
      }
      ;(data.support || []).forEach((p) => add(p, C.support))
      ;(data.resistance || []).forEach((p) => add(p, C.resistance))
    }

    // new pair: frame the recent bars plus the projection; otherwise keep the user's pan / zoom
    const key = data.symbol
    if (lastSelectionRef.current !== key || !savedRange) {
      const n = candles.length
      const ahead = Math.max(0, Math.round((maxProjTime - lastTime) / DAY_S))
      chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - VIEW_BARS), to: n - 1 + ahead + 4 })
    } else {
      chart.timeScale().setVisibleLogicalRange(savedRange)
    }
    lastSelectionRef.current = key
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, C, theme, showSR])

  const pat = data && data.symbol === ticker ? data.pattern : null
  const lines = data && data.symbol === ticker ? data.lines || [] : []

  const patternLabel = (type) => ({
    ascending_triangle: t('Ascending triangle'),
    descending_triangle: t('Descending triangle'),
    symmetrical_triangle: t('Symmetrical triangle'),
    rising_wedge: t('Rising wedge'),
    falling_wedge: t('Falling wedge'),
    channel_up: t('Rising channel'),
    channel_down: t('Falling channel'),
    range: t('Range'),
    bull_flag: t('Bull flag'),
    bear_flag: t('Bear flag'),
  })[type] || type

  // geometry only: what the two lines look like, no market view
  const patternHint = (type) => ({
    ascending_triangle: t('Flat resistance with rising support'),
    descending_triangle: t('Flat support with falling resistance'),
    symmetrical_triangle: t('Falling resistance and rising support, converging'),
    rising_wedge: t('Both lines rising, converging'),
    falling_wedge: t('Both lines falling, converging'),
    channel_up: t('Parallel lines rising'),
    channel_down: t('Parallel lines falling'),
    range: t('Parallel horizontal lines'),
    bull_flag: t('Short falling channel after a sharp rise'),
    bear_flag: t('Short rising channel after a sharp drop'),
  })[type] || ''

  const lineStatus = (st) => ({
    intact: t('Intact'),
    broken: t('Broken'),
    reclaimed: t('Broken, back inside'),
  })[st] || st

  const sinceText = (n) => (n === 0 ? t('on the last closed bar') : t('{n} bars ago', { n }))

  const emptyReason = () => {
    switch (data?.reason) {
      case 'no_lines': return t('No valid trend lines on the daily chart right now')
      case 'no_pattern': return t('Trend lines found, but they do not form a pattern')
      case 'insufficient_data': return t('Not enough daily history')
      case 'bad_data': return t('The daily data is incomplete')
      default: return t('No forming pattern')
    }
  }

  return (
    <div>
      <div className="chart-toolbar">
        <div className="seg">
          {(pairs || []).map((p) => (
            <button key={p} className={ticker === p ? 'active' : ''} onClick={() => setTicker(p)}>
              {p}
            </button>
          ))}
        </div>
        <div className="seg">
          <button className="active" style={{ cursor: 'default' }}>1D</button>
        </div>
        <div className="seg">
          <button className={showSR ? 'active' : ''} onClick={() => setShowSR((v) => !v)}>
            {t('S/R levels')}
          </button>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="chart-wrap panel" style={{ padding: 8 }}>
        <div ref={containerRef} style={{ width: '100%' }} />
      </div>

      <div className="chart-legend">
        <span><span className="legend-dot" style={{ background: C.resistance }} />{t('Resistance')}</span>
        <span><span className="legend-dot" style={{ background: C.support }} />{t('Support')}</span>
        <span><span className="legend-dot" style={{ background: hexToRgba(C.resistance, 0.7) }} />{t('Projection')}</span>
        <span>● {t('Touches')}</span>
      </div>

      {data && data.symbol === ticker && (
        <div className="grid-2" style={{ marginTop: 14, alignItems: 'start' }}>
          <div className="panel">
            <h3 className="panel-title">{t('Pattern')}</h3>
            {pat ? (
              <>
                <div className="row">
                  <span className="row-label">{t('Type')}</span>
                  <span className="row-value">{patternLabel(pat.type)}</span>
                </div>
                <div className="row">
                  <span className="row-label">{t('Status')}</span>
                  <span className="row-value">
                    <span className={`tag ${pat.status === 'breakout_up' ? 'long' : pat.status === 'breakout_down' ? 'short' : 'flat'}`}>
                      {pat.status === 'breakout_up' ? t('Breakout up') : pat.status === 'breakout_down' ? t('Breakout down') : t('Forming')}
                    </span>
                    {pat.bars_since_break !== null && pat.bars_since_break !== undefined && (
                      <span style={{ marginLeft: 8, color: 'var(--text-dim)' }}>{sinceText(pat.bars_since_break)}</span>
                    )}
                  </span>
                </div>
                <div className="row">
                  <span className="row-label">{t('Duration')}</span>
                  <span className="row-value">{t('{n} bars', { n: pat.bars })} · {t('since {date}', { date: fmtDate(pat.start_time) })}</span>
                </div>
                <div className="row">
                  <span className="row-label">{t('Touches (resistance / support)')}</span>
                  <span className="row-value">{pat.resistance_touches} / {pat.support_touches}</span>
                </div>
                <div className="row">
                  <span className="row-label" title={t('0% = on the support line, 100% = on the resistance line')}>{t('Price position')}</span>
                  <span className="row-value">{pat.position === null || pat.position === undefined ? '—' : `${(pat.position * 100).toFixed(0)}%`}</span>
                </div>
                <div className="row">
                  <span className="row-label">{t('Width now')}</span>
                  <span className="row-value">{pat.width_pct.toFixed(2)}%</span>
                </div>
                {pat.bars_to_apex !== null && pat.bars_to_apex !== undefined && (
                  <div className="row">
                    <span className="row-label">{t('Apex')}</span>
                    <span className="row-value">
                      {t('in {n} bars', { n: pat.bars_to_apex })} · {t('{pct}% of the way', { pct: Math.round(pat.apex_progress * 100) })}
                    </span>
                  </div>
                )}
                <div style={{ marginTop: 10, fontSize: 12, color: 'var(--text-faint)' }}>{patternHint(pat.type)}</div>
              </>
            ) : (
              <div className="empty-state" style={{ padding: '14px 0' }}>{emptyReason()}</div>
            )}
          </div>

          <div className="panel">
            <h3 className="panel-title">{t('Trend lines')}</h3>
            {lines.length ? (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>{t('Line')}</th>
                      <th>{t('Touches')}</th>
                      <th>{t('Slope / bar')}</th>
                      <th>{t('Now')}</th>
                      <th>{t('Price vs line')}</th>
                      <th>{t('Status')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {lines.map((ln) => (
                      <tr key={ln.side} style={{ cursor: 'default' }}>
                        <td style={{ color: ln.side === 'resistance' ? C.resistance : C.support }}>
                          {ln.side === 'resistance' ? t('Resistance') : t('Support')}
                        </td>
                        <td>{ln.touches}</td>
                        <td>{fmtPct(ln.slope_pct_per_bar, 3)}</td>
                        <td>{fmtPrice(ln.end.price)}</td>
                        <td>{fmtPct(ln.dist_pct)} ({ln.dist_atr > 0 ? '+' : ''}{ln.dist_atr} ATR)</td>
                        <td>
                          {lineStatus(ln.status)}
                          {ln.bars_since_break !== null && ln.bars_since_break !== undefined && (
                            <span style={{ color: 'var(--text-dim)' }}> · {sinceText(ln.bars_since_break)}</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="empty-state" style={{ padding: '14px 0' }}>{emptyReason()}</div>
            )}
          </div>
        </div>
      )}

      {data && data.symbol === ticker && (
        <div style={{ marginTop: 12, fontSize: 12, color: 'var(--text-faint)' }}>
          {t('Informational only: does not affect signals. Lines use closed daily candles up to {date}; the faded last candle is still forming and is not used.', { date: fmtDate(data.as_of) })}
        </div>
      )}
    </div>
  )
}
