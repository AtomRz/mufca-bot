import React, { useEffect, useState, useCallback, useRef } from 'react'
import { api, connectLive, getAuthToken, clearAuthToken } from './api'
import StatusPanel from './components/StatusPanel'
import ChartPanel from './components/ChartPanel'
import HistoryPanel from './components/HistoryPanel'
import SettingsPanel from './components/SettingsPanel'
import LoginScreen from './components/LoginScreen'
import SignalLamps from './components/SignalLamps'

const TABS = [
  { id: 'status', label: 'Status' },
  { id: 'chart', label: 'Chart' },
  { id: 'history', label: 'History' },
]

const TREND_COLOR = { bullish: 'var(--long)', bearish: 'var(--short)', neutral: 'var(--text-dim)' }
const TREND_LABEL = { bullish: 'Bullish', bearish: 'Bearish', neutral: 'Neutral' }

export default function App() {
  const [authenticated, setAuthenticated] = useState(!!getAuthToken())
  const [tab, setTab] = useState('status')
  const [connStatus, setConnStatus] = useState('connecting')
  const [config, setConfig] = useState(null)
  const [lastEvent, setLastEvent] = useState(null)
  const [pulseKey, setPulseKey] = useState(0)
  const [pulse, setPulse] = useState(null)
  const [chartTicker, setChartTicker] = useState(null)
  const [chartTf, setChartTf] = useState('1h')
  const [chartLoading, setChartLoading] = useState(false)

  const loadConfig = useCallback(() => {
    api.getConfig().then(setConfig).catch(() => {})
  }, [])

  // 🆕 CHOP/Trend/Leverage в топ-баре теперь следуют за парой/tf, выбранными на
  // вкладке Chart, а не за жёстко зашитой "первой парой всегда на 1h"
  const loadPulse = useCallback(() => {
    if (!chartTicker) return
    api.getPulse(chartTicker, chartTf).then(setPulse).catch(() => {})
  }, [chartTicker, chartTf])

  useEffect(() => {
    if (!chartTicker && config?.pairs?.length) setChartTicker(config.pairs[0])
  }, [config, chartTicker])

  useEffect(() => {
    if (!authenticated) return
    loadConfig()
  }, [authenticated, loadConfig])

  useEffect(() => {
    if (!authenticated) return
    loadPulse()
  }, [authenticated, loadPulse])

  // 🆕 loadConfig/loadPulse меняют идентичность при каждом ререндере/смене пары —
  // держим свежие версии в ref, чтобы WS-эффект ниже не пересоздавал соединение
  // на каждый клик по паре в Chart, а зависел только от authenticated
  const loadConfigRef = useRef(loadConfig)
  const loadPulseRef = useRef(loadPulse)
  loadConfigRef.current = loadConfig
  loadPulseRef.current = loadPulse

  useEffect(() => {
    if (!authenticated) return
    const disconnect = connectLive(
      (event) => {
        setLastEvent(event)
        setPulseKey((k) => k + 1)
        if (event.type === 'config_changed') { loadConfigRef.current(); loadPulseRef.current() }
        if (event.type === 'scan_tick') loadPulseRef.current()
      },
      setConnStatus,
    )
    return disconnect
  }, [authenticated])

  // 🆕 Глобальный хук для Android-приложения (WebView-обёртка): при тапе по
  // push-уведомлению MainActivity.kt зовёт window.mufcaOpenSignal(ticker, tf, tab)
  // через evaluateJavascript, чтобы сразу открыть нужный сигнал, а не просто
  // развернуть дашборд на дефолтной вкладке. В обычном браузере эта функция просто
  // никогда не вызывается — no-op, безопасно для веб-версии.
  useEffect(() => {
    window.mufcaOpenSignal = (ticker, tf, tabId) => {
      if (tabId) setTab(tabId)
      if (ticker) setChartTicker(ticker)
      if (tf) setChartTf(tf)
    }
    return () => { delete window.mufcaOpenSignal }
  }, [])

  if (!authenticated) {
    return <LoginScreen onSuccess={() => setAuthenticated(true)} />
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="logo">
          <span
            key={pulseKey}
            className={`pulse ${connStatus === 'connected' ? 'live' : ''}`}
            title={connStatus === 'connected' ? 'Connected' : 'Disconnected'}
          />
          MUFCA
        </div>
        {config && <span className="mode-badge">{config.mode}</span>}
        {config && <span className="mode-badge">HTF {config.htf_bias}</span>}
        {pulse && (
          <span
            className="mode-badge"
            title={`${pulse.ticker} ${pulse.tf} — CHOP ${pulse.chop} (threshold ${pulse.chop_threshold})`}
            style={{ color: pulse.chop_trending ? 'var(--long)' : 'var(--text-dim)' }}
          >
            {pulse.ticker} CHOP {pulse.chop}
          </span>
        )}
        {pulse && (
          <span className="mode-badge" style={{ color: TREND_COLOR[pulse.trend] }} title={`${pulse.ticker} ${pulse.tf} trend`}>
            {TREND_LABEL[pulse.trend]}
          </span>
        )}
        {pulse && (
          <span className="mode-badge" title="Rough informational estimate, not used for real position sizing">
            {pulse.suggested_leverage}x lev
          </span>
        )}
        {pulse && <SignalLamps lamps={pulse.lamps} />}
        <div className="spacer" />
        {chartLoading && <span className="conn-label" style={{ color: 'var(--accent)' }}>loading…</span>}
        <span className="conn-label">
          {connStatus === 'connected' ? 'live' : connStatus}
        </span>
        <button
          className="btn"
          style={{ padding: '4px 10px', fontSize: 11 }}
          onClick={() => { clearAuthToken(); window.location.reload() }}
        >
          Log out
        </button>
      </header>

      <nav className="tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`tab ${tab === t.id ? 'active' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
        <div className="spacer" />
        <button
          className={`tab ${tab === 'settings' ? 'active' : ''}`}
          style={{ display: 'flex', alignItems: 'center', padding: '0 16px' }}
          onClick={() => setTab('settings')}
          title="Settings"
          aria-label="Settings"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </button>
      </nav>

      <main className="content">
        {tab === 'status' && <StatusPanel lastEvent={lastEvent} pairs={config?.pairs} />}
        {tab === 'chart' && (
          <ChartPanel
            pairs={config?.pairs}
            lastEvent={lastEvent}
            colors={config?.colors}
            ticker={chartTicker}
            tf={chartTf}
            onTickerChange={setChartTicker}
            onTfChange={setChartTf}
            onLoadingChange={setChartLoading}
          />
        )}
        {tab === 'history' && <HistoryPanel lastEvent={lastEvent} />}
        {tab === 'settings' && (
          <SettingsPanel config={config} onChanged={loadConfig} />
        )}
      </main>
    </div>
  )
}
