import React, { useEffect, useRef, useState } from 'react'

/**
 * 🆕 "SHOCK" lamp in the top bar — warns about abnormal market conditions:
 * a market shock (5x ATR move, see app/shock_guard.py) or an exchange-wallet
 * on-chain anomaly (app/onchain.py). Data comes from GET /api/shock.
 *
 *   dim          all normal
 *   red + glow   at least one shock / anomaly is active right now
 *   flash        a new one appeared since the last refresh
 *
 * Unlike the SHDW lamp it is always visible, so a dim lamp also tells you the
 * guard is watching. Clicking it opens the Status tab, where the details are
 * listed in the "Abnormal market conditions" banner.
 */
export default function ShockLamp({ shock, onClick }) {
  const shocks = shock?.market_shocks || []
  const anomaly = shock?.onchain_anomaly
  const anomalyEvents = anomaly?.events || []
  const count = shocks.length + (anomaly ? 1 : 0)
  const active = count > 0

  const prevCount = useRef(null)
  const [flash, setFlash] = useState(false)

  useEffect(() => {
    if (!shock) return undefined
    const was = prevCount.current
    prevCount.current = count
    if (was !== null && count > was) {
      setFlash(true)
      const id = setTimeout(() => setFlash(false), 6000)
      return () => clearTimeout(id)
    }
    return undefined
  }, [count, shock])

  if (!shock) return null

  const color = active ? 'var(--short)' : 'var(--text-dim)'

  const lines = []
  if (!active) {
    lines.push('No market shock or abnormal on-chain activity')
  } else {
    shocks.forEach((s) => {
      lines.push(
        `${s.ticker} ${s.timeframe} shock: ${s.move_pct > 0 ? '+' : ''}${s.move_pct}% in ${s.bars} bars (${s.move_atr}x ATR)`,
      )
    })
    anomalyEvents.forEach((ev) => {
      lines.push(
        `${ev.exchange} ${ev.wallet} ${ev.kind}:${Math.round(ev.prev_eth).toLocaleString()} → ${Math.round(ev.curr_eth).toLocaleString()} ETH (${ev.change_pct}%)`,
      )
    })
    if (anomaly && anomalyEvents.length === 0) lines.push('On-chain anomaly active')
    lines.push(shock.blocks_entries && shocks.length > 0 ? 'New entries are blocked while the shock lasts' : 'Entries are not blocked')
  }
  if (shock.alerts_muted) lines.push('Discord / push alerts for this are muted (Settings → Notifications)')
  lines.push('Click → Status')

  return (
    <div
      className={`lamp shock-lamp${flash ? ' lamp-flash' : ''}`}
      title={lines.join('\n')}
      onClick={onClick}
      style={{ cursor: onClick ? 'pointer' : 'default' }}
    >
      <span className="lamp-dot" style={{ background: color, boxShadow: active ? `0 0 6px ${color}` : 'none' }} />
      <span className="lamp-label" style={{ color: active ? 'var(--short)' : undefined }}>
        SHOCK{count > 0 ? ` ${count}` : ''}
      </span>
    </div>
  )
}
