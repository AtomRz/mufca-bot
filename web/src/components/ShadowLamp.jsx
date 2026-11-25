import React, { useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n'

const KEY_LABEL = { a_long: 'A long', a_short: 'A short', u_long: 'U long', u_short: 'U short' }
const MODE_LABEL = { block_trend: 'off in TREND', block_all: 'off' }

function ago(iso, t) {
  if (!iso) return null
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return null
  const m = Math.max(0, Math.round((Date.now() - ts) / 60000))
  if (m < 1) return t('just now')
  if (m < 60) return t('{n} min ago', { n: m })
  const h = Math.round(m / 60)
  if (h < 48) return t('{n} h ago', { n: h })
  return t('{n} d ago', { n: Math.round(h / 24) })
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
  const { t } = useI18n()
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
  const overrideBlocks = shadow.block_overrides || []
  if (blocks.length === 0 && overrideBlocks.length === 0 && total === 0) return null

  const active = blocks.length > 0 || overrideBlocks.length > 0
  const color = active ? 'var(--accent)' : 'var(--text-dim)'
  const glow = active && shadow.open > 0

  const lines = []
  const keyLabel = (k) => (KEY_LABEL[k] ? t(KEY_LABEL[k]) : k)
  const modeLabel = (m) => (MODE_LABEL[m] ? t(MODE_LABEL[m]) : m)
  lines.push(
    active
      ? t('Entry switches active: {list}', { list: blocks.length ? blocks.map(([k, m]) => `${keyLabel(k)} ${modeLabel(m)}`).join(', ') : t('none global') })
      : t('No Entry switch active right now (earlier shadow records are kept)'),
  )
  overrideBlocks.forEach((o) => {
    lines.push(`  ${o.ticker} ${o.timeframe}: ${keyLabel(o.key)} ${modeLabel(o.mode)}`)
  })
  if (total === 0) {
    lines.push(t('No blocked signal recorded yet — waiting for the first one'))
  } else {
    lines.push(t('Shadow positions: {open} open · {closed} closed', { open: shadow.open, closed: shadow.closed }))
    Object.entries(shadow.by_key || {}).forEach(([k, b]) => {
      const wr = b.closed ? ` · ${t('{pct}% wins', { pct: Math.round((b.wins / b.closed) * 100) })}` : ''
      lines.push(`  ${keyLabel(k)}: ${t('{open} open, {closed} closed', { open: b.open, closed: b.closed })}${wr}`)
    })
    const last = ago(shadow.last_recorded, t)
    if (last) lines.push(t('Last recorded {when}', { when: last }))
  }
  lines.push(t('Click → Insights (tick “Include shadow”)'))

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
