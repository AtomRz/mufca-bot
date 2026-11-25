import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { useI18n } from '../i18n'

/**
 * "NYSE" lamp in the top bar — which part of the New York trading day it is.
 * Informational only: nothing in the signal logic reads it. Data comes from
 * GET /api/nyse (app/market_hours.py), which already accounts for exchange
 * holidays, early closes and US daylight saving.
 *
 *   blue    pre-market    04:00-09:30 ET
 *   green   regular hours 09:30-16:00 ET (13:00 on early-close days)
 *   yellow  after-hours   16:00-20:00 ET (17:00 on early-close days)
 *   dim     closed        overnight, weekends, holidays
 *
 * The tooltip says what the state is and when it changes, in your local time
 * and in ET. The lamp refetches right after each scheduled change, once a
 * minute as a safety net, and whenever the tab becomes visible again.
 */

const STATE = {
  pre: { color: 'var(--nyse-pre)', label: 'Pre-market' },
  open: { color: 'var(--long)', label: 'Open' },
  after: { color: 'var(--nyse-after)', label: 'After-hours' },
  closed: { color: 'var(--text-dim)', label: 'Closed' },
}

const NEXT_EVENT = {
  pre: 'Pre-market starts',
  open: 'Market opens',
  after: 'Market closes',
  closed: 'After-hours end',
}

const hhmm = (d, locale, timeZone) =>
  d.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit', hour12: false, timeZone })

function untilText(ms, t) {
  const min = Math.max(1, Math.round(ms / 60000))
  const h = Math.floor(min / 60)
  if (h >= 48) return t('{d} d {h} h', { d: Math.floor(h / 24), h: h % 24 })
  if (h >= 1) return t('{h} h {m} min', { h, m: min % 60 })
  return t('{n} min', { n: min })
}

function tooltipLines(nyse, t, locale) {
  if (nyse.state === 'unknown') return [t('NYSE status unavailable')]

  const info = STATE[nyse.state] || STATE.closed
  let head = t('NYSE: {state}', { state: t(info.label) })
  if (nyse.state === 'closed') {
    if (nyse.closed_reason === 'holiday') head += ` — ${nyse.holiday_name || t('holiday')}`
    else if (nyse.closed_reason === 'weekend') head += ` — ${t('weekend')}`
  }
  if (nyse.early_close && nyse.state !== 'closed') head += ` ${t('(early close today)')}`
  const lines = [head]

  if (nyse.next_change_at) {
    const at = new Date(nyse.next_change_at)
    if (!Number.isNaN(at.getTime())) {
      const sameDay = at.toDateString() === new Date().toDateString()
      const day = sameDay ? '' : `${at.toLocaleDateString(locale, { weekday: 'short' })} `
      const note = nyse.next_state === 'pre' && nyse.early_close ? ` ${t('(early-close day)')}` : ''
      const evLabel = NEXT_EVENT[nyse.next_state] ? t(NEXT_EVENT[nyse.next_state]) : t('Next change')
      lines.push(t('{event}{note}: {when} your time ({et} ET)', { event: evLabel, note, when: `${day}${hhmm(at, locale)}`, et: hhmm(at, locale, 'America/New_York') }))
      lines.push(t('in {time}', { time: untilText(at.getTime() - Date.now(), t) }))
    }
  }
  return lines
}

export default function NyseLamp() {
  const { t, locale } = useI18n()
  const [nyse, setNyse] = useState(null)

  const load = useCallback(() => {
    api.getNyse().then(setNyse).catch(() => {})
  }, [])

  useEffect(() => {
    load()
    const refresh = () => { if (!document.hidden) load() }
    const poll = setInterval(refresh, 60000)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      clearInterval(poll)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [load])

  // Refetch right after the scheduled change so the colour flips on time
  // instead of waiting for the next poll.
  const nextAt = nyse?.next_change_at
  useEffect(() => {
    if (!nextAt) return undefined
    const ms = Date.parse(nextAt) - Date.now() + 1500
    if (!(ms > 0) || ms > 2 ** 31 - 1) return undefined
    const id = setTimeout(load, ms)
    return () => clearTimeout(id)
  }, [nextAt, load])

  if (!nyse) return null

  const info = STATE[nyse.state] || STATE.closed
  const lit = nyse.state === 'pre' || nyse.state === 'open' || nyse.state === 'after'

  return (
    <div className="lamp nyse-lamp" title={tooltipLines(nyse, t, locale).join('\n')}>
      <span className="lamp-dot" style={{ background: info.color, boxShadow: lit ? `0 0 6px ${info.color}` : 'none' }} />
      <span className="lamp-label" style={{ color: lit ? info.color : undefined }}>NYSE</span>
    </div>
  )
}
