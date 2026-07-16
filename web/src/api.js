const BASE = ''
const AUTH_KEY = 'mufca_auth_token'

// 🆕 Явное хранение auth-токена в sessionStorage вместо расчёта на то, что браузер
// сам протащит закэшированный Basic Auth на все запросы. Он это делает для fetch(),
// но НЕ делает для нативного WebSocket API (тот вообще не умеет кастомные заголовки) —
// поэтому токен явно кладём и в заголовок fetch(), и в query-параметр WS-урла.
export function getAuthToken() {
  return sessionStorage.getItem(AUTH_KEY)
}

export function setAuthToken(token) {
  sessionStorage.setItem(AUTH_KEY, token)
}

export function clearAuthToken() {
  sessionStorage.removeItem(AUTH_KEY)
}

/** Пробует залогиниться — делает реальный запрос к /api/config с этими кредами.
 * Если WEB_USERNAME/WEB_PASSWORD не заданы на бэкенде, auth там выключен и это
 * всегда успешно независимо от введённых значений — так и задумано. */
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
    window.location.reload() // покажет форму логина заново
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
  getPairs: () => request('/api/pairs'),
  addPair: (ticker) =>
    request('/api/pairs', { method: 'POST', body: JSON.stringify({ ticker }) }),
  removePair: (ticker) =>
    request(`/api/pairs/${encodeURIComponent(ticker)}`, { method: 'DELETE' }),
  getChart: (ticker, tf, track = 'a', limit = 150) =>
    request(
      `/api/chart?ticker=${encodeURIComponent(ticker)}&tf=${tf}&track=${track}&limit=${limit}`,
    ),
  getConfig: () => request('/api/config'),
  setMode: (mode) =>
    request('/api/config/mode', { method: 'POST', body: JSON.stringify({ mode }) }),
  setHtf: (htf) =>
    request('/api/config/htf', { method: 'POST', body: JSON.stringify({ htf }) }),
  setUtha: (enabled) =>
    request('/api/config/utha', { method: 'POST', body: JSON.stringify({ enabled }) }),
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
  getHistorySummary: () => request('/api/history/summary'),
  getHistoryRecords: (ticker, tf, side, track = 'a', limit = 30) =>
    request(
      `/api/history/records?ticker=${encodeURIComponent(ticker)}&tf=${tf}&side=${side}&track=${track}&limit=${limit}`,
    ),
  getPulse: (ticker, tf = '1h') =>
    request(`/api/pulse?tf=${tf}${ticker ? `&ticker=${encodeURIComponent(ticker)}` : ''}`),
}

/**
 * Хук-обёртка для /ws/live. Переподключается с бэкоффом,
 * зовёт onEvent для каждого JSON-события от бота (сигнал/тик сканера/смена конфига).
 */
export function connectLive(onEvent, onStatusChange) {
  let ws = null
  let closed = false
  let attempt = 0

  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const token = getAuthToken()
  const url = `${proto}://${window.location.host}/ws/live${token ? `?auth=${encodeURIComponent(token)}` : ''}`

  function connect() {
    if (closed) return
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
      setTimeout(connect, delay)
    }
    ws.onerror = () => ws.close()
  }

  connect()

  return () => {
    closed = true
    ws?.close()
  }
}
