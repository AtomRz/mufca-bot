import React, { useEffect, useState, useCallback } from 'react'
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

  const loadConfig = useCallback(() => {
    api.getConfig().then(setConfig).catch(() => {})
  }, [])

  const loadPulse = useCallback(() => {
    api.getPulse().then(setPulse).catch(() => {})
  }, [])

  useEffect(() => {
    if (!authenticated) return
    loadConfig()
    loadPulse()
  }, [authenticated, loadConfig, loadPulse])

  useEffect(() => {
    if (!authenticated) return
    const disconnect = connectLive(
      (event) => {
        setLastEvent(event)
        setPulseKey((k) => k + 1)
        if (event.type === 'config_changed') { loadConfig(); loadPulse() }
        if (event.type === 'scan_tick') loadPulse()
      },
      setConnStatus,
    )
    return disconnect
  }, [authenticated, loadConfig, loadPulse])

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
            CHOP {pulse.chop}
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
        {tab === 'chart' && <ChartPanel pairs={config?.pairs} lastEvent={lastEvent} colors={config?.colors} />}
        {tab === 'history' && <HistoryPanel lastEvent={lastEvent} />}
        {tab === 'settings' && (
          <SettingsPanel config={config} onChanged={loadConfig} />
        )}
      </main>
    </div>
  )
}
