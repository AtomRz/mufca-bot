import React, { useEffect, useState, useCallback, useRef } from 'react'
import { api, connectLive, getAuthToken, clearAuthToken } from './api'
import StatusPanel from './components/StatusPanel'
import ChartPanel from './components/ChartPanel'
import HistoryPanel from './components/HistoryPanel'
import OnchainPanel from './components/OnchainPanel'
import SettingsPanel from './components/SettingsPanel'
import LoginScreen from './components/LoginScreen'
import SignalLamps from './components/SignalLamps'

const TABS = [
  { id: 'status', label: 'Status' },
  { id: 'chart', label: 'Chart' },
  { id: 'history', label: 'History' },
  { id: 'onchain', label: 'Onchain' },
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
  const contentRef = useRef(null)

  const loadConfig = useCallback(() => {
    api.getConfig().then(setConfig).catch(() => {})
  }, [])

  // 🆕 CHOP/Trend/Leverage in the top bar now follow the pair/tf selected on
  // the Chart tab, instead of a hardcoded "always the first pair on 1h"
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

  // 🆕 loadConfig/loadPulse get a new identity on every rerender/pair change —
  // keep fresh versions in a ref so the WS effect below doesn't recreate the
  // connection on every pair click in Chart, and only depends on authenticated
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

  // 🆕 Global hook for the Android app (WebView wrapper): on tapping a push
  // notification, MainActivity.kt calls window.mufcaOpenSignal(ticker, tf, tab)
  // via evaluateJavascript, to open the right signal right away instead of
  // just expanding the dashboard on the default tab. In a regular browser
  // this function simply never gets called — a no-op, safe for the web version.
  useEffect(() => {
    window.mufcaOpenSignal = (ticker, tf, tabId) => {
      if (tabId) setTab(tabId)
      if (ticker) setChartTicker(ticker)
      if (tf) setChartTf(tf)
    }
    return () => { delete window.mufcaOpenSignal }
  }, [])

  // 🆕 FIX (Android client): SwipeRefreshLayout decides whether to allow
  // pull-to-refresh based on the WebView's own scrollY — but our actual
  // scrolling happens INSIDE .content (overflow-y: auto), not at the page
  // level, so the native scrollY always stays 0, even when a list (e.g.
  // History) is scrolled down. The result: swiping up inside the list,
  // bringing it back to the top, was read by the native side as "the user
  // is pulling down from scrollY=0" and triggered a refresh. Unlike the
  // chart (there, any touch unambiguously belongs to the canvas — it has no
  // native scroll of its own at all), here we DO need to account for the
  // real scrollTop position: block pull-to-refresh only while .content is
  // actually scrolled down, and release it as soon as we're back at the
  // very top — there the "pull down" gesture is legitimate again.
  useEffect(() => {
    const el = contentRef.current
    if (!el) return
    const notify = (active) => window.AndroidChartBridge?.setChartTouching?.(active)
    const onScroll = () => notify(el.scrollTop > 0)
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      el.removeEventListener('scroll', onScroll)
      notify(false)
    }
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

      <main className="content" ref={contentRef}>
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
        {tab === 'onchain' && <OnchainPanel lastEvent={lastEvent} />}
        {tab === 'settings' && (
          <SettingsPanel config={config} onChanged={loadConfig} />
        )}
      </main>
    </div>
  )
}
