import React, { useEffect, useRef, useState } from 'react'

const KEY_LABEL = { a_long: 'A long', a_short: 'A short', u_long: 'U long', u_short: 'U short' }
const MODE_LABEL = { block_trend: 'off in TREND', block_all: 'off' }

function ago(iso) {
  if (!iso) return null
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return null
  const m = Math.max(0, Math.round((Date.now() - t) / 60000))
  if (m < 1) return 'just now'
  if (m < 60) return `${m} min ago`
  const h = Math.round(m / 60)
  if (h < 48) return `${h} h ago`
  return `${Math.round(h / 24)} d ago`
}

/**
 * 🆕 "SHDW" lamp in the top bar — shows that Entry switches (Settings → Entry)
 * are active and that their blocked signals are being tracked as shadow
 * positions (app/shadow.py), so it's visible at a glance instead of only in
 * the logs.
 *
 *   hidden       no Entry switch active and nothing ever recorded
 *   dim          switches are off again, but earlier shadow records remain
 *   amber        switch(es) active, waiting for the first blocked signal
 *   amber+glow   tracking: at least one shadow position is open right now
 *   flash        a new shadow record just appeared since the last refresh
 *
 * Clicking it opens the Insights tab (Components → "Include shadow").
 */
export default function ShadowLamp({ shadow, onClick }) {
  const total = shadow ? shadow.open + shadow.closed : 0
  const prevTotal = useRef(null)
  const [flash, setFlash] = useState(false)

  useEffect(() => {
    if (!shadow) return undefined
    const was = prevTotal.current
    prevTotal.current = total
    if (was !== null && total > was) {
      setFlash(true)
      const id = setTimeout(() => setFlash(false), 6000)
      return () => clearTimeout(id)
    }
    return undefined
  }, [total, shadow])

  if (!shadow) return null
  const blocks = Object.entries(shadow.blocks || {})
  if (blocks.length === 0 && total === 0) return null

  const active = blocks.length > 0
  const color = active ? 'var(--accent)' : 'var(--text-dim)'
  const glow = active && shadow.open > 0

  const lines = []
  lines.push(
    active
      ? `Entry switches active: ${blocks.map(([k, m]) => `${KEY_LABEL[k] || k} ${MODE_LABEL[m] || m}`).join(', ')}`
      : 'No Entry switch active right now (earlier shadow records are kept)',
  )
  if (total === 0) {
    lines.push('No blocked signal recorded yet — waiting for the first one')
  } else {
    lines.push(`Shadow positions: ${shadow.open} open · ${shadow.closed} closed`)
    Object.entries(shadow.by_key || {}).forEach(([k, b]) => {
      const wr = b.closed ? ` · ${Math.round((b.wins / b.closed) * 100)}% wins` : ''
      lines.push(`  ${KEY_LABEL[k] || k}: ${b.open} open, ${b.closed} closed${wr}`)
    })
    const last = ago(shadow.last_recorded)
    if (last) lines.push(`Last recorded ${last}`)
  }
  lines.push('Click → Insights (tick “Include shadow”)')

  return (
    <div
      className={`lamp shadow-lamp${flash ? ' lamp-flash' : ''}`}
      title={lines.join('\n')}
      onClick={onClick}
      style={{ cursor: onClick ? 'pointer' : 'default' }}
    >
      <span className="lamp-dot" style={{ background: color, boxShadow: glow ? `0 0 6px ${color}` : 'none' }} />
      <span className="lamp-label" style={{ color: active ? 'var(--accent)' : undefined }}>
        SHDW{shadow.open > 0 ? ` ${shadow.open}` : ''}
      </span>
    </div>
  )
}
