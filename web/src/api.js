const BASE = ''
const AUTH_KEY = 'mufca_auth_token'

// 🆕 localStorage вместо sessionStorage — токен переживает перезапуск WebView-процесса.
// Это осознанный компромисс ради Android-приложения: WebView не гарантирует, что
// sessionStorage доживёт до следующего холодного старта (Android может убить процесс
// в фоне), значит без этого пришлось бы логиниться заново при каждом открытии
// приложения. Для персонального дашборда за Basic Auth + Cloudflare Tunnel на своём же
// телефоне это приемлемый риск (тот же trade-off, что у любого обычного мобильного
// приложения с сохранённой сессией).
export function getAuthToken() {
  return localStorage.getItem(AUTH_KEY)
}

export function setAuthToken(token) {
  localStorage.setItem(AUTH_KEY, token)
}

export function clearAuthToken() {
  localStorage.removeItem(AUTH_KEY)
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
 * Хук-обёртка для /ws/live. Переподключается с бэкоффом,
 * зовёт onEvent для каждого JSON-события от бота (сигнал/тик сканера/смена конфига).
 *
 * 🆕 Раньше в URL WebSocket-подключения шёл сырой base64(login:password) —
 * единственный способ передать креды на WS-хендшейк, так как браузерный
 * WebSocket API не умеет кастомные заголовки. Проблема: всё, что в query-параметре
 * URL, обычно попадает в access-логи сервера (и потенциально Cloudflare) открытым
 * текстом — постоянный пароль, который живёт там вечно. Теперь вместо этого
 * сначала получаем короткоживущий (30 сек) одноразовый тикет через обычный
 * авторизованный fetch (креды — в заголовке, заголовки в access-логи не пишутся),
 * и уже с этим тикетом открываем WS. Даже если тикет попадёт в логи — он
 * бесполезен уже через полминуты или сразу после использования.
 */
export function connectLive(onEvent, onStatusChange) {
  let ws = null
  let closed = false
  let attempt = 0
  let timeoutId = null // 🆕 отслеживаем, чтобы чистить при повторных onclose/cleanup

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
    if (closed) return // на случай если logout произошёл, пока ждали тикет
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
