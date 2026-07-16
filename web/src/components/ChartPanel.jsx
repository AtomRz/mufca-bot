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

export default function ChartPanel({ pairs, lastEvent, colors, ticker, tf, onTickerChange, onTfChange }) {
  const containerRef = useRef(null)
  const chartRef = useRef(null)
  const seriesRef = useRef({})
  const lastSelectionKeyRef = useRef(null) // 🆕 меняется только при смене ticker/tf/track
  const [track, setTrack] = useState('a')
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  const C = useMemo(() => ({ ...DEFAULT_COLORS, ...(colors || {}) }), [colors])

  const load = useCallback(() => {
    if (!ticker) return
    api
      .getChart(ticker, tf, track)
      .then((d) => {
        setData(d)
        setError(null)
      })
      .catch((e) => setError(e.message))
  }, [ticker, tf, track])

  useEffect(() => {
    load()
  }, [load])

  // тик сканера / новый сигнал по этой же паре/тф — подтягиваем свежие данные
  useEffect(() => {
    if (!lastEvent || !ticker) return
    if (lastEvent.type === 'scan_tick') load()
    if (lastEvent.type === 'signal' && lastEvent.ticker === ticker && lastEvent.tf === tf) load()
    // смена цветов/индикаторов в Settings — перечитать данные (пороги MFI могли измениться)
    if (lastEvent.type === 'config_changed') load()
  }, [lastEvent, ticker, tf, load])

  // init chart один раз
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
      timeScale: { borderColor: '#232c3a', timeVisible: true },
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
    candle.priceScale().applyOptions({ scaleMargins: { top: 0.04, bottom: 0.42 } })

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
    })
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.62, bottom: 0.28 } })

    const mfi = chart.addLineSeries({
      color: C.mfi_line,
      lineWidth: 1,
      priceScaleId: 'mfi',
      priceLineVisible: false,
      lastValueVisible: false,
    })
    mfi.priceScale().applyOptions({ scaleMargins: { top: 0.78, bottom: 0 }, visible: true })

    seriesRef.current = { candle, framaMid, framaUpper, framaLower, bbUpper, bbLower, volume, mfi, srLines: [], tradeLines: [], mfiLines: [] }

    const handleResize = () => {
      if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth })
    }
    window.addEventListener('resize', handleResize)
    handleResize()

    return () => {
      window.removeEventListener('resize', handleResize)
      chart.remove()
      chartRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // цвета поменяли в Settings — перекрашиваем уже созданные серии без пересоздания чарта
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

  // заливка данных при их обновлении
  useEffect(() => {
    if (!data || !chartRef.current) return
    const s = seriesRef.current
    const times = data.candles.map((c) => c.time)

    // 🆕 запоминаем текущий видимый диапазон ДО обновления данных — иначе
    // каждый scan_tick/новый сигнал прыгает график к fitContent(), сбивая
    // скролл/зум, который выставил пользователь
    const selectionKey = `${ticker}-${tf}-${track}`
    const isNewSelection = lastSelectionKeyRef.current !== selectionKey
    const savedRange = isNewSelection ? null : chartRef.current.timeScale().getVisibleLogicalRange()

    s.candle.setData(data.candles)
    s.framaMid.setData(toLineData(times, data.frama))
    s.framaUpper.setData(toLineData(times, data.frama_upper))
    s.framaLower.setData(toLineData(times, data.frama_lower))
    s.bbUpper.setData(toLineData(times, data.bb_upper))
    s.bbLower.setData(toLineData(times, data.bb_lower))
    s.volume.setData(
      data.candles.map((c) => ({
        time: c.time,
        value: c.volume,
        color: c.close >= c.open ? hexToRgba(C.candle_up, 0.5) : hexToRgba(C.candle_down, 0.5),
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

    // S/R уровни и entry/tp/sl — как horizontal price lines на свечной серии
    ;[...s.srLines, ...s.tradeLines].forEach((line) => {
      try { s.candle.removePriceLine(line) } catch (_) {}
    })
    s.srLines = []
    s.tradeLines = []

    data.support?.forEach((level) => {
      s.srLines.push(
        s.candle.createPriceLine({
          price: level, color: C.support, lineWidth: 1,
          lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: 'S',
        }),
      )
    })
    data.resistance?.forEach((level) => {
      s.srLines.push(
        s.candle.createPriceLine({
          price: level, color: C.resistance, lineWidth: 1,
          lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: 'R',
        }),
      )
    })

    if (data.active_trade) {
      const t = data.active_trade
      if (t.entry) s.tradeLines.push(s.candle.createPriceLine({ price: t.entry, color: '#dde3ec', lineWidth: 1, lineStyle: LineStyle.Solid, title: 'Entry' }))
      if (t.tp) s.tradeLines.push(s.candle.createPriceLine({ price: t.tp, color: C.support, lineWidth: 2, lineStyle: LineStyle.Solid, title: 'TP' }))
      if (t.sl) s.tradeLines.push(s.candle.createPriceLine({ price: t.sl, color: C.resistance, lineWidth: 2, lineStyle: LineStyle.Solid, title: 'SL' }))
    }

    // 🆕 восстанавливаем позицию вместо сброса к fitContent на каждое обновление
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
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="chart-legend">
        <span><span className="legend-dot" style={{ background: C.frama }} />FRAMA</span>
        <span><span className="legend-dot" style={{ background: C.bb }} />Bollinger</span>
        <span><span className="legend-dot" style={{ background: C.support }} />Support</span>
        <span><span className="legend-dot" style={{ background: C.resistance }} />Resistance</span>
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
