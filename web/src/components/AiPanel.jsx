import React, { useEffect, useState, useCallback } from 'react'
import { api } from '../api'
import { OriginSwitch, useOrigin } from './HistoryPanel'

// Gemini "second opinion" on each signal — informational only, it never blocks a trade.
// Settings on top, below them the check that matters: does the verdict predict results?

const VERDICT_LABEL = {
  approve: { text: 'Approve', color: 'var(--long)' },
  neutral: { text: 'Neutral', color: 'var(--accent)' },
  reject: { text: 'Reject', color: 'var(--short)' },
  none: { text: 'No verdict (baseline)', color: 'var(--text-dim)' },
}
const NEWS_LABEL = {
  low: { text: 'Low', color: 'var(--long)' },
  medium: { text: 'Medium', color: 'var(--accent)' },
  high: { text: 'High', color: 'var(--short)' },
}

function Toggle({ checked, onChange, disabled }) {
  return (
    <label className="switch">
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="switch-track" />
    </label>
  )
}

function fmtPct(v) {
  if (v === undefined || v === null || !Number.isFinite(v)) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

function StatsTable({ title, rows, labels }) {
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>{title}</th>
            <th>Trades</th>
            <th>Win rate</th>
            <th>Avg PnL</th>
            <th>Total PnL</th>
          </tr>
        </thead>
        <tbody>
          {Object.keys(labels).map((k) => {
            const g = rows?.[k]
            const empty = !g || !g.n
            return (
              <tr key={k}>
                <td style={{ color: labels[k].color, fontWeight: 600 }}>{labels[k].text}</td>
                <td>{g?.n ?? 0}</td>
                <td>{empty ? '—' : `${(g.win_rate * 100).toFixed(1)}%`}</td>
                <td style={empty ? undefined : { color: g.avg_pnl_pct >= 0 ? 'var(--long)' : 'var(--short)' }}>{empty ? '—' : fmtPct(g.avg_pnl_pct)}</td>
                <td style={empty ? undefined : { color: g.sum_pnl_pct >= 0 ? 'var(--long)' : 'var(--short)' }}>{empty ? '—' : fmtPct(g.sum_pnl_pct)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export default function AiPanel({ lastEvent, config, onConfigChanged }) {
  const [origin, setOrigin] = useOrigin()
  const [stats, setStats] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [model, setModel] = useState('')
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState(null)

  const ai = config?.ai_filter
  const timeoutOptions = config?.ai_timeout_options ?? [10, 15, 25, 40, 60]

  useEffect(() => { if (ai?.model) setModel(ai.model) }, [ai?.model])

  const load = useCallback(() => {
    api.getAiStats(origin).then((d) => { setStats(d); setError(null) }).catch((e) => setError(e.message))
  }, [origin])

  useEffect(() => {
    load()
    const id = setInterval(() => { if (!document.hidden) load() }, 60000)
    return () => clearInterval(id)
  }, [load])

  // a trade closed or a verdict just arrived
  useEffect(() => {
    if (lastEvent?.type === 'ai_verdict' || lastEvent?.type === 'signal') load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastEvent])

  const change = async (changes) => {
    setBusy(true)
    try {
      await api.setAiFilter(changes)
      setError(null)
      if (onConfigChanged) await onConfigChanged()
      load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const commitModel = () => {
    const m = model.trim()
    if (m && ai && m !== ai.model) change({ model: m })
  }

  // One tiny real request with the current settings — shows whether the next verdict can work.
  const runTest = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      setTestResult(await api.testAi())
    } catch (e) {
      setTestResult({ ok: false, status: 'error', message: e.message, warnings: [] })
    } finally {
      setTesting(false)
    }
  }

  if (!ai) return <div className="empty-state">Loading…</div>

  return (
    <div>
      {error && <div className="error-banner">{error}</div>}

      <div className="panel">
        <h3 className="panel-title">AI verdict (Gemini second opinion)</h3>
        <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 12 }}>
          Informational only: after a signal is sent, Gemini reviews it and the verdict is shown next to it. Nothing is blocked.
          Judge it by the table below, not by how convincing the reasons sound.
        </div>
        {!ai.has_key && (
          <div className="error-banner">GEMINI_API_KEY is not set — add it to .env and restart the container. Until then no verdicts are requested.</div>
        )}
        <div className="toggle-row">
          <span className="row-label" title="Master switch. Needs GEMINI_API_KEY in the environment.">Enabled</span>
          <Toggle checked={ai.enabled} disabled={busy} onChange={(v) => change({ enabled: v })} />
        </div>
        <div className="toggle-row">
          <span className="row-label" title="Lets Gemini search Google for recent news and events (the news risk field). Google bills it per search query after a monthly free allowance; off = the verdict is based only on the numbers sent.">
            Google Search grounding (news)
          </span>
          <Toggle checked={ai.grounding} disabled={busy} onChange={(v) => change({ grounding: v })} />
        </div>
        <div className="field">
          <label title="Gemini model code as listed in Google's API docs. A wrong code just yields 'no data' verdicts (see the log).">Model</label>
          <input
            type="text"
            value={model}
            disabled={busy}
            spellCheck={false}
            onChange={(e) => setModel(e.target.value)}
            onBlur={commitModel}
            onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur() }}
          />
        </div>
        <div className="field">
          <label title="Hard limit for one request. A grounded request is slower. On timeout the signal simply has no verdict.">Timeout</label>
          <select value={ai.timeout_s} disabled={busy} onChange={(e) => change({ timeout_s: Number(e.target.value) })}>
            {[...new Set([...timeoutOptions, ai.timeout_s])].sort((a, b) => a - b).map((s) => (
              <option key={s} value={s}>{s} s</option>
            ))}
          </select>
        </div>
        <div className="field">
          <label title="Sends one tiny request to Gemini with the settings above (no market data) and checks that the key works, the model answers and the reply parses.">Connection</label>
          <button className="btn" disabled={busy || testing} onClick={runTest}>
            {testing ? 'Testing…' : 'Test'}
          </button>
        </div>
        {testResult && (
          <div style={{ fontSize: 13, marginTop: 4 }}>
            <div style={{ color: testResult.ok ? 'var(--long)' : 'var(--short)', fontWeight: 600 }}>
              {testResult.ok ? '✓ OK' : '✗ Failed'}
              {testResult.model && <span style={{ color: 'var(--text-dim)', fontWeight: 400 }}>
                {` · ${testResult.model} · grounding ${testResult.grounded ? 'on' : 'off'} · ${testResult.latency_s}s`}
              </span>}
            </div>
            <div style={{ color: 'var(--text-dim)', marginTop: 2 }}>{testResult.message}</div>
            {(testResult.warnings || []).map((w) => (
              <div key={w} style={{ color: 'var(--accent)', marginTop: 2 }}>⚠ {w}</div>
            ))}
          </div>
        )}
      </div>

      <div style={{ margin: '12px 0' }}><OriginSwitch origin={origin} onChange={setOrigin} /></div>

      <div className="panel">
        <h3 className="panel-title">Results by verdict (closed trades)</h3>
        {!stats && !error && <div className="empty-state">Loading…</div>}
        {stats && (
          <>
            <StatsTable title="Verdict" rows={stats.by_verdict} labels={VERDICT_LABEL} />
            <div style={{ height: 12 }} />
            <StatsTable title="News risk" rows={stats.by_news_risk} labels={NEWS_LABEL} />
            <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 10 }}>
              The baseline row is closed trades without a verdict (opened before this feature, or the AI had no answer).
              Wait for a few dozen trades per group before reading anything into a gap.
              {stats.open_with_verdict > 0 && ` ${stats.open_with_verdict} open trade(s) already have a verdict and will be counted once they close.`}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
