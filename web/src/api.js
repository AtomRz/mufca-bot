const BASE = ''

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
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
  const url = `${proto}://${window.location.host}/ws/live`

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
