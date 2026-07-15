import React, { useEffect, useRef, useState, useCallback } from 'react'
import { createChart, ColorType, LineStyle } from 'lightweight-charts'
import { api } from '../api'

const TIMEFRAMES = ['1h', '4h']

function toLineData(times, values) {
  const out = []
  for (let i = 0; i < times.length; i++) {
    if (values[i] !== null && values[i] !== undefined) {
      out.push({ time: times[i], value: values[i] })
    }
  }
  return out
}

export default function ChartPanel({ pairs, lastEvent }) {
  const containerRef = useRef(null)
  const chartRef = useRef(null)
  const seriesRef = useRef({})
  const [ticker, setTicker] = useState(null)
  const [tf, setTf] = useState('1h')
  const [track, setTrack] = useState('a')
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!ticker && pairs?.length) setTicker(pairs[0])
  }, [pairs, ticker])

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

    // ── Раскладка по высоте: цена сверху, объём и MFI — отдельными панелями снизу ──
    chart.priceScale('right').applyOptions({ scaleMargins: { top: 0.04, bottom: 0.42 } })
    chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.62, bottom: 0.28 } })
    chart.priceScale('mfi').applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } })

    const candle = chart.addCandlestickSeries({
      upColor: '#45d0a5',
      downColor: '#f2637a',
      borderVisible: false,
      wickUpColor: '#45d0a5',
      wickDownColor: '#f2637a',
      priceScaleId: 'right',
    })

    const framaMid = chart.addLineSeries({
      color: '#e8a33d',
      lineWidth: 2,
      priceScaleId: 'right',
      priceLineVisible: false,
      lastValueVisible: false,
    })
    const framaUpper = chart.addLineSeries({
      color: 'rgba(232,163,61,0.45)',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceScaleId: 'right',
      priceLineVisible: false,
      lastValueVisible: false,
    })
    const framaLower = chart.addLineSeries({
      color: 'rgba(232,163,61,0.45)',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceScaleId: 'right',
      priceLineVisible: false,
      lastValueVisible: false,
    })
    const bbUpper = chart.addLineSeries({
      color: 'rgba(124,135,151,0.5)',
      lineWidth: 1,
      priceScaleId: 'right',
      priceLineVisible: false,
      lastValueVisible: false,
    })
    const bbLower = chart.addLineSeries({
      color: 'rgba(124,135,151,0.5)',
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

    const mfi = chart.addLineSeries({
      color: '#8b93ff',
      lineWidth: 1,
      priceScaleId: 'mfi',
      priceLineVisible: false,
      lastValueVisible: false,
    })

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
  }, [])

  // заливка данных при их обновлении
  useEffect(() => {
    if (!data || !chartRef.current) return
    const s = seriesRef.current
    const times = data.candles.map((c) => c.time)

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
        color: c.close >= c.open ? 'rgba(69,208,165,0.5)' : 'rgba(242,99,122,0.5)',
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
          price: data.mfi_overbought, color: 'rgba(242,99,122,0.6)', lineWidth: 1,
          lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'OB',
        }),
      )
    }
    if (data.mfi_oversold != null) {
      s.mfiLines.push(
        s.mfi.createPriceLine({
          price: data.mfi_oversold, color: 'rgba(69,208,165,0.6)', lineWidth: 1,
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
          price: level, color: '#45d0a5', lineWidth: 1,
          lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: 'S',
        }),
      )
    })
    data.resistance?.forEach((level) => {
      s.srLines.push(
        s.candle.createPriceLine({
          price: level, color: '#f2637a', lineWidth: 1,
          lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: 'R',
        }),
      )
    })

    if (data.active_trade) {
      const t = data.active_trade
      if (t.entry) s.tradeLines.push(s.candle.createPriceLine({ price: t.entry, color: '#dde3ec', lineWidth: 1, lineStyle: LineStyle.Solid, title: 'Entry' }))
      if (t.tp) s.tradeLines.push(s.candle.createPriceLine({ price: t.tp, color: '#45d0a5', lineWidth: 2, lineStyle: LineStyle.Solid, title: 'TP' }))
      if (t.sl) s.tradeLines.push(s.candle.createPriceLine({ price: t.sl, color: '#f2637a', lineWidth: 2, lineStyle: LineStyle.Solid, title: 'SL' }))
    }

    chartRef.current.timeScale().fitContent()
  }, [data])

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
          {TIMEFRAMES.map((t) => (
            <button key={t} className={tf === t ? 'active' : ''} onClick={() => setTf(t)}>
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
        <span><span className="legend-dot" style={{ background: '#e8a33d' }} />FRAMA</span>
        <span><span className="legend-dot" style={{ background: '#7c8797' }} />Bollinger</span>
        <span><span className="legend-dot" style={{ background: '#45d0a5' }} />Support</span>
        <span><span className="legend-dot" style={{ background: '#f2637a' }} />Resistance</span>
        <span><span className="legend-dot" style={{ background: '#8b93ff' }} />MFI</span>
        <span><span className="legend-dot" style={{ background: '#f2637a' }} />MFI overbought</span>
        <span><span className="legend-dot" style={{ background: '#45d0a5' }} />MFI oversold</span>
      </div>

      <div className="chart-wrap panel" style={{ padding: 8 }}>
        <div ref={containerRef} style={{ width: '100%' }} />
      </div>
    </div>
  )
}
