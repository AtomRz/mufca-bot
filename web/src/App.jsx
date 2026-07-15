import React, { useEffect, useState, useCallback } from 'react'
import { api, connectLive } from './api'
import StatusPanel from './components/StatusPanel'
import ChartPanel from './components/ChartPanel'
import HistoryPanel from './components/HistoryPanel'
import SettingsPanel from './components/SettingsPanel'

const TABS = [
  { id: 'status', label: 'Status' },
  { id: 'chart', label: 'Chart' },
  { id: 'history', label: 'History' },
  { id: 'settings', label: 'Settings' },
]

export default function App() {
  const [tab, setTab] = useState('status')
  const [connStatus, setConnStatus] = useState('connecting')
  const [config, setConfig] = useState(null)
  const [lastEvent, setLastEvent] = useState(null)
  const [pulseKey, setPulseKey] = useState(0)

  const loadConfig = useCallback(() => {
    api.getConfig().then(setConfig).catch(() => {})
  }, [])

  useEffect(() => {
    loadConfig()
  }, [loadConfig])

  useEffect(() => {
    const disconnect = connectLive(
      (event) => {
        setLastEvent(event)
        setPulseKey((k) => k + 1)
        if (event.type === 'config_changed') loadConfig()
      },
      setConnStatus,
    )
    return disconnect
  }, [loadConfig])

  return (
    <div className="app">
      <header className="topbar">
        <div className="logo">
          <span
            key={pulseKey}
            className={`pulse ${connStatus === 'connected' ? 'live' : ''}`}
            title={connStatus === 'connected' ? 'На связи' : 'Нет соединения'}
          />
          MUFCA
        </div>
        {config && <span className="mode-badge">{config.mode}</span>}
        {config && <span className="mode-badge">HTF {config.htf_bias}</span>}
        <div className="spacer" />
        <span className="conn-label">
          {connStatus === 'connected' ? 'live' : connStatus}
        </span>
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
