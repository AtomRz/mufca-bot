const BASE = ''
const AUTH_KEY = 'mufca_auth_token'

// 🆕 localStorage instead of sessionStorage — the token survives a restart
// of the WebView process. This is a deliberate trade-off for the Android
// app: WebView doesn't guarantee sessionStorage survives to the next cold
// start (Android can kill the process in the background), so without this
// you'd have to log in again every time the app opens. For a personal
// dashboard behind Basic Auth + Cloudflare Tunnel on your own phone this is
// an acceptable risk (the same trade-off any regular mobile app with a
// saved session makes).
export function getAuthToken() {
  return localStorage.getItem(AUTH_KEY)
}

export function setAuthToken(token) {
  localStorage.setItem(AUTH_KEY, token)
}

export function clearAuthToken() {
  localStorage.removeItem(AUTH_KEY)
}

/** Tries to log in — makes a real request to /api/config with these
 * credentials. If WEB_USERNAME/WEB_PASSWORD aren't set on the backend, auth
 * is disabled there and this always succeeds regardless of the values
 * entered — that's intentional. */
export async function login(username, password) {
  const token = btoa(`${username}:${password}`)
  const res = await fetch(`${BASE}/api/config`, {
    headers: { Authorization: `Basic ${token}` },
  })
  if (res.status === 401) throw new Error('Invalid username or password')
  if (!res.ok) throw new Error(`Login failed (${res.status})`)
  setAuthToken(token)
  return token
}

async function request(path, options = {}) {
  const token = getAuthToken()
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) }
  if (token) headers.Authorization = `Basic ${token}`

  const res = await fetch(`${BASE}${path}`, { ...options, headers })

  if (res.status === 401) {
    clearAuthToken()
    window.location.reload() // will show the login form again
    throw new Error('Session expired')
  }
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail || detail
    } catch (_) {}
    throw new Error(detail)
  }
  return res.json()
}

export const api = {
  getStatus: () => request('/api/status'),
  getOnchain: () => request('/api/onchain'),
  getPairs: () => request('/api/pairs'),
  addPair: (ticker) =>
    request('/api/pairs', { method: 'POST', body: JSON.stringify({ ticker }) }),
  removePair: (ticker, purgeHistory = false) =>
    request(
      `/api/pairs/${encodeURIComponent(ticker)}${purgeHistory ? '?purge_history=true' : ''}`,
      { method: 'DELETE' }
    ),
  getChart: (ticker, tf, track = 'a', limit = 200) =>
    request(
      `/api/chart?ticker=${encodeURIComponent(ticker)}&tf=${tf}&track=${track}&limit=${limit}`,
    ),
  getConfig: () => request('/api/config'),
  setMode: (mode) =>
    request('/api/config/mode', { method: 'POST', body: JSON.stringify({ mode }) }),
  setHtf: (htf) =>
    request('/api/config/htf', { method: 'POST', body: JSON.stringify({ htf }) }),
  setTp1SlMode: (tp1SlMode) =>
    request('/api/config/tp1-sl-mode', { method: 'POST', body: JSON.stringify({ tp1_sl_mode: tp1SlMode }) }),
  setDiscordNotifications: (enabled) =>
    request('/api/config/discord-notifications', { method: 'POST', body: JSON.stringify({ enabled }) }),
  setScanInterval: (seconds) =>
    request('/api/config/scan-interval', { method: 'POST', body: JSON.stringify({ seconds }) }),
  setOnchainInterval: (seconds) =>
    request('/api/config/onchain-interval', { method: 'POST', body: JSON.stringify({ seconds }) }),
  setDerivativesEnabled: (enabled) =>
    request('/api/config/derivatives-enabled', { method: 'POST', body: JSON.stringify({ enabled }) }),
  setDerivativesInterval: (seconds) =>
    request('/api/config/derivatives-interval', { method: 'POST', body: JSON.stringify({ seconds }) }),
  getDerivatives: (ticker) =>
    request(`/api/derivatives${ticker ? `?ticker=${encodeURIComponent(ticker)}` : ''}`),
  getSpread: (ticker) =>
    request(`/api/spread${ticker ? `?ticker=${encodeURIComponent(ticker)}` : ''}`),
  setUtha: (enabled) =>
    request('/api/config/utha', { method: 'POST', body: JSON.stringify({ enabled }) }),
  setFilterToggle: (filter, enabled) =>
    request('/api/config/filters', { method: 'POST', body: JSON.stringify({ filter, enabled }) }),
  setChop: (tf, value) =>
    request('/api/config/chop', { method: 'POST', body: JSON.stringify({ tf, value }) }),
  setTpConfig: (param, value) =>
    request('/api/config/tpconfig', {
      method: 'POST',
      body: JSON.stringify({ param, value: String(value) }),
    }),
  setIndicators: (patch) =>
    request('/api/config/indicators', { method: 'POST', body: JSON.stringify(patch) }),
  setColors: (patch) =>
    request('/api/config/colors', { method: 'POST', body: JSON.stringify(patch) }),
  setVolumeProfile: (patch) =>
    request('/api/config/volume-profile', { method: 'POST', body: JSON.stringify(patch) }),
  getHistorySummary: () => request('/api/history/summary'),
  getHistoryRecords: (ticker, tf, side, track = 'a', limit = 30) =>
    request(
      `/api/history/records?ticker=${encodeURIComponent(ticker)}&tf=${tf}&side=${side}&track=${track}&limit=${limit}`,
    ),
  getPulse: (ticker, tf = '1h') =>
    request(`/api/pulse?tf=${tf}${ticker ? `&ticker=${encodeURIComponent(ticker)}` : ''}`),
  getDevices: () => request('/api/devices'),
  testPush: () => request('/api/devices/test-push', { method: 'POST' }),
}

/**
 * Hook-style wrapper for /ws/live. Reconnects with backoff, calls onEvent
 * for every JSON event from the bot (signal / scanner tick / config change).
 *
 * 🆕 The WebSocket connection URL used to carry the raw base64(login:password) —
 * the only way to pass credentials to a WS handshake, since the browser
 * WebSocket API can't set custom headers. The problem: anything in a URL
 * query parameter usually ends up in the server's (and potentially
 * Cloudflare's) access logs in plain text — a permanent password that lives
 * there forever. Now we instead first get a short-lived (30 sec), single-use
 * ticket via a normal authorized fetch (credentials go in a header, headers
 * aren't written to access logs), and open the WS with that ticket. Even if
 * the ticket ends up in the logs, it's worthless after half a minute, or
 * immediately after being used.
 */
export function connectLive(onEvent, onStatusChange) {
  let ws = null
  let closed = false
  let attempt = 0
  let timeoutId = null // 🆕 tracked so it can be cleared on a repeated onclose/cleanup

  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'

  async function getTicket() {
    const token = getAuthToken()
    if (!token) return null
    try {
      const res = await fetch('/api/ws-ticket', {
        method: 'POST',
        headers: { Authorization: `Basic ${token}` },
      })
      if (!res.ok) return null
      const data = await res.json()
      return data.ticket
    } catch (_) {
      return null
    }
  }

  async function connect() {
    if (closed) return
    const ticket = await getTicket()
    if (closed) return // in case logout happened while we were waiting for the ticket
    const url = `${proto}://${window.location.host}/ws/live${ticket ? `?ticket=${encodeURIComponent(ticket)}` : ''}`
    ws = new WebSocket(url)
    ws.onopen = () => {
      attempt = 0
      onStatusChange?.('connected')
    }
    ws.onmessage = (msg) => {
      try {
        onEvent(JSON.parse(msg.data))
      } catch (_) {}
    }
    ws.onclose = () => {
      onStatusChange?.('disconnected')
      if (closed) return
      attempt += 1
      const delay = Math.min(1000 * 2 ** attempt, 15000)
      clearTimeout(timeoutId)
      timeoutId = setTimeout(connect, delay)
    }
    ws.onerror = () => ws.close()
  }

  connect()

  return () => {
    closed = true
    clearTimeout(timeoutId)
    ws?.close()
  }
}
