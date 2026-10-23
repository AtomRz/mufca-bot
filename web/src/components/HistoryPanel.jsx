import React, { useEffect, useState, useCallback, useRef } from 'react'
import { api } from '../api'

function winrateColor(wr) {
  if (wr >= 0.55) return 'var(--long)'
  if (wr >= 0.45) return 'var(--accent)'
  return 'var(--short)'
}

function StatCard({ label, value, color }) {
  return (
    <div className="stat-card">
      <div className="label">{label}</div>
      <div className="value" style={color ? { color } : undefined}>{value}</div>
    </div>
  )
}

function fmtPct(v) {
  if (v === undefined || v === null || !Number.isFinite(v)) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

// 🆕 a missing/non-finite aggregate used to throw "toFixed of null" and blank the
// whole History tab (the error boundary swallows the panel)
function num2(v) {
  return Number.isFinite(v) ? v.toFixed(2) : '—'
}

function fmtDate(ts) {
  if (!ts) return '—'
  try {
    // 🆕 FIX: backtest records from before this fix stored the timestamp as
    // a string of digits ("1721260800000" — epoch in ms), not ISO.
    // new Date("1721260800000") parses the STRING as a date format (not as
    // a number), doesn't recognize this pattern, and gives Invalid Date.
    // new Date(1721260800000) with the same value as a Number works
    // correctly — so explicitly convert purely-numeric strings to Number.
    const value = typeof ts === 'string' && /^\d+$/.test(ts) ? Number(ts) : ts
    const d = new Date(value)
    if (isNaN(d.getTime())) return ts
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  } catch (_) {
    return ts
  }
}

export default function HistoryPanel({ lastEvent }) {
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null) // {ticker, tf, side, track}
  const [records, setRecords] = useState(null)
  const [recordsError, setRecordsError] = useState(null)
  const [deletingIdx, setDeletingIdx] = useState(null)
  const [confirmIdx, setConfirmIdx] = useState(null) // row waiting for the second "sure?" click
  const [deleteError, setDeleteError] = useState(null)
  const recordsSeq = useRef(0)
  const confirmTimer = useRef(null)

  const load = useCallback(() => {
    api
      .getHistorySummary()
      .then((d) => { setSummary(d); setError(null) }) // 🆕 a later success clears an earlier error
      .catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    load()
    // 🆕 no point polling while the tab is in the background
    const id = setInterval(() => { if (!document.hidden) load() }, 60000)
    return () => clearInterval(id)
  }, [load])

  const loadRecords = useCallback(() => {
    if (!selected) return
    // 🆕 FIX: clicking through rows quickly let a slow response for the PREVIOUS
    // row land after the current one and replace its trades (under the wrong
    // title). Only the latest request may set the state.
    const seq = ++recordsSeq.current
    api
      .getHistoryRecords(selected.ticker, selected.tf, selected.side, selected.track, 30)
      .then((d) => { if (seq === recordsSeq.current) { setRecords(d.records); setRecordsError(null) } })
      // 🆕 a failed request used to show "No closed trades for this combination"
      // — indistinguishable from a really empty list
      .catch((e) => { if (seq === recordsSeq.current) setRecordsError(e.message) })
  }, [selected])

  // 🆕 FIX: switching to another row kept showing the previous row's trades
  // (and never showed "Loading…") until the new response arrived
  useEffect(() => {
    setRecords(null)
    setRecordsError(null)
    setConfirmIdx(null)
    setDeleteError(null)
    loadRecords()
  }, [loadRecords])

  // a signal just closed — refresh the summary, and the open trades list too
  useEffect(() => {
    if (lastEvent?.type === 'signal') { load(); loadRecords() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastEvent])

  useEffect(() => () => clearTimeout(confirmTimer.current), [])

  // 🆕 FIX: window.confirm()/alert() are silently swallowed by an Android
  // WebView whose client doesn't implement onJsConfirm/onJsAlert (confirm()
  // then always returns false, so the delete button did nothing). Confirmation
  // is now inline: first click arms the button ("sure?"), second click deletes;
  // it disarms itself after 4 s. Errors show in the panel instead of alert().
  const handleDelete = async (rec) => {
    if (!selected) return
    if (confirmIdx !== rec.idx) {
      setConfirmIdx(rec.idx)
      clearTimeout(confirmTimer.current)
      confirmTimer.current = setTimeout(() => setConfirmIdx(null), 4000)
      return
    }
    clearTimeout(confirmTimer.current)
    setConfirmIdx(null)
    setDeleteError(null)
    setDeletingIdx(rec.idx)
    try {
      await api.deleteHistoryRecord(selected.ticker, selected.tf, selected.side, rec.idx, selected.track, rec.timestamp, rec.entry)
      loadRecords() // row counts/PnL aggregates changed — refresh both tables
      load()
    } catch (e) {
      setDeleteError(`Failed to delete record: ${e.message}`)
      loadRecords() // the usual reason is a stale list — show the current one
    } finally {
      setDeletingIdx(null)
    }
  }

  if (!summary) return error ? <div className="error-banner">{error}</div> : <div className="empty-state">Loading…</div>

  const { rows, total } = summary

  if (rows.length === 0) {
    return <div className="empty-state">No closed signals yet — stats will appear after the first TP/SL hit.</div>
  }

  const isSelected = (r) =>
    selected && selected.ticker === r.ticker && selected.tf === r.tf && selected.side === r.side && selected.track === r.track

  return (
    <div>
      {error && <div className="error-banner">{error}</div>}
      {total && (
        <div className="panel">
          <h3 className="panel-title">Overall (all pairs)</h3>
          <div className="stat-cards">
            <StatCard label="Win Rate" value={`${Math.round(total.win_rate * 100)}%`} color={winrateColor(total.win_rate)} />
            <StatCard label="Signals" value={total.count} />
            <StatCard label="Avg PnL" value={fmtPct(total.avg_pnl)} color={total.avg_pnl >= 0 ? 'var(--long)' : 'var(--short)'} />
            <StatCard label="Avg MFE" value={`${num2(total.avg_mfe)}%`} />
            <StatCard label="Avg MAE" value={`${num2(total.avg_mae)}%`} />
            <StatCard label="TP / SL after TP1 / SL / Cancel" value={`${total.tp_hits} / ${total.sl_after_tp1_hits ?? 0} / ${total.sl_hits} / ${total.cancelled}`} />
          </div>
        </div>
      )}

      <div className="panel">
        <h3 className="panel-title">By pair / timeframe / track</h3>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Pair</th>
                <th>TF</th>
                <th>Side</th>
                <th>Track</th>
                <th>Signals</th>
                <th>Win Rate</th>
                <th>Avg PnL</th>
                <th>Avg MFE</th>
                <th>Avg MAE</th>
                <th title="TP2 hit / stopped after TP1 (moved SL, usually still a profit) / SL before TP1 / cancelled">TP/SL·TP1/SL/Canc</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={`${r.ticker}-${r.tf}-${r.side}-${r.track}`}
                  className={isSelected(r) ? 'selected' : ''}
                  onClick={() => setSelected(r)}
                >
                  <td>{r.ticker}</td>
                  <td>{r.tf}</td>
                  <td className={r.side === 'long' ? 'tag long' : 'tag short'} style={{ display: 'inline-block', margin: '2px 0' }}>
                    {r.side}
                  </td>
                  <td>{r.track === 'a' ? 'Andean' : r.track === 'u' ? 'UT Bot' : r.track === 'b' ? 'Breakout' : r.track}</td>
                  <td>{r.count}</td>
                  <td>
                    <span className="winrate-bar">
                      <span
                        className="winrate-bar-fill"
                        style={{ width: `${Math.round(r.win_rate * 100)}%`, background: winrateColor(r.win_rate) }}
                      />
                    </span>
                    <span style={{ color: winrateColor(r.win_rate) }}>{Math.round(r.win_rate * 100)}%</span>
                  </td>
                  <td style={{ color: r.avg_pnl >= 0 ? 'var(--long)' : 'var(--short)' }}>{fmtPct(r.avg_pnl)}</td>
                  <td>{num2(r.avg_mfe)}%</td>
                  <td>{num2(r.avg_mae)}%</td>
                  <td className="mono" style={{ color: 'var(--text-dim)' }}>
                    {r.tp_hits}/{r.sl_after_tp1_hits ?? 0}/{r.sl_hits}/{r.cancelled}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {selected && (
        <div className="panel">
          <h3 className="panel-title">
            {selected.ticker} · {selected.tf} · {selected.side} · {selected.track === 'a' ? 'Andean' : selected.track === 'u' ? 'UT Bot' : selected.track === 'b' ? 'Breakout' : selected.track} — recent trades
          </h3>
          {recordsError && <div className="error-banner">{recordsError}</div>}
          {deleteError && <div className="error-banner">{deleteError}</div>}
          {!records && !recordsError && <div className="empty-state">Loading…</div>}
          {records && records.length === 0 && <div className="empty-state">No closed trades for this combination.</div>}
          {records && records.length > 0 && (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>Entry</th>
                    <th>Exit</th>
                    <th>PnL</th>
                    <th>MFE</th>
                    <th>MAE</th>
                    <th>Result</th>
                    <th>Regime</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {records.map((rec) => (
                    <tr key={rec.idx}>
                      <td>{fmtDate(rec.timestamp)}</td>
                      <td>{rec.entry}</td>
                      <td>{rec.exit ?? '—'}</td>
                      <td style={{ color: rec.moved_pct >= 0 ? 'var(--long)' : 'var(--short)' }}>{fmtPct(rec.moved_pct)}</td>
                      <td>{num2(rec.max_favorable_pct ?? 0)}%</td>
                      <td>{num2(rec.max_adverse_pct ?? 0)}%</td>
                      <td>
                        <span className={`tag ${rec.exit_type === 'tp' ? 'long' : rec.exit_type === 'sl' ? 'short' : rec.exit_type === 'sl_after_tp1' ? 'long' : 'flat'}`}>
                          {rec.exit_type === 'sl_after_tp1' ? 'SL (post-TP1)' : rec.exit_type}
                        </span>
                        {rec.tp1_hit && <span className="tag long" style={{ marginLeft: 6 }} title="TP1 was reached before final exit — PnL reflects the blended 50/50 result">TP1 ✓</span>}
                        {rec.synthetic && <span className="tag flat" style={{ marginLeft: 6 }}>sim</span>}
                      </td>
                      <td style={{ color: 'var(--text-dim)' }}>{rec.regime || '—'}</td>
                      <td>
                        <button
                          className="row-delete-btn"
                          title={confirmIdx === rec.idx ? 'Click again to delete — this cannot be undone' : 'Delete this record'}
                          onClick={() => handleDelete(rec)}
                          disabled={deletingIdx === rec.idx}
                          style={confirmIdx === rec.idx ? { color: 'var(--short)', fontWeight: 700 } : undefined}
                        >
                          {deletingIdx === rec.idx ? '…' : confirmIdx === rec.idx ? 'sure?' : '×'}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
