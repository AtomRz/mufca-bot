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
  const lastSelectionKeyRef = useRef(null) // 🆕 меняется только при смене ticker/tf/track
  const [track, setTrack] = useState('a')
  const [barsLimit, setBarsLimit] = useState(200)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const requestIdRef = useRef(0) // 🆕 для игнорирования устаревших ответов (race condition fix)

  const C = useMemo(() => ({ ...DEFAULT_COLORS, ...(colors || {}) }), [colors])

  const load = useCallback(() => {
    if (!ticker) return
    // 🆕 FIX: при быстром переключении пар (BTC→ETH→SOL) несколько запросов летят
    // почти одновременно; из-за сетевой изменчивости более РАННИЙ запрос (например
    // BTC) мог ответить ПОЗЖЕ более позднего (SOL) — .then() из BTC переписал бы
    // уже показанные корректные данные SOL на устаревшие BTC. Помечаем каждый
    // запрос номером и применяем только самый свежий.
    const myRequestId = ++requestIdRef.current
    // 🆕 FIX: индикатор загрузки теперь живёт в топ-баре (рядом с LIVE), а не
    // отдельным блоком прямо тут — раньше он вставлялся/убирался в потоке
    // документа над графиком и на каждое обновление сдвигал сам график вверх-вниз.
    onLoadingChange?.(true)
    api
      .getChart(ticker, tf, track, barsLimit)
      .then((d) => {
        if (myRequestId !== requestIdRef.current) return // устарел, игнорируем
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
      // 🆕 FIX: без этого шкала объёма автомасштабируется по фактическому
      // мин/макс видимых баров — если в текущем окне зелёного объёма заметно
      // больше, чем красного (или наоборот), ноль съезжает вверх/вниз внутри
      // панели вместо того, чтобы оставаться по центру. Форсируем симметричный
      // диапазон [-maxAbs, +maxAbs], чтобы ноль был зафиксирован по центру
      // всегда, независимо от перекоса покупок/продаж в видимой области.
      autoscaleInfoProvider: (original) => {
        const res = original()
        if (res?.priceRange) {
          const maxAbs = Math.max(Math.abs(res.priceRange.minValue), Math.abs(res.priceRange.maxValue))
          return { ...res, priceRange: { minValue: -maxAbs, maxValue: maxAbs } }
        }
        return res
      },
    })
    // 🆕 volume и mfi делят одну и ту же зону шкалы (merged pane) — объём фоном,
    // MFI-линия поверх, вместо двух раздельных полос друг под другом
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

    return () => {
      window.removeEventListener('resize', handleResize)
      chart.remove()
      chartRef.current = null
      seriesRef.current = {}
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
    const selectionKey = `${ticker}-${tf}-${track}-${barsLimit}`
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

    // 🆕 S/R теперь полупрозрачные зоны (как supply/demand на TradingView), а не
    // тонкие пунктирные линии. Baseline-серия заливает область между value и
    // baseValue цветом topFillColor — то есть между level±halfWidth получаем
    // ровную горизонтальную полосу на всю ширину графика.
    ;[...s.srLines, ...s.srZones, ...s.tradeLines].forEach((line) => {
      try { s.candle.removePriceLine(line) } catch (_) {}
      try { chartRef.current.removeSeries(line) } catch (_) {}
    })
    s.srLines = []
    s.srZones = []
    s.tradeLines = []

    const srZone = (level, color) => {
      const halfWidth = level * 0.0012 // ~0.12% каждая сторона — подстраивается ниже
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

      // 🆕 маркер сигнала на баре входа — раньше signal_bar_time всегда был null
      // из-за бага с именем поля в chart_data.py (entry_time_ms vs bar_opened_time)
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
