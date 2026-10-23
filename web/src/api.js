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
// 🆕 FIX: btoa() only accepts Latin-1 — a password with Cyrillic/emoji threw
// InvalidCharacterError before the request was even sent, and characters in the
// 128-255 range were encoded as Latin-1 while the server decodes the header as
// UTF-8 (so the login failed with "invalid password" for the right password).
// Encode as UTF-8 bytes first, which is what the backend expects.
function toBase64Utf8(str) {
  const bytes = new TextEncoder().encode(str)
  let bin = ''
  bytes.forEach((b) => { bin += String.fromCharCode(b) })
  return btoa(bin)
}

export async function login(username, password) {
  const token = toBase64Utf8(`${username}:${password}`)
  const res = await fetch(`${BASE}/api/config`, {
    headers: { Authorization: `Basic ${token}` },
  })
  if (res.status === 401) throw new Error('Invalid username or password')
  // the backend answers 429 (plain text) while this IP is locked out after
  // repeated failures — say so instead of a generic "Login failed (429)"
  if (res.status === 429) throw new Error('Too many failed attempts — try again later')
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
  // 🆕 REMOVED (dead code cleanup): getPairs() -> GET /api/pairs used to
  // sit here, called by nothing — the pairs list comes from getConfig()
  // (config.pairs) instead. The backend endpoint was removed too.
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
  setIntrabarCheck: (enabled) =>
    request('/api/config/intrabar-check', { method: 'POST', body: JSON.stringify({ enabled }) }),
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
  getComponents: (minSamples = 30) =>
    request(`/api/components?min_samples=${minSamples}`),
  runTp1Sim: ({ ticker, tf, numBars = 3000 } = {}) =>
    request('/api/tp1sim', { method: 'POST', body: JSON.stringify({ ticker: ticker || null, tf: tf || null, num_bars: numBars }) }),
  runCompSim: ({ ticker, tf, numBars = 3000, minSamples = 30 } = {}) =>
    request('/api/compsim', { method: 'POST', body: JSON.stringify({ ticker: ticker || null, tf: tf || null, num_bars: numBars, min_samples: minSamples }) }),
  setPullbackConfig: (patch) =>
    request('/api/config/pullback', { method: 'POST', body: JSON.stringify(patch) }),
  getPullbackStats: () => request('/api/pullback'),
  // Runs as a background job on the server: one request that stays open for
  // minutes is cut off by Cloudflare Tunnel at ~100 s, so start it and poll.
  startPullSim: ({ ticker, tf, numBars = 3000 } = {}) =>
    request('/api/pullsim/start', { method: 'POST', body: JSON.stringify({ ticker: ticker || null, tf: tf || null, num_bars: numBars }) }),
  getPullSimStatus: (jobId) =>
    request(`/api/pullsim/status?job_id=${encodeURIComponent(jobId)}`),
  setUtha: (enabled) =>
    request('/api/config/utha', { method: 'POST', body: JSON.stringify({ enabled }) }),
  setFilterToggle: (filter, enabled) =>
    request('/api/config/filters', { method: 'POST', body: JSON.stringify({ filter, enabled }) }),
  setChop: (tf, value) =>
    request('/api/config/chop', { method: 'POST', body: JSON.stringify({ tf, value }) }),
  setHurstWindow: (tf, value) =>
    request('/api/config/hurst_window', { method: 'POST', body: JSON.stringify({ tf, value }) }),
  setHurstConfig: (patch) =>
    request('/api/config/hurst_config', { method: 'POST', body: JSON.stringify(patch) }),
  setTpConfig: (param, value) =>
    request('/api/config/tpconfig', {
      method: 'POST',
      body: JSON.stringify({ param, value: String(value) }),
    }),
  setIndicators: (patch) =>
    request('/api/config/indicators', { method: 'POST', body: JSON.stringify(patch) }),
  setBreakoutConfig: (patch) =>
    request('/api/config/breakout', { method: 'POST', body: JSON.stringify(patch) }),
  setColors: (patch) =>
    request('/api/config/colors', { method: 'POST', body: JSON.stringify(patch) }),
  setVolumeProfile: (patch) =>
    request('/api/config/volume-profile', { method: 'POST', body: JSON.stringify(patch) }),
  setZones: (patch) =>
    request('/api/config/zones', { method: 'POST', body: JSON.stringify(patch) }),
  getHistorySummary: () => request('/api/history/summary'),
  getHistoryRecords: (ticker, tf, side, track = 'a', limit = 30) =>
    request(
      `/api/history/records?ticker=${encodeURIComponent(ticker)}&tf=${tf}&side=${side}&track=${track}&limit=${limit}`,
    ),
  // timestamp/entry fingerprint the clicked row so the server refuses the delete
  // if the list shifted since it was loaded (stale idx would hit another record).
  deleteHistoryRecord: (ticker, tf, side, idx, track = 'a', timestamp = null, entry = null) =>
    request(
      `/api/history/records?ticker=${encodeURIComponent(ticker)}&tf=${tf}&side=${side}&idx=${idx}&track=${track}` +
        (timestamp != null ? `&timestamp=${encodeURIComponent(timestamp)}` : '') +
        (entry != null ? `&entry=${encodeURIComponent(entry)}` : ''),
      { method: 'DELETE' },
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
  let connecting = false // 🆕 guards against two overlapping connect() runs
  let timeoutId = null // 🆕 tracked so it can be cleared on a repeated onclose/cleanup
  let pingId = null
  let lastRx = Date.now()
  let sawPong = false // the watchdog only arms once the server proved it answers pings

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

  function stopPing() {
    clearInterval(pingId)
    pingId = null
  }

  // 🆕 FIX: a phone that sleeps / switches networks leaves the WebSocket
  // half-open: no onclose fires for minutes, the badge keeps saying "live" and
  // no events arrive. Send a ping every 25 s (also keeps proxies from idling the
  // connection out) and drop the socket if nothing at all was received for 70 s.
  function startPing() {
    stopPing()
    pingId = setInterval(() => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return
      if (sawPong && Date.now() - lastRx > 70000) {
        ws.close()
        return
      }
      try { ws.send('ping') } catch (_) {}
    }, 25000)
  }

  async function connect() {
    if (closed || connecting) return
    connecting = true
    try {
      const ticket = await getTicket()
      if (closed) return // in case logout happened while we were waiting for the ticket
      const url = `${proto}://${window.location.host}/ws/live${ticket ? `?ticket=${encodeURIComponent(ticket)}` : ''}`
      const socket = new WebSocket(url)
      ws = socket
      socket.onopen = () => {
        attempt = 0
        lastRx = Date.now()
        onStatusChange?.('connected')
        startPing()
      }
      socket.onmessage = (msg) => {
        lastRx = Date.now()
        try {
          const data = JSON.parse(msg.data)
          if (data && data.type === 'pong') { sawPong = true; return }
          onEvent(data)
        } catch (_) {}
      }
      socket.onclose = () => {
        if (ws !== socket) return // a stale socket from before a forced reconnect
        stopPing()
        onStatusChange?.('disconnected')
        if (closed) return
        attempt += 1
        const delay = Math.min(1000 * 2 ** attempt, 15000)
        clearTimeout(timeoutId)
        timeoutId = setTimeout(connect, delay)
      }
      socket.onerror = () => socket.close()
    } finally {
      connecting = false
    }
  }

  // 🆕 Coming back to the tab / the network returning: don't wait out the
  // backoff (up to 15 s) — reconnect right away if the socket is not alive, and
  // probe it with a ping if it looks alive.
  function wake() {
    if (closed) return
    if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
      clearTimeout(timeoutId)
      attempt = 0
      connect()
    } else if (ws.readyState === WebSocket.OPEN) {
      try { ws.send('ping') } catch (_) {}
    }
  }
  const onVisible = () => { if (document.visibilityState === 'visible') wake() }
  document.addEventListener('visibilitychange', onVisible)
  window.addEventListener('online', wake)

  connect()

  return () => {
    closed = true
    clearTimeout(timeoutId)
    stopPing()
    document.removeEventListener('visibilitychange', onVisible)
    window.removeEventListener('online', wake)
    ws?.close()
  }
}
