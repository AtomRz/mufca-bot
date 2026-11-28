import React, { useEffect, useState, useCallback } from 'react'
import { api } from '../api'
import { useI18n } from '../i18n'
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
  'pre-rawfill': 'before raw_mfe fill',
  'pre-compfill': 'before components fill',
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

function fmtWhen(iso, locale) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  return d.toLocaleString(locale, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function fmtSize(bytes) {
  if (!Number.isFinite(bytes)) return '—'
  return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`
}

// Fills {name} placeholders of an already translated sentence with React nodes (<b>, <code>, ...).
function rich(text, nodes) {
  return text.split(/(\{\w+\})/).map((part, i) => {
    const m = part.match(/^\{(\w+)\}$/)
    return m && nodes[m[1]] !== undefined ? <React.Fragment key={i}>{nodes[m[1]]}</React.Fragment> : part
  })
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
  const { t } = useI18n()
  const [detail, setDetail] = useState(null)
  const [error, setError] = useState(null)
  const [confirm, setConfirm] = useState(null) // {ticker, tf} waiting for the second click

  useEffect(() => {
    let alive = true
    api.getBackupDetail(id).then((d) => alive && setDetail(d)).catch((e) => alive && setError(e.message))
    return () => { alive = false }
  }, [id])

  if (error) return <p style={{ color: 'var(--short)', fontSize: 12 }}>{error}</p>
  if (!detail) return <p style={small}>{t('Loading…')}</p>

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
  const label = (ticker, tf, text) => (confirm === `${ticker || ''}|${tf || ''}` ? t('Sure? Click again') : text)

  return (
    <div style={{ marginTop: 8 }}>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr><th>{t('Pair')}</th><th>{t('TF')}</th><th>{t('Records now')}</th><th>{t('In backup')}</th><th>{t('Live now → backup')}</th><th /></tr>
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
                    {label(c.ticker, c.tf, t('Restore this'))}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p style={{ ...small, marginTop: 8 }}>
        {t('Restore replaces the stored records with the backup\'s. Open trades are never touched, and a safety copy of the current history is saved first, so a restore can be undone from this list.')}
      </p>
      <button className="btn danger" disabled={busy} onClick={() => ask(null, null)} style={{ marginTop: 6 }}>
        {label(null, null, t('Restore ALL pairs from this backup'))}
      </button>
    </div>
  )
}

function BackupsSection({ refreshKey, onHistoryChanged }) {
  const { t, locale } = useI18n()
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
      setNotice({ text: okText ? okText(res) : t('Done') })
      load()
      return res
    } catch (e) {
      setNotice({ error: true, text: e.message })
    } finally {
      setBusy(false)
    }
  }

  const create = async () => {
    const res = await run(() => api.createBackup(label), (m) => t('Backup saved: {records} records.', { records: m.records }))
    if (res) setLabel('')
  }

  const restore = async (id, ticker, tf) => {
    const res = await run(
      () => api.restoreBackup(id, ticker, tf),
      (r) => t('Restored {records} records ({kept} open kept). Undo: restore the "before restore" backup {id}.', { records: r.records, kept: r.open_kept, id: r.pre_restore_backup }),
    )
    if (res) onHistoryChanged()
  }

  const remove = async (id) => {
    if (confirmDelete !== id) { setConfirmDelete(id); setTimeout(() => setConfirmDelete((c) => (c === id ? null : c)), 4000); return }
    setConfirmDelete(null)
    if (open === id) setOpen(null)
    await run(() => api.deleteBackup(id), () => t('Backup deleted.'))
  }

  const cur = data?.current

  return (
    <div className="panel">
      <h3 className="panel-title">{t('Saved backups of the signal history')}</h3>
      <p style={{ ...small, marginBottom: 12 }}>
        {rich(t('A backup is a full copy of the signal history. One is taken automatically on start and then every {hours} h when something changed; a safety copy is also taken right before a restore, a fill, a backfill, tagging, a {reset} or a delete. The newest {keep} automatic/safety copies are kept; manual ones are never deleted automatically. They live in {dir} — the same volume as the history, so use Download for a copy that survives losing the volume.'), {
          hours: data?.interval_hours ?? '…',
          reset: <code>!reset</code>,
          keep: data?.keep_auto ?? '…',
          dir: <code>{data?.dir ?? 'data/backups'}</code>,
        })}
      </p>

      {cur && (
        <p style={{ fontSize: 12, marginBottom: 12 }}>
          {rich(t('History now: {records} records ({closed} closed, {open} open) ·'), { records: <b>{cur.records}</b>, closed: cur.closed, open: cur.open })}{' '}
          {Object.entries(cur.by_source || {}).map(([k, v]) => (
            <span key={k} style={{ color: SOURCE_COLOR[k], marginRight: 8 }}>{k} {v}</span>
          ))}
        </p>
      )}

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
        <input
          type="text" value={label} maxLength={200} placeholder={t('Note (optional)')} style={{ minWidth: 200, flex: 1 }}
          onChange={(e) => setLabel(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !busy) create() }}
        />
        <button onClick={create} disabled={busy}>{t('Back up now')}</button>
        <button className="btn" onClick={load} disabled={busy}>{t('Refresh')}</button>
      </div>

      <Notice notice={notice} />
      {error && <p style={{ color: 'var(--short)', fontSize: 13 }}>{t('Failed: {error}', { error })}</p>}
      {data && data.backups.length === 0 && <p style={small}>{t('No backups yet.')}</p>}

      {data && data.backups.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr><th>{t('Created')}</th><th>{t('Kind')}</th><th>{t('Records')}</th><th>{t('Size')}</th><th>{t('Note')}</th><th /></tr>
            </thead>
            <tbody>
              {data.backups.map((b) => (
                <React.Fragment key={b.id}>
                  <tr className={open === b.id ? 'selected' : ''} onClick={() => setOpen(open === b.id ? null : b.id)} style={{ cursor: 'pointer' }}>
                    <td>{fmtWhen(b.created_at, locale)}</td>
                    <td>{KIND_LABEL[b.kind] ? t(KIND_LABEL[b.kind]) : b.kind}</td>
                    <td>{b.records} <span style={small}>{t('({closed} closed, {open} open)', { closed: b.closed, open: b.open })}</span></td>
                    <td>{fmtSize(b.size_bytes)}</td>
                    <td style={{ maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis' }}>{b.label || '—'}</td>
                    <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }} onClick={(e) => e.stopPropagation()}>
                      <button className="btn" disabled={busy} onClick={() => run(() => api.downloadBackup(b.id), () => t('Download started.'))}>{t('Download')}</button>{' '}
                      <button className="btn danger" disabled={busy} onClick={() => remove(b.id)}>
                        {confirmDelete === b.id ? t('Sure?') : t('Delete')}
                      </button>
                    </td>
                  </tr>
                  {open === b.id && (
                    <tr>
                      <td colSpan={6}>
                        <div style={{ ...small, marginBottom: 4 }}>{rich(t('id {id}'), { id: <code>{b.id}</code> })}</div>
                        <BackupDetail id={b.id} busy={busy} onRestore={(tk, f) => restore(b.id, tk, f)} />
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
  const { t } = useI18n()
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
      setNotice({ text: t('Removed {n} duplicate(s). Safety backup: {id}.', { n: r.removed, id: r.backup_id }) })
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
            {t('{n} record(s) were added by an audit although the same signal was already stored ({list}{more}).', {
              n: info.duplicates,
              list: info.rows.slice(0, 6).map((r) => `${r.ticker} ${r.tf} ${r.side}: ${r.n}`).join(' · '),
              more: info.rows.length > 6 ? ' …' : '',
            })}
          </p>
          <button className="btn danger" onClick={remove} disabled={busy}>
            {confirm ? t('Sure? Click again') : t('Remove {n} duplicate(s)', { n: info.duplicates })}
          </button>
          <p style={{ ...small, marginTop: 6 }}>
            {t('Only the audit-added copies are removed (tagged backtest_fill / backtest_backfill) — the original record always stays. A safety backup is taken first.')}
          </p>
        </>
      ) : (
        <p style={small}>{t('No duplicate audit additions.')}</p>
      )}
      <Notice notice={notice} />
    </div>
  )
}

function OrderBlock({ refreshKey, onHistoryChanged }) {
  const { t } = useI18n()
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
      setNotice({ text: t('Sorted {n} list(s). Safety backup: {id}.', { n: r.sorted, id: r.backup_id }) })
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
            {t('{n} list(s) are not in date order ({list}{more}).', {
              n: info.slots,
              list: info.rows.slice(0, 6).map((r) => `${r.ticker} ${r.tf} ${r.side}`).join(' · '),
              more: info.rows.length > 6 ? ' …' : '',
            })}
          </p>
          <button onClick={sort} disabled={busy}>{t('Sort by date')}</button>
          <p style={{ ...small, marginTop: 6 }}>{t('Only reorders (oldest first) — nothing is added or removed. A safety backup is taken first.')}</p>
        </>
      ) : (
        <p style={small}>{t('History is in date order.')}</p>
      )}
      <Notice notice={notice} />
    </div>
  )
}

function SourcesSection({ refreshKey, onHistoryChanged }) {
  const { t } = useI18n()
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
      setNotice({ text: t('Tagged {tagged} record(s); {unknown} could not be told. Safety backup: {id}.', { tagged: r.tagged, unknown: r.unknown, id: r.backup_id }) })
      load(); onHistoryChanged()
    } catch (e) {
      setNotice({ error: true, text: e.message })
    } finally { setBusy(false) }
  }

  return (
    <div className="panel">
      <h3 className="panel-title">{t('Where the stored records came from')}</h3>
      <p style={{ ...small, marginBottom: 12 }}>
        {rich(t('Old records have no origin tag. It is inferred: a real (live) record is stamped with the moment the bot saw the signal, a backtest record with the exact open of the signal bar, and only live records carry fields such as {fields}. Tagging writes that inference into the records so it stays known.'), {
          fields: <><code>tp_raw</code> / <code>tp1_hit</code></>,
        })}
      </p>
      {error && <p style={{ color: 'var(--short)', fontSize: 13 }}>{t('Failed: {error}', { error })}</p>}
      {data && (
        <>
          <p style={{ fontSize: 13, marginBottom: 8 }}>
            {Object.entries(data.total).map(([k, v]) => (
              <span key={k} style={{ color: SOURCE_COLOR[k], marginRight: 12 }}><b>{v}</b> {k}</span>
            ))}
            {Object.keys(data.total).length === 0 && <span style={small}>{t('history is empty')}</span>}
          </p>
          <p style={{ ...small, marginBottom: 10 }}>
            {data.untagged > 0
              ? t('{n} record(s) have no tag yet — origin inferred ({basis}).', { n: data.untagged, basis: Object.entries(data.by_basis).filter(([k]) => k !== 'tag').map(([k, v]) => `${k} ${v}`).join(', ') })
              : t('Every record is tagged.')}
          </p>
          {data.untagged > 0 && <button onClick={tag} disabled={busy}>{t('Tag untagged records')}</button>}
          <Notice notice={notice} />
          <DuplicatesBlock refreshKey={refreshKey} onHistoryChanged={onHistoryChanged} />
          <OrderBlock refreshKey={refreshKey} onHistoryChanged={onHistoryChanged} />
          {data.rows.length > 0 && (
            <details style={{ marginTop: 10 }}>
              <summary style={{ cursor: 'pointer', fontSize: 12 }}>{t('Per pair / timeframe / side')}</summary>
              <div className="table-wrap" style={{ marginTop: 8 }}>
                <table className="data-table">
                  <thead><tr><th>{t('Pair')}</th><th>{t('TF')}</th><th>{t('Side')}</th><th>{t('Records')}</th><th>{t('By origin')}</th></tr></thead>
                  <tbody>
                    {data.rows.map((r) => (
                      <tr key={`${r.ticker}-${r.tf}-${r.side}`}>
                        <td>{r.ticker}</td><td>{r.tf}</td><td>{t(r.side)}</td><td>{r.n}</td>
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
  const { t, locale } = useI18n()
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
      const parts = [t('Added {added} signal(s): {filled} fill + {backfilled} backfill. Safety backup: {id}.', { added: r.added, filled: r.filled, backfilled: r.backfilled, id: r.backup_id })]
      if (r.no_room) parts.push(t('{n} more did not fit ({keep} stored per side) — raise the limit below and run the audit again.', { n: r.no_room, keep: r.keep }))
      if (r.already_there) parts.push(t('{n} were already stored.', { n: r.already_there }))
      setNotice({ text: parts.join(' ') })
      onHistoryChanged()
    } catch (e) {
      setNotice({ error: true, text: e.message })
    } finally { setApplying(false) }
  }

  const saveKeep = async () => {
    const n = Number(keepDraft)
    if (!Number.isInteger(n) || n < 0 || n > 2000) { setNotice({ error: true, text: t('Enter a whole number 0–2000 (0 = automatic).') }); return }
    try {
      await api.setTpConfigKeep(n)
      setKeepDraft('')
      setNotice({ text: t('Stored records per side set to {n}.', { n: n || t('automatic') }) })
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
      <h3 className="panel-title">{t('Backtest audit — missed signals & deeper history')}</h3>
      <p style={{ ...small, marginBottom: 12 }}>
        {rich(t('Replays the {current} signal logic over real exchange history (dry run — nothing is written) and compares each simulated signal with the stored history. {missed} = inside the stored window but absent (fill candidates); {older} = before the oldest stored record (backfill candidates, to get more signals for analysis). Live records differ from a backtest by design (spread/derivatives gates, intrabar check, exact entry moment), so "live-only" is not an error. Adding is add-only: no stored record is removed or changed, and a backup is taken first.'), {
          current: <b>{t('current')}</b>,
          missed: <b>{t('Missed')}</b>,
          older: <b>{t('older')}</b>,
        })}
      </p>
      <SimScopeFields
        pairs={pairs} timeframes={timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={<button onClick={start} disabled={sim.running}>{sim.running ? t('Running…') : t('Run audit')}</button>}
      />
      <SimStatus sim={sim} />
      <Notice notice={notice} />

      {result && !sim.running && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          <p style={{ fontSize: 13, marginBottom: 10 }}>
            {rich(t('Backtest signals {n}'), { n: <b>{tot.backtest}</b> })} · {rich(t('matched {n}'), { n: <b>{tot.matched}</b> })} ·{' '}
            <span style={{ color: tot.missed ? 'var(--accent)' : undefined }}>{rich(t('missed in window {n}'), { n: <b>{tot.missed}</b> })}</span> ·{' '}
            <span style={{ color: tot.older ? 'var(--accent)' : undefined }}>{rich(t('older than window {n}'), { n: <b>{tot.older}</b> })}</span>
            <br />
            <span style={small}>
              {t('stored but not reproduced: live-only {liveOnly} · stale backtest {stale} · outside the tested range {outside} · outcome changed {changed}', { liveOnly: tot.live_only, stale: tot.stale_backtest, outside: tot.outside, changed: tot.changed })}
            </span>
          </p>

          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr><th>{t('Pair')}</th><th>{t('TF')}</th><th>{t('Side')}</th><th>{t('Track')}</th><th>{t('BT')}</th><th>{t('Stored')}</th><th>{t('Match')}</th><th>{t('Missed')}</th><th>{t('Older')}</th><th>{t('Live-only')}</th><th>{t('Stale')}</th><th>{t('Changed')}</th></tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={`${r.ticker}-${r.tf}-${r.side}-${r.track}`}>
                    <td>{r.ticker}</td><td>{r.tf}</td><td>{t(r.side)}</td><td>{r.track}</td>
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
              <summary style={{ cursor: 'pointer', fontSize: 12 }}>{t('Missed signals (first 40 per pair/tf)')}</summary>
              {result.slots.map((sl) => sl.missed.length > 0 && (
                <div key={`${sl.ticker}-${sl.tf}`} style={{ fontSize: 12, marginTop: 6 }}>
                  <b>{sl.ticker} {sl.tf}</b>
                  {sl.missed.map((m, i) => (
                    <div key={i} style={{ color: 'var(--text-faint)' }}>
                      {fmtWhen(m.ts, locale)} · {t(m.side)} · {t('track {track}', { track: m.track })} → {m.exit_type} {Number.isFinite(m.moved_pct) ? `${m.moved_pct >= 0 ? '+' : ''}${m.moved_pct.toFixed(2)}%` : ''}
                    </div>
                  ))}
                </div>
              ))}
            </details>
          )}

          <div style={{ marginTop: 14, display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <button onClick={() => apply(true, false)} disabled={applying || applied || !tot.missed}>
              {t('Add {n} missed', { n: tot.missed })}
            </button>
            <button onClick={() => apply(false, true)} disabled={applying || applied || !tot.older}>
              {t('Add {n} older (deepen)', { n: tot.older })}
            </button>
            <button className="btn" onClick={() => apply(true, true)} disabled={applying || applied || !(tot.missed || tot.older)}>
              {t('Add both')}
            </button>
            {applied && <span style={small}>{t('Applied — run the audit again to see the new state.')}</span>}
          </div>
          <p style={{ ...small, marginTop: 8 }}>
            {rich(t('Added records are tagged {tags} and kept in time order, so they are easy to exclude from analyses that need live data. They carry {field} computed from the bars after entry. Adaptive TP/SL calibration uses only the newest {n} records of each side.'), {
              tags: <><code>backtest_fill</code> / <code>backtest_backfill</code></>,
              field: <code>raw_mfe_pct</code>,
              n: config?.tp_config?.signal_history_limit ?? 'N',
            })}
          </p>
        </>
      )}

      <div style={{ marginTop: 16, paddingTop: 12, borderTop: '1px solid var(--border-soft)' }}>
        <div style={{ fontSize: 13, marginBottom: 6 }}>
          {rich(t('Stored records per side: {n}'), { n: <b>{keepNow ?? '…'}</b> })}{' '}
          <span style={small}>{t('(0 = automatic, 3 × the training window). Older signals only fit if this is raised.')}</span>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <input type="number" min={0} max={2000} step={25} value={keepDraft} placeholder={t('e.g. 200')} style={{ width: 110 }}
                 onChange={(e) => setKeepDraft(e.target.value)} />
          <button className="btn" onClick={saveKeep} disabled={keepDraft === ''}>{t('Set')}</button>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------
// raw_mfe / raw_mae for stored backtest records
// ---------------------------------------------------------------------
function RawFillSection({ pairs, timeframes }) {
  const { t } = useI18n()
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(6000)
  const sim = useSimJob('rawfill')
  const result = sim.job?.status === 'done' ? sim.job.result : null
  const ran = sim.job?.params
  const ranBars = ran?.num_bars ?? numBars
  const tot = result?.totals

  const start = (apply) => {
    const bad = barsError(numBars)
    if (bad) { sim.setError(bad); return }
    sim.start(() => api.runRawFill({ ticker, tf, numBars, apply }))
  }
  // writing re-uses the scope that was just previewed, so what is written is what was shown
  const write = () => sim.start(() => api.runRawFill({ ticker: ran?.ticker || '', tf: ran?.tf || '', numBars: ran?.num_bars ?? numBars, apply: true }))

  return (
    <div className="panel">
      <h3 className="panel-title">{t('raw_mfe for backtest records')}</h3>
      <p style={{ ...small, marginBottom: 12 }}>
        {rich(t('{fields} is how far price really went over the next {n} bars after entry, whatever TP/SL closed the trade — the number the Components analysis trusts. New backtest records get it automatically; this fills the ones already stored (the first bootstrap and anything added by the audit) from real exchange bars. {check} only reports. {write} adds just those two fields where missing, after a safety backup — live records and the fields adaptive TP/SL reads are never touched. Fetch enough bars to reach back past your oldest stored record.'), {
          fields: <><code>raw_mfe</code> / <code>raw_mae</code></>,
          n: result?.max_hold_bars ?? 'MAX_HOLD',
          check: <b>{t('Check')}</b>,
          write: <b>{t('Write')}</b>,
        })}
      </p>
      <SimScopeFields
        pairs={pairs} timeframes={timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={<button onClick={() => start(false)} disabled={sim.running}>{sim.running ? t('Running…') : t('Check')}</button>}
      />
      <SimStatus sim={sim} />

      {result && !sim.running && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          <p style={{ fontSize: 13, marginBottom: 10 }}>
            {rich(t('Backtest records without it {n}'), { n: <b>{tot.candidates}</b> })} ·{' '}
            <span style={{ color: result.applied ? 'var(--long)' : tot.filled ? 'var(--accent)' : undefined }}>
              {rich(result.applied ? t('filled {n}') : t('can be filled {n}'), { n: <b>{tot.filled}</b> })}
            </span>{' '}· {t('already had it {n}', { n: tot.already })}
            <br />
            <span style={small}>
              {t('skipped: no bars {noBars} (older than the fetched history) · window not complete yet {incomplete} · entry does not match the bar {mismatch}', { noBars: tot.no_bars, incomplete: tot.incomplete, mismatch: tot.mismatch })}
            </span>
          </p>
          {result.slots.length > 0 && (
            <div className="table-wrap">
              <table className="data-table">
                <thead><tr><th>{t('Pair')}</th><th>{t('TF')}</th><th>{t('To fill')}</th><th>{t('OK')}</th><th>{t('No bars')}</th><th>{t('Incomplete')}</th><th>{t('Mismatch')}</th></tr></thead>
                <tbody>
                  {result.slots.filter((s) => s.candidates).map((s) => (
                    <tr key={`${s.ticker}-${s.tf}`}>
                      <td>{s.ticker}</td><td>{s.tf}</td><td>{s.candidates}</td><td>{s.filled}</td>
                      <td style={s.no_bars ? { color: 'var(--accent)' } : undefined}>{s.no_bars}</td>
                      <td>{s.incomplete}</td>
                      <td style={s.mismatch ? { color: 'var(--short)' } : undefined}>{s.mismatch}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {result.applied ? (
            <p style={{ fontSize: 13, marginTop: 10, color: 'var(--long)' }}>
              {t('Written. Safety backup: {id}. Open Insights → Components → Backtest only to see it.', { id: result.backup_id })}
            </p>
          ) : (
            <div style={{ marginTop: 12 }}>
              <button onClick={write} disabled={sim.running || !tot.filled}>{t('Write to {n} record(s)', { n: tot.filled })}</button>
              {tot.no_bars > 0 && <span style={{ ...small, marginLeft: 10 }}>{t('Raise the bars above to reach the {n} older record(s).', { n: tot.no_bars })}</span>}
            </div>
          )}
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------
// confidence components for stored backtest records
// ---------------------------------------------------------------------
function CompFillSection({ pairs, timeframes }) {
  const { t } = useI18n()
  const [ticker, setTicker] = useState('')
  const [tf, setTf] = useState('')
  const [numBars, setNumBars] = useState(6000)
  const sim = useSimJob('compfill')
  const result = sim.job?.status === 'done' ? sim.job.result : null
  const ran = sim.job?.params
  const ranBars = ran?.num_bars ?? numBars
  const tot = result?.totals

  const start = () => {
    const bad = barsError(numBars)
    if (bad) { sim.setError(bad); return }
    sim.start(() => api.runCompFill({ ticker, tf, numBars, apply: false }))
  }
  // writing re-uses the scope that was just previewed, so what is written is what was shown
  const write = () => sim.start(() => api.runCompFill({ ticker: ran?.ticker || '', tf: ran?.tf || '', numBars: ran?.num_bars ?? numBars, apply: true }))

  return (
    <div className="panel">
      <h3 className="panel-title">{t('Components for backtest records')}</h3>
      <p style={{ ...small, marginBottom: 12 }}>
        {rich(t('The stored backtest records (the first bootstrap and anything added by the audit) were saved without their confidence components, so the Components analysis (Backtest only) has nothing to group by. This replays the {current} logic over real exchange history, like Simulate does, and gives each stored backtest record the components of the simulated signal at the same bar. A record the current logic no longer produces stays empty — it is never guessed. The components are what today\'s logic and settings score that bar with. {check} only reports. {write} adds just {field} where missing, after a safety backup — live records and the fields adaptive TP/SL reads are never touched. Heavy: it runs a backtest per pair and timeframe.'), {
          current: <b>{t('current')}</b>,
          check: <b>{t('Check')}</b>,
          write: <b>{t('Write')}</b>,
          field: <code>confidence_components</code>,
        })}
      </p>
      <SimScopeFields
        pairs={pairs} timeframes={timeframes} ticker={ticker} setTicker={setTicker} tf={tf} setTf={setTf}
        numBars={numBars} setNumBars={setNumBars}
        extra={<button onClick={start} disabled={sim.running}>{sim.running ? t('Running…') : t('Check')}</button>}
      />
      <SimStatus sim={sim} />

      {result && !sim.running && (
        <>
          <BarCoverageWarning coverage={result.bar_coverage} requested={ranBars} />
          <p style={{ fontSize: 13, marginBottom: 10 }}>
            {rich(t('Backtest records without components {n}'), { n: <b>{tot.candidates}</b> })} ·{' '}
            <span style={{ color: result.applied ? 'var(--long)' : tot.filled ? 'var(--accent)' : undefined }}>
              {rich(result.applied ? t('filled {n}') : t('can be filled {n}'), { n: <b>{tot.filled}</b> })}
            </span>{' '}· {t('already had them {n}', { n: tot.already })}
            <br />
            <span style={small}>
              {t('skipped: no simulated signal at that bar {unmatched} (the current logic does not produce it, or it is outside the fetched bars) · entry does not match {mismatch}', { unmatched: tot.unmatched, mismatch: tot.mismatch })}
            </span>
          </p>
          {result.slots.some((s) => s.candidates) && (
            <div className="table-wrap">
              <table className="data-table">
                <thead><tr><th>{t('Pair')}</th><th>{t('TF')}</th><th>{t('To fill')}</th><th>{t('OK')}</th><th>{t('Unmatched')}</th><th>{t('Mismatch')}</th></tr></thead>
                <tbody>
                  {result.slots.filter((s) => s.candidates).map((s) => (
                    <tr key={`${s.ticker}-${s.tf}`}>
                      <td>{s.ticker}</td><td>{s.tf}</td><td>{s.candidates}</td><td>{s.filled}</td>
                      <td style={s.unmatched ? { color: 'var(--accent)' } : undefined}>{s.unmatched}</td>
                      <td style={s.mismatch ? { color: 'var(--short)' } : undefined}>{s.mismatch}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {result.applied ? (
            <p style={{ fontSize: 13, marginTop: 10, color: 'var(--long)' }}>
              {t('Written. Safety backup: {id}. Open Insights → Components → Backtest only to see it.', { id: result.backup_id })}
            </p>
          ) : (
            <div style={{ marginTop: 12 }}>
              <button onClick={write} disabled={sim.running || !tot.filled}>{t('Write to {n} record(s)', { n: tot.filled })}</button>
              {tot.unmatched > 0 && <span style={{ ...small, marginLeft: 10 }}>{t('More bars reach older records; the rest are signals today\'s logic would not give.')}</span>}
            </div>
          )}
        </>
      )}
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
      <RawFillSection pairs={pairs} timeframes={timeframes} />
      <CompFillSection pairs={pairs} timeframes={timeframes} />
    </div>
  )
}
