import React, { useEffect, useState, useCallback } from 'react'
import { api } from '../api'
import { SimScopeFields, SimStatus, BarCoverageWarning, useSimJob, barsError } from './InsightsPanel'

const KIND_LABEL = {
  manual: 'manual',
  auto: 'automatic',
  'pre-restore': 'before restore',
  'pre-fill': 'before fill',
  'pre-backfill': 'before backfill',
  'pre-tag': 'before tagging',
  'pre-dedupe': 'before dedupe',
  'pre-sort': 'before sort',
  'pre-reset': 'before reset',
  'pre-delete': 'before delete',
}

const SOURCE_COLOR = {
  live: 'var(--long)',
  backtest: 'var(--text-dim)',
  backtest_fill: 'var(--accent)',
  backtest_backfill: 'var(--accent)',
  sim: 'var(--text-faint)',
  unknown: 'var(--short)',
}

const small = { fontSize: 12, color: 'var(--text-faint)' }

function fmtWhen(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function fmtSize(bytes) {
  if (!Number.isFinite(bytes)) return '—'
  return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`
}

function Notice({ notice }) {
  if (!notice) return null
  return (
    <p style={{ fontSize: 13, marginBottom: 10, color: notice.error ? 'var(--short)' : 'var(--long)' }}>
      {notice.text}
    </p>
  )
}

// ---------------------------------------------------------------------
// Backups list + restore
// ---------------------------------------------------------------------
function BackupDetail({ id, onRestore, busy }) {
  const [detail, setDetail] = useState(null)
  const [error, setError] = useState(null)
  const [confirm, setConfirm] = useState(null) // {ticker, tf} waiting for the second click

  useEffect(() => {
    let alive = true
    api.getBackupDetail(id).then((d) => alive && setDetail(d)).catch((e) => alive && setError(e.message))
    return () => { alive = false }
  }, [id])

  if (error) return <p style={{ color: 'var(--short)', fontSize: 12 }}>{error}</p>
  if (!detail) return <p style={small}>Loading…</p>

  const same = (r) => r.backup === r.now && r.backup_live === r.now_live && r.backup_backtest === r.now_backtest
  // one line per pair/timeframe (both sides summed)
  const slots = {}
  detail.rows.forEach((r) => {
    const k = `${r.ticker}|${r.tf}`
    const c = slots[k] || (slots[k] = { ticker: r.ticker, tf: r.tf, backup: 0, now: 0, backup_live: 0, now_live: 0, differs: false })
    c.backup += r.backup; c.now += r.now; c.backup_live += r.backup_live; c.now_live += r.now_live
    if (!same(r)) c.differs = true
  })
  const list = Object.values(slots)

  const ask = (ticker, tf) => {
    const key = `${ticker || ''}|${tf || ''}`
    if (confirm === key) { setConfirm(null); onRestore(ticker, tf) } else { setConfirm(key) }
  }
  const label = (ticker, tf, text) => (confirm === `${ticker || ''}|${tf || ''}` ? 'Sure? Click again' : text)

  return (
    <div style={{ marginTop: 8 }}>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr><th>Pair</th><th>TF</th><th>Records now</th><th>In backup</th><th>Live now → backup</th><th /></tr>
          </thead>
          <tbody>
            {list.map((c) => (
              <tr key={`${c.ticker}-${c.tf}`}>
                <td>{c.ticker}</td>
                <td>{c.tf}</td>
                <td>{c.now}</td>
                <td style={c.differs ? { color: 'var(--accent)' } : undefined}>{c.backup}</td>
                <td>{c.now_live} → {c.backup_live}</td>
                <td style={{ textAlign: 'right' }}>
                  <button className="btn" disabled={busy} onClick={() => ask(c.ticker, c.tf)}>
                    {label(c.ticker, c.tf, 'Restore this')}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p style={{ ...small, marginTop: 8 }}>
        Restore replaces the stored records with the backup's. Open trades are never touched, and a safety copy
        of the current history is saved first, so a restore can be undone from this list.
      </p>
      <button className="btn danger" disabled={busy} onClick={() => ask(null, null)} style={{ marginTop: 6 }}>
        {label(null, null, 'Restore ALL pairs from this backup')}
      </button>
    </div>
  )
}

function BackupsSection({ refreshKey, onHistoryChanged }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(null)
  const [confirmDelete, setConfirmDelete] = useState(null)

  const load = useCallback(() => {
    api.getBackups().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }, [])
  useEffect(() => { load() }, [load, refreshKey])

  const run = async (fn, okText) => {
    setBusy(true); setNotice(null)
    try {
      const res = await fn()
      setNotice({ text: okText ? okText(res) : 'Done' })
      load()
      return res
    } catch (e) {
      setNotice({ error: true, text: e.message })
    } finally {
      setBusy(false)
    }
  }

  const create = async () => {
    const res = await run(() => api.createBackup(label), (m) => `Backup saved: ${m.records} records.`)
    if (res) setLabel('')
  }

  const restore = async (id, ticker, tf) => {
    const res = await run(
      () => api.restoreBackup(id, ticker, tf),
      (r) => `Restored ${r.records} records (${r.open_kept} open kept). Undo: restore the "before restore" backup ${r.pre_restore_backup}.`,
    )
    if (res) onHistoryChanged()
  }

  const remove = async (id) => {
    if (confirmDelete !== id) { setConfirmDelete(id); setTimeout(() => setConfirmDelete((c) => (c === id ? null : c)), 4000); return }
    setConfirmDelete(null)
    if (open === id) setOpen(null)
    await run(() => api.deleteBackup(id), () => 'Backup deleted.')
  }

  const cur = data?.current

  return (
    <div className="panel">
      <h3 className="panel-title">Saved backups of the signal history</h3>
      <p style={{ ...small, marginBottom: 12 }}>
        A backup is a full copy of <code>signals_history.json</code>. One is taken automatically on start and then every{' '}
        {data?.interval_hours ?? '…'} h when something changed; a safety copy is also taken right before a restore, a fill,
        a backfill, tagging, a <code>!reset</code> or a delete. The newest {data?.keep_auto ?? '…'} automatic/safety copies
        are kept; manual ones are never deleted automatically. They live in <code>{data?.dir ?? 'data/backups'}</code> —
        the same volume as the history, so use Download for a copy that survives losing the volume.
      </p>

      {cur && (
        <p style={{ fontSize: 12, marginBottom: 12 }}>
          History now: <b>{cur.records}</b> records ({cur.closed} closed, {cur.open} open) ·{' '}
          {Object.entries(cur.by_source || {}).map(([k, v]) => (
            <span key={k} style={{ color: SOURCE_COLOR[k], marginRight: 8 }}>{k} {v}</span>
          ))}
        </p>
      )}

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
        <input
          type="text" value={label} maxLength={200} placeholder="Note (optional)" style={{ minWidth: 200, flex: 1 }}
          onChange={(e) => setLabel(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !busy) create() }}
        />
        <button onClick={create} disabled={busy}>Back up now</button>
        <button className="btn" onClick={load} disabled={busy}>Refresh</button>
      </div>

      <Notice notice={notice} />
      {error && <p style={{ color: 'var(--short)', fontSize: 13 }}>Failed: {error}</p>}
      {data && data.backups.length === 0 && <p style={small}>No backups yet.</p>}

      {data && data.backups.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr><th>Created</th><th>Kind</th><th>Records</th><th>Size</th><th>Note</th><th /></tr>
            </thead>
            <tbody>
              {data.backups.map((b) => (
                <React.Fragment key={b.id}>
                  <tr className={open === b.id ? 'selected' : ''} onClick={() => setOpen(open === b.id ? null : b.id)} style={{ cursor: 'pointer' }}>
                    <td>{fmtWhen(b.created_at)}</td>
                    <td>{KIND_LABEL[b.kind] || b.kind}</td>
                    <td>{b.records} <span style={small}>({b.closed} closed, {b.open} open)</span></td>
                    <td>{fmtSize(b.size_bytes)}</td>
                    <td style={{ maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis' }}>{b.label || '—'}</td>
                    <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }} onClick={(e) => e.stopPropagation()}>
                      <button className="btn" disabled={busy} onClick={() => run(() => api.downloadBackup(b.id), () => 'Download started.')}>Download</button>{' '}
                      <button className="btn danger" disabled={busy} onClick={() => remove(b.id)}>
                        {confirmDelete === b.id ? 'Sure?' : 'Delete'}
                      </button>
                    </td>
                  </tr>
                  {open === b.id && (
                    <tr>
                      <td colSpan={6}>
                        <div style={{ ...small, marginBottom: 4 }}>id <code>{b.id}</code></div>
                        <BackupDetail id={b.id} busy={busy} onRestore={(t, f) => restore(b.id, t, f)} />
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------
// Where the stored records came from
// ---------------------------------------------------------------------
function DuplicatesBlock({ refreshKey, onHistoryChanged }) {
  const [info, setInfo] = useState(null)
  const [notice, setNotice] = useState(null)
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState(false)

  const load = useCallback(() => {
    api.getHistoryDuplicates().then(setInfo).catch((e) => setNotice({ error: true, text: e.message }))
  }, [])
  useEffect(() => { load() }, [load, refreshKey])

  const remove = async () => {
    if (!confirm) { setConfirm(true); setTimeout(() => setConfirm(false), 4000); return }
    setConfirm(false); setBusy(true); setNotice(null)
    try {
      const r = await api.removeHistoryDuplicates()
      setNotice({ text: `Removed ${r.removed} duplicate(s). Safety backup: ${r.backup_id}.` })
      load(); onHistoryChanged()
    } catch (e) {
      setNotice({ error: true, text: e.message })
    } finally { setBusy(false) }
  }

  if (!info) return <Notice notice={notice} />
  return (
    <div style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--border-soft)' }}>
      {info.duplicates > 0 ? (
        <>
          <p style={{ fontSize: 13, marginBottom: 6, color: 'var(--accent)' }}>
            {info.duplicates} record(s) were added by an audit although the same signal was already stored
            ({info.rows.slice(0, 6).map((r) => `${r.ticker} ${r.tf} ${r.side}: ${r.n}`).join(' · ')}
            {info.rows.length > 6 ? ' …' : ''}).
          </p>
          <button className="btn danger" onClick={remove} disabled={busy}>
            {confirm ? 'Sure? Click again' : `Remove ${info.duplicates} duplicate(s)`}
          </button>
          <p style={{ ...small, marginTop: 6 }}>
            Only the audit-added copies are removed (tagged backtest_fill / backtest_backfill) — the original record always stays.
            A safety backup is taken first.
          </p>
        </>
      ) : (
        <p style={small}>No duplicate audit additions.</p>
      )}
      <Notice notice={notice} />
    </div>
  )
}

function OrderBlock({ refreshKey, onHistoryChanged }) {
  const [info, setInfo] = useState(null)
  const [notice, setNotice] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api.getHistoryOrder().then(setInfo).catch((e) => setNotice({ error: true, text: e.message }))
  }, [])
  useEffect(() => { load() }, [load, refreshKey])

  const sort = async () => {
    setBusy(true); setNotice(null)
    try {
      const r = await api.sortHistoryOrder()
      setNotice({ text: `Sorted ${r.sorted} list(s). Safety backup: ${r.backup_id}.` })
      load(); onHistoryChanged()
    } catch (e) {
      setNotice({ error: true, text: e.message })
    } finally { setBusy(false) }
  }

  if (!info) return <Notice notice={notice} />
  return (
    <div style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--border-soft)' }}>
      {info.slots > 0 ? (
        <>
          <p style={{ fontSize: 13, marginBottom: 6, color: 'var(--accent)' }}>
            {info.slots} list(s) are not in date order
            ({info.rows.slice(0, 6).map((r) => `${r.ticker} ${r.tf} ${r.side}`).join(' · ')}{info.rows.length > 6 ? ' …' : ''}).
          </p>
          <button onClick={sort} disabled={busy}>Sort by date</button>
          <p style={{ ...small, marginTop: 6 }}>Only reorders (oldest first) — nothing is added or removed. A safety backup is taken first.</p>
        </>
      ) : (
        <p style={small}>History is in date order.</p>
      )}
      <Notice notice={notice} />
    </div>
  )
}

function SourcesSection({ refreshKey, onHistoryChanged }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api.getHistorySources().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }, [])
  useEffect(() => { load() }, [load, refreshKey])

  const tag = async () => {
    setBusy(true); setNotice(null)
    try {
      const r = await api.tagHistorySources()
      setNotice({ text: `Tagged ${r.tagged} record(s); ${r.unknown} could not be told. Safety backup: ${r.backup_id}.` })
      load(); onHistoryChanged()
    } catch (e) {
      setNotice({ error: true, text: e.message })
    } finally { setBusy(false) }
  }

  return (
    <div className="panel">
      <h3 className="panel-title">Where the stored records came from</h3>
      <p style={{ ...small, marginBottom: 12 }}>
        Old records have no origin tag. It is inferred: a real (live) record is stamped with the moment the bot saw the
        signal, a backtest record with the exact open of the signal bar, and only live records carry fields such as
        <code> tp_raw</code> / <code>tp1_hit</code> / <code>raw_mfe_pct</code>. Tagging writes that inference into the records so it stays known.
      </p>
      {error && <p style={{ color: 'var(--short)', fontSize: 13 }}>Failed: {error}</p>}
      {data && (
        <>
          <p style={{ fontSize: 13, marginBottom: 8 }}>
            {Object.entries(data.total).map(([k, v]) => (
              <span key={k} style={{ color: SOURCE_COLOR[k], marginRight: 12 }}><b>{v}</b> {k}</span>
            ))}
            {Object.keys(data.total).length === 0 && <span style={small}>history is empty</span>}
          </p>
          <p style={{ ...small, marginBottom: 10 }}>
            {data.untagged > 0
              ? `${data.untagged} record(s) have no tag yet — origin inferred (${Object.entries(data.by_basis).filter(([k]) => k !== 'tag').map(([k, v]) => `${k} ${v}`).join(', ')}).`
              : 'Every record is tagged.'}
          </p>
          {data.untagged > 0 && <button onClick={tag} disabled={busy}>Tag untagged records</button>}
          <Notice notice={notice} />
          <DuplicatesBlock refreshKey={refreshKey} onHistoryChanged={onHistoryChanged} />
          <OrderBlock refreshKey={refreshKey} onHistoryChanged={onHistoryChanged} />
          {data.rows.length > 0 && (
            <details style={{ marginTop: 10 }}>
              <summary style={{ cursor: 'pointer', fontSize: 12 }}>Per pair / timeframe / side</summary>
              <div className="table-wrap" style={{ marginTop: 8 }}>
                <table className="data-table">
                  <thead><tr><th>Pair</th><th>TF</th><th>Side</th><th>Records</th><th>By origin</th></tr></thead>
                  <tbody>
                    {data.rows.map((r) => (
                      <tr key={`${r.ticker}-${r.tf}-${r.side}`}>
                        <td>{r.ticker}</td><td>{r.tf}</td><td>{r.side}</td><td>{r.n}</td>
                        <td>{Object.entries(r.counts).map(([k, v]) => (
                          <span key={k} style={{ color: SOURCE_COLOR[k], marginRight: 8 }}>{k} {v}</span>
                        ))}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------
// Backtest audit: compare a fresh backtest with the stored history
// ---------------------------------------------------------------------
function AuditSection({ pairs, timeframes, config, onConfigChanged, onHistoryChanged }) {
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(6000)
  const [applying, setApplying] = useState(false)
  const [notice, setNotice] = useState(null)
  const [applied, setApplied] = useState(false)
  const [keepDraft, setKeepDraft] = useState('')
  const sim = useSimJob('reconcile')
  const result = sim.job?.status === 'done' ? sim.job.result : null
  const ranBars = sim.job?.params?.num_bars ?? numBars
  const keepNow = config?.tp_config?.signal_history_keep_effective

  // a new finished audit re-enables "apply"
  const jobId = sim.job?.id
  useEffect(() => { setApplied(false); setNotice(null) }, [jobId])

  const start = () => {
    const bad = barsError(numBars)
    if (bad) { sim.setError(bad); return }
    setNotice(null)
    sim.start(() => api.runReconcile({ ticker, tf, numBars }))
  }

  const apply = async (fill, backfill) => {
    setApplying(true); setNotice(null)
    try {
      const r = await api.applyReconcile(result.apply_id, fill, backfill)
      setApplied(true)
      const parts = [`Added ${r.added} signal(s): ${r.filled} fill + ${r.backfilled} backfill. Safety backup: ${r.backup_id}.`]
      if (r.no_room) parts.push(`${r.no_room} more did not fit (${r.keep} stored per side) — raise the limit below and run the audit again.`)
      if (r.already_there) parts.push(`${r.already_there} were already stored.`)
      setNotice({ text: parts.join(' ') })
      onHistoryChanged()
    } catch (e) {
      setNotice({ error: true, text: e.message })
    } finally { setApplying(false) }
  }

  const saveKeep = async () => {
    const n = Number(keepDraft)
    if (!Number.isInteger(n) || n < 0 || n > 2000) { setNotice({ error: true, text: 'Enter a whole number 0–2000 (0 = automatic).' }); return }
    try {
      await api.setTpConfigKeep(n)
      setKeepDraft('')
      setNotice({ text: `Stored records per side set to ${n || 'automatic'}.` })
      onConfigChanged?.()
    } catch (e) {
      setNotice({ error: true, text: e.message })
    }
  }

  const tot = result?.totals
  const rows = []
  ;(result?.slots || []).forEach((sl) => sl.rows.forEach((r) => rows.push({ ticker: sl.ticker, tf: sl.tf, ...r })))

  return (
    <div className="panel">
      <h3 className="panel-title">Backtest audit — missed signals &amp; deeper history</h3>
      <p style={{ ...small, marginBottom: 12 }}>
        Replays the <b>current</b> signal logic over real exchange history (dry run — nothing is written) and compares each
        simulated signal with the stored history. <b>Missed</b> = inside the stored window but absent (fill candidates);
        <b> older</b> = before the oldest stored record (backfill candidates, to get more signals for analysis).
        Live records differ from a backtest by design (spread/derivatives gates, intrabar check, exact entry moment), so
        "live-only" is not an error. Adding is add-only: no stored record is removed or changed, and a backup is taken first.
      </p>
      <SimScopeFields
        pairs={pairs} timeframes={timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={<button onClick={start} disabled={sim.running}>{sim.running ? 'Running…' : 'Run audit'}</button>}
      />
      <SimStatus sim={sim} />
      <Notice notice={notice} />

      {result && !sim.running && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          <p style={{ fontSize: 13, marginBottom: 10 }}>
            Backtest signals <b>{tot.backtest}</b> · matched <b>{tot.matched}</b> ·{' '}
            <span style={{ color: tot.missed ? 'var(--accent)' : undefined }}>missed in window <b>{tot.missed}</b></span> ·{' '}
            <span style={{ color: tot.older ? 'var(--accent)' : undefined }}>older than window <b>{tot.older}</b></span>
            <br />
            <span style={small}>
              stored but not reproduced: live-only {tot.live_only} · stale backtest {tot.stale_backtest} · outside the tested range {tot.outside} · outcome changed {tot.changed}
            </span>
          </p>

          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr><th>Pair</th><th>TF</th><th>Side</th><th>Track</th><th>BT</th><th>Stored</th><th>Match</th><th>Missed</th><th>Older</th><th>Live-only</th><th>Stale</th><th>Changed</th></tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={`${r.ticker}-${r.tf}-${r.side}-${r.track}`}>
                    <td>{r.ticker}</td><td>{r.tf}</td><td>{r.side}</td><td>{r.track}</td>
                    <td>{r.backtest}</td><td>{r.stored}</td><td>{r.matched}</td>
                    <td style={r.missed ? { color: 'var(--accent)' } : undefined}>{r.missed}</td>
                    <td style={r.older ? { color: 'var(--accent)' } : undefined}>{r.older}</td>
                    <td>{r.live_only}</td><td>{r.stale_backtest}</td>
                    <td style={r.changed ? { color: 'var(--short)' } : undefined}>{r.changed}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {result.slots.some((sl) => sl.missed.length) && (
            <details style={{ marginTop: 10 }}>
              <summary style={{ cursor: 'pointer', fontSize: 12 }}>Missed signals (first 40 per pair/tf)</summary>
              {result.slots.map((sl) => sl.missed.length > 0 && (
                <div key={`${sl.ticker}-${sl.tf}`} style={{ fontSize: 12, marginTop: 6 }}>
                  <b>{sl.ticker} {sl.tf}</b>
                  {sl.missed.map((m, i) => (
                    <div key={i} style={{ color: 'var(--text-faint)' }}>
                      {fmtWhen(m.ts)} · {m.side} · track {m.track} → {m.exit_type} {Number.isFinite(m.moved_pct) ? `${m.moved_pct >= 0 ? '+' : ''}${m.moved_pct.toFixed(2)}%` : ''}
                    </div>
                  ))}
                </div>
              ))}
            </details>
          )}

          <div style={{ marginTop: 14, display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <button onClick={() => apply(true, false)} disabled={applying || applied || !tot.missed}>
              Add {tot.missed} missed
            </button>
            <button onClick={() => apply(false, true)} disabled={applying || applied || !tot.older}>
              Add {tot.older} older (deepen)
            </button>
            <button className="btn" onClick={() => apply(true, true)} disabled={applying || applied || !(tot.missed || tot.older)}>
              Add both
            </button>
            {applied && <span style={small}>Applied — run the audit again to see the new state.</span>}
          </div>
          <p style={{ ...small, marginTop: 8 }}>
            Added records are tagged <code>backtest_fill</code> / <code>backtest_backfill</code>, kept in time order, and have no
            <code> raw_mfe_pct</code> (a backtest does not track it) — so they are easy to exclude from analyses that need live data.
            Adaptive TP/SL calibration uses only the newest {config?.tp_config?.signal_history_limit ?? 'N'} records of each side.
          </p>
        </>
      )}

      <div style={{ marginTop: 16, paddingTop: 12, borderTop: '1px solid var(--border-soft)' }}>
        <div style={{ fontSize: 13, marginBottom: 6 }}>
          Stored records per side: <b>{keepNow ?? '…'}</b>{' '}
          <span style={small}>(0 = automatic, 3 × the training window). Older signals only fit if this is raised.</span>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <input type="number" min={0} max={2000} step={25} value={keepDraft} placeholder="e.g. 200" style={{ width: 110 }}
                 onChange={(e) => setKeepDraft(e.target.value)} />
          <button className="btn" onClick={saveKeep} disabled={keepDraft === ''}>Set</button>
        </div>
      </div>
    </div>
  )
}

export default function BackupPanel({ lastEvent, pairs, timeframes, config, onConfigChanged }) {
  // bumped whenever history changed under us (restore / fill / tag / live signal) so the origin counts refresh
  const [refreshKey, setRefreshKey] = useState(0)
  const bump = useCallback(() => setRefreshKey((k) => k + 1), [])
  useEffect(() => {
    if (lastEvent?.type === 'history_changed') bump()
  }, [lastEvent, bump])

  return (
    <div>
      <BackupsSection refreshKey={refreshKey} onHistoryChanged={bump} />
      <SourcesSection refreshKey={refreshKey} onHistoryChanged={bump} />
      <AuditSection
        pairs={pairs} timeframes={timeframes} config={config}
        onConfigChanged={onConfigChanged} onHistoryChanged={bump}
      />
    </div>
  )
}
