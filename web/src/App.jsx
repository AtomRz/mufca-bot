import React, { useEffect, useState, useCallback, useRef } from 'react'
import { api, connectLive, getAuthToken, clearAuthToken } from './api'
import StatusPanel from './components/StatusPanel'
import ChartPanel from './components/ChartPanel'
import HistoryPanel from './components/HistoryPanel'
import SettingsPanel from './components/SettingsPanel'
import LoginScreen from './components/LoginScreen'

const TABS = [
  { id: 'status', label: 'Status' },
  { id: 'chart', label: 'Chart' },
  { id: 'history', label: 'History' },
  { id: 'settings', label: 'Settings' },
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
        <div className="spacer" />
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
