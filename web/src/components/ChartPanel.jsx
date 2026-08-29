import React, { useEffect, useRef, useState, useCallback, useMemo } from 'react'
import { createChart, ColorType, LineStyle } from 'lightweight-charts'
import { api } from '../api'

const TIMEFRAMES = ['1h', '4h']

const DEFAULT_COLORS = {
  frama: '#e8a33d',
  bb: '#7c8797',
  support: '#45d0a5',
  resistance: '#f2637a',
  mfi_line: '#8b93ff',
  mfi_overbought: '#f2637a',
  mfi_oversold: '#45d0a5',
  candle_up: '#45d0a5',
  candle_down: '#f2637a',
  tp_line: '#45d0a5',
  sl_line: '#f2637a',
  signal_long: '#45d0a5',
  signal_short: '#f2637a',
}

function hexToRgba(hex, alpha) {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || '')
  if (!m) return hex
  const [r, g, b] = [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)]
  return `rgba(${r},${g},${b},${alpha})`
}

function toLineData(times, values) {
  const out = []
  for (let i = 0; i < times.length; i++) {
    if (values[i] !== null && values[i] !== undefined) {
      out.push({ time: times[i], value: values[i] })
    }
  }
  return out
}

export default function ChartPanel({ pairs, lastEvent, colors, ticker, tf, onTickerChange, onTfChange, onLoadingChange }) {
  const containerRef = useRef(null)
  const chartRef = useRef(null)
  const seriesRef = useRef({})
  const lastSelectionKeyRef = useRef(null) // 🆕 only changes on ticker/tf/track change
  const [track, setTrack] = useState('a')
  const [barsLimit, setBarsLimit] = useState(100)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const requestIdRef = useRef(0) // 🆕 to ignore stale responses (race condition fix)

  const C = useMemo(() => ({ ...DEFAULT_COLORS, ...(colors || {}) }), [colors])

  const load = useCallback(() => {
    if (!ticker) return
    // 🆕 FIX: when quickly switching pairs (BTC→ETH→SOL), several requests
    // go out almost simultaneously; due to network jitter, the EARLIER
    // request (e.g. BTC) could respond LATER than a more recent one (SOL) —
    // BTC's .then() would overwrite the already-shown, correct SOL data with
    // stale BTC data. Tag each request with a number and only apply the
    // most recent one.
    const myRequestId = ++requestIdRef.current
    // 🆕 FIX: the loading indicator now lives in the top bar (next to LIVE),
    // not as a separate block right here — it used to be inserted/removed
    // in the document flow above the chart and shifted the chart itself up
    // and down on every update.
    onLoadingChange?.(true)
    api
      .getChart(ticker, tf, track, barsLimit)
      .then((d) => {
        if (myRequestId !== requestIdRef.current) return // stale, ignore
        setData(d)
        setError(null)
      })
      .catch((e) => {
        if (myRequestId !== requestIdRef.current) return
        setError(e.message)
      })
      .finally(() => {
        if (myRequestId === requestIdRef.current) onLoadingChange?.(false)
      })
  }, [ticker, tf, track, barsLimit, onLoadingChange])

  useEffect(() => {
    load()
    return () => onLoadingChange?.(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load])

  // scanner tick / new signal for this same pair/tf — pull fresh data
  useEffect(() => {
    if (!lastEvent || !ticker) return
    if (lastEvent.type === 'scan_tick') load()
    if (lastEvent.type === 'signal' && lastEvent.ticker === ticker && lastEvent.tf === tf) load()
    // colors/indicators changed in Settings — reread data (MFI thresholds may have changed)
    if (lastEvent.type === 'config_changed') load()
  }, [lastEvent, ticker, tf, load])

  // init the chart once
  useEffect(() => {
    if (!containerRef.current || chartRef.current) return
    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#7c8797',
        fontFamily: "'IBM Plex Mono', monospace",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: '#1b222b' },
        horzLines: { color: '#1b222b' },
      },
      rightPriceScale: { borderColor: '#232c3a' },
      timeScale: { borderColor: '#232c3a', timeVisible: true, rightOffset: 6 },
      crosshair: { mode: 0 },
      height: 640,
    })
    chartRef.current = chart

    const candle = chart.addCandlestickSeries({
      upColor: C.candle_up,
      downColor: C.candle_down,
      borderVisible: false,
      wickUpColor: C.candle_up,
      wickDownColor: C.candle_down,
      priceScaleId: 'right',
    })
    candle.priceScale().applyOptions({ scaleMargins: { top: 0.04, bottom: 0.36 } })

    const framaMid = chart.addLineSeries({
      color: C.frama,
      lineWidth: 2,
      priceScaleId: 'right',
      priceLineVisible: false,
      lastValueVisible: false,
    })
    const framaUpper = chart.addLineSeries({
      color: hexToRgba(C.frama, 0.45),
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceScaleId: 'right',
      priceLineVisible: false,
      lastValueVisible: false,
    })
    const framaLower = chart.addLineSeries({
      color: hexToRgba(C.frama, 0.45),
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceScaleId: 'right',
      priceLineVisible: false,
      lastValueVisible: false,
    })
    const bbUpper = chart.addLineSeries({
      color: hexToRgba(C.bb, 0.5),
      lineWidth: 1,
      priceScaleId: 'right',
      priceLineVisible: false,
      lastValueVisible: false,
    })
    const bbLower = chart.addLineSeries({
      color: hexToRgba(C.bb, 0.5),
      lineWidth: 1,
      priceScaleId: 'right',
      priceLineVisible: false,
      lastValueVisible: false,
    })

    const volume = chart.addHistogramSeries({
      priceScaleId: 'volume',
      priceFormat: { type: 'volume' },
      color: '#232c3a',
      priceLineVisible: false,
      lastValueVisible: false,
      // 🆕 FIX: without this, the volume scale auto-scales to the actual
      // min/max of the visible bars — if the current window has noticeably
      // more green volume than red (or vice versa), zero drifts up/down
      // within the panel instead of staying centered. Force a symmetric
      // range [-maxAbs, +maxAbs] so zero is always pinned to the center,
      // regardless of any buy/sell skew in the visible area.
      autoscaleInfoProvider: (original) => {
        const res = original()
        if (res?.priceRange) {
          const maxAbs = Math.max(Math.abs(res.priceRange.minValue), Math.abs(res.priceRange.maxValue))
          return { ...res, priceRange: { minValue: -maxAbs, maxValue: maxAbs } }
        }
        return res
      },
    })
    // 🆕 volume and mfi share the same scale zone (merged pane) — volume in
    // the background, the MFI line on top, instead of two separate strips
    // stacked under each other
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.66, bottom: 0 } })

    const mfi = chart.addLineSeries({
      color: C.mfi_line,
      lineWidth: 1.5,
      priceScaleId: 'mfi',
      priceLineVisible: false,
      lastValueVisible: false,
    })
    mfi.priceScale().applyOptions({ scaleMargins: { top: 0.66, bottom: 0 }, visible: true })

    seriesRef.current = { candle, framaMid, framaUpper, framaLower, bbUpper, bbLower, volume, mfi, srLines: [], srZones: [], tradeLines: [], mfiLines: [] }

    const handleResize = () => {
      if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth })
    }
    window.addEventListener('resize', handleResize)
    handleResize()

    // 🆕 FIX (Android client): in the Android app, the WebView is wrapped in
    // SwipeRefreshLayout — a vertical swipe on the chart competed with
    // pull-to-refresh for the same gesture (sometimes panning the chart,
    // sometimes accidentally refreshing the page). We notify the native
    // side (see MainActivity.kt, ChartTouchBridge) when a finger is
    // actually on the chart, so it can disable SwipeRefreshLayout for that
    // duration. window.AndroidChartBridge only exists inside the Android
    // WebView — in a regular browser these calls are no-ops.
    const notifyChartTouch = (active) => {
      window.AndroidChartBridge?.setChartTouching?.(active)
    }
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
      notifyChartTouch(false) // in case of unmounting right in the middle of a gesture
      chart.remove()
      chartRef.current = null
      seriesRef.current = {}
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // colors changed in Settings — recolor already-created series without recreating the chart
  useEffect(() => {
    const s = seriesRef.current
    if (!s.candle) return
    s.candle.applyOptions({
      upColor: C.candle_up, downColor: C.candle_down,
      wickUpColor: C.candle_up, wickDownColor: C.candle_down,
    })
    s.framaMid.applyOptions({ color: C.frama })
    s.framaUpper.applyOptions({ color: hexToRgba(C.frama, 0.45) })
    s.framaLower.applyOptions({ color: hexToRgba(C.frama, 0.45) })
    s.bbUpper.applyOptions({ color: hexToRgba(C.bb, 0.5) })
    s.bbLower.applyOptions({ color: hexToRgba(C.bb, 0.5) })
    s.mfi.applyOptions({ color: C.mfi_line })
  }, [C])

  // populate data when it updates
  useEffect(() => {
    if (!data || !chartRef.current) return
    const s = seriesRef.current
    const times = data.candles.map((c) => c.time)

    // 🆕 remember the current visible range BEFORE updating data — otherwise
    // every scan_tick/new signal jumps the chart to fitContent(), throwing
    // off whatever scroll/zoom position the user had set
    const selectionKey = `${ticker}-${tf}-${track}-${barsLimit}`
    const isNewSelection = lastSelectionKeyRef.current !== selectionKey
    const savedRange = isNewSelection ? null : chartRef.current.timeScale().getVisibleLogicalRange()

    // 🆕 FIX: if the user manually dragged/zoomed a price axis (the right
    // price scale, volume, or MFI), lightweight-charts switches that scale
    // into manual mode (autoScale: false) and stops adjusting the range to
    // new data. On a ticker/tf change, setData() plugs in another
    // instrument's prices, but the scale stays stuck on the previous pair's
    // range — e.g. BTC (~$65k) → DOGE (~$0.08), and the candles end up way
    // outside the visible area. fitContent() below only fixes the time
    // axis, not the price one — so we need to explicitly restore autoScale
    // on a new pair.
    if (isNewSelection) {
      s.candle.priceScale().applyOptions({ autoScale: true })
      s.volume.priceScale().applyOptions({ autoScale: true })
      s.mfi.priceScale().applyOptions({ autoScale: true })
    }

    s.candle.setData(data.candles)
    s.framaMid.setData(toLineData(times, data.frama))
    s.framaUpper.setData(toLineData(times, data.frama_upper))
    s.framaLower.setData(toLineData(times, data.frama_lower))
    s.bbUpper.setData(toLineData(times, data.bb_upper))
    s.bbLower.setData(toLineData(times, data.bb_lower))
    s.volume.setData(
      data.candles.map((c) => ({
        time: c.time,
        value: c.close >= c.open ? c.volume : -c.volume,
        color: c.close >= c.open ? hexToRgba(C.candle_up, 0.65) : hexToRgba(C.candle_down, 0.65),
      })),
    )
    s.mfi.setData(toLineData(times, data.mfi))

    s.mfiLines?.forEach((line) => {
      try { s.mfi.removePriceLine(line) } catch (_) {}
    })
    s.mfiLines = []
    if (data.mfi_overbought != null) {
      s.mfiLines.push(
        s.mfi.createPriceLine({
          price: data.mfi_overbought, color: hexToRgba(C.mfi_overbought, 0.6), lineWidth: 1,
          lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'OB',
        }),
      )
    }
    if (data.mfi_oversold != null) {
      s.mfiLines.push(
        s.mfi.createPriceLine({
          price: data.mfi_oversold, color: hexToRgba(C.mfi_oversold, 0.6), lineWidth: 1,
          lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'OS',
        }),
      )
    }

    // 🆕 S/R are now semi-transparent zones (like supply/demand on
    // TradingView), not thin dashed lines. A Baseline series fills the area
    // between value and baseValue with topFillColor — meaning between
    // level±halfWidth we get an even horizontal band across the full chart width.
    ;[...s.srLines, ...s.srZones, ...s.tradeLines].forEach((line) => {
      try { s.candle.removePriceLine(line) } catch (_) {}
      try { chartRef.current.removeSeries(line) } catch (_) {}
    })
    s.srLines = []
    s.srZones = []
    s.tradeLines = []

    const srZone = (level, color) => {
      const halfWidth = level * 0.0012 // ~0.12% each side — tuned below
      const zone = chartRef.current.addBaselineSeries({
        baseValue: { type: 'price', price: level - halfWidth },
        topLineColor: 'transparent',
        topFillColor1: color,
        topFillColor2: color,
        bottomLineColor: 'transparent',
        bottomFillColor1: 'transparent',
        bottomFillColor2: 'transparent',
        lineWidth: 1,
        priceScaleId: 'right',
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      })
      zone.setData(times.map((t) => ({ time: t, value: level + halfWidth })))
      s.srZones.push(zone)
    }

    data.support?.forEach((level) => srZone(level, hexToRgba(C.support, 0.16)))
    data.resistance?.forEach((level) => srZone(level, hexToRgba(C.resistance, 0.16)))

    if (data.active_trade) {
      const t = data.active_trade
      if (t.entry) s.tradeLines.push(s.candle.createPriceLine({ price: t.entry, color: '#dde3ec', lineWidth: 1, lineStyle: LineStyle.Solid, title: 'Entry' }))
      if (t.tp1 && t.tp1 !== t.tp) {
        s.tradeLines.push(s.candle.createPriceLine({
          price: t.tp1, color: hexToRgba(C.tp_line, 0.7), lineWidth: 1,
          lineStyle: LineStyle.Dashed, title: t.tp1_hit ? 'TP1 ✓' : 'TP1',
        }))
      }
      if (t.tp) s.tradeLines.push(s.candle.createPriceLine({ price: t.tp, color: C.tp_line, lineWidth: 2, lineStyle: LineStyle.Solid, title: 'TP2' }))
      if (t.sl) {
        s.tradeLines.push(s.candle.createPriceLine({
          price: t.sl, color: C.sl_line, lineWidth: 2, lineStyle: LineStyle.Solid,
          title: t.tp1_hit ? 'SL (BE)' : 'SL',
        }))
      }

      // 🆕 signal marker on the entry bar — signal_bar_time used to always be
      // null because of a field-name bug in chart_data.py (entry_time_ms vs bar_opened_time)
      if (t.signal_bar_time) {
        const isLong = t.side === 'long'
        s.candle.setMarkers([{
          time: t.signal_bar_time,
          position: isLong ? 'belowBar' : 'aboveBar',
          color: isLong ? C.signal_long : C.signal_short,
          shape: isLong ? 'arrowUp' : 'arrowDown',
          text: isLong ? 'Long' : 'Short',
        }])
      } else {
        s.candle.setMarkers([])
      }
    } else {
      s.candle.setMarkers([])
    }

    // 🆕 restore the position instead of resetting to fitContent on every update
    if (isNewSelection || !savedRange) {
      chartRef.current.timeScale().fitContent()
    } else {
      chartRef.current.timeScale().setVisibleLogicalRange(savedRange)
    }
    lastSelectionKeyRef.current = selectionKey
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data])

  return (
    <div>
      <div className="chart-toolbar">
        <div className="seg">
          {(pairs || []).map((p) => (
            <button key={p} className={ticker === p ? 'active' : ''} onClick={() => onTickerChange?.(p)}>
              {p}
            </button>
          ))}
        </div>
        <div className="seg">
          {TIMEFRAMES.map((t) => (
            <button key={t} className={tf === t ? 'active' : ''} onClick={() => onTfChange?.(t)}>
              {t}
            </button>
          ))}
        </div>
        <div className="seg">
          {['a', 'u'].map((t) => (
            <button key={t} className={track === t ? 'active' : ''} onClick={() => setTrack(t)}>
              {t === 'a' ? 'Andean' : 'UT Bot'}
            </button>
          ))}
        </div>
        <div className="seg">
          {[100, 200, 300, 500].map((n) => (
            <button key={n} className={barsLimit === n ? 'active' : ''} onClick={() => setBarsLimit(n)}>
              {n} bars
            </button>
          ))}
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="chart-legend">
        <span><span className="legend-dot" style={{ background: C.frama }} />FRAMA</span>
        <span><span className="legend-dot" style={{ background: C.bb }} />Bollinger</span>
        <span><span className="legend-dot" style={{ background: C.support }} />Support</span>
        <span><span className="legend-dot" style={{ background: C.resistance }} />Resistance</span>
        <span><span className="legend-dot" style={{ background: C.tp_line }} />TP</span>
        <span><span className="legend-dot" style={{ background: C.sl_line }} />SL</span>
        <span><span className="legend-dot" style={{ background: hexToRgba(C.candle_up, 0.5) }} />Delta Volume</span>
        <span><span className="legend-dot" style={{ background: C.mfi_line }} />MFI</span>
        <span><span className="legend-dot" style={{ background: C.mfi_overbought }} />MFI overbought</span>
        <span><span className="legend-dot" style={{ background: C.mfi_oversold }} />MFI oversold</span>
      </div>

      <div className="chart-wrap panel" style={{ padding: 8 }}>
        <div ref={containerRef} style={{ width: '100%' }} />
      </div>
    </div>
  )
}
