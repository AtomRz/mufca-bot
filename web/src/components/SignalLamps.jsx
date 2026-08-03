import React, { useMemo } from 'react'

// Короткие подписи под лампочками.
const SIGNAL_LABELS = { mfi: 'MFI', andean: 'AND', ut_bot: 'UT' }
const FILTER_LABELS = {
  frama: 'FRAMA',
  chop: 'CHOP',
  atr: 'ATR',
  htf: 'HTF',
  fake_break: 'BRK',
  liq_sweep: 'SWEEP',
}

/**
 * 🆕 Лампочки сигналов (MFI/Andean/UT Bot) и фильтров в топ-баре.
 *
 * Логика идентична signals.check_signals (см. app/chart_data.py get_market_pulse) —
 * фронт тут только красит то, что уже посчитал бэкенд, никакой своей интерпретации
 * сигналов не добавляет.
 *
 * Направление фильтров ("какой фильтр против сигнала") определяется по большинству
 * голосов среди самих сигнальных лампочек (MFI/Andean/UT Bot), а не по общему тренду
 * FRAMA в топ-баре — если MFI и Andean оба сейчас "бычьи", фильтры красятся по long,
 * даже если общий тренд ещё bearish.
 */
export default function SignalLamps({ lamps }) {
  const candidateDir = useMemo(() => {
    if (!lamps) return null
    const states = [lamps.mfi?.state, lamps.andean?.state, lamps.ut_bot?.state]
    const longVotes = states.filter((s) => s === 'bull').length
    const shortVotes = states.filter((s) => s === 'bear').length
    if (longVotes === 0 && shortVotes === 0) return null
    if (longVotes === shortVotes) return null
    return longVotes > shortVotes ? 'long' : 'short'
  }, [lamps])

  if (!lamps) return null

  return (
    <div className="signal-lamps" title="Signals and filters for the pair/tf selected on the Chart tab">
      {Object.keys(SIGNAL_LABELS).map((key) => (
        <SignalDot key={key} label={SIGNAL_LABELS[key]} state={lamps[key]?.state} />
      ))}
      <div className="lamp-sep" />
      {Object.keys(FILTER_LABELS).map((key) => (
        <FilterDot key={key} label={FILTER_LABELS[key]} filter={lamps.filters?.[key]} candidateDir={candidateDir} />
      ))}
    </div>
  )
}

function SignalDot({ label, state }) {
  const color = state === 'bull' ? 'var(--long)' : state === 'bear' ? 'var(--short)' : 'var(--text-dim)'
  const title = `${label}: ${state === 'bull' ? 'long' : state === 'bear' ? 'short' : 'neutral'}`
  return (
    <div className="lamp" title={title}>
      <span className="lamp-dot" style={{ background: color, boxShadow: state !== 'neutral' ? `0 0 6px ${color}` : 'none' }} />
      <span className="lamp-label">{label}</span>
    </div>
  )
}

function FilterDot({ label, filter, candidateDir }) {
  if (!filter) return null

  let color = 'var(--text-dim)'
  let title = `${label}: no clear signal direction yet`

  if (!filter.enabled) {
    color = 'var(--text-dim)'
    title = `${label}: filter disabled in settings`
  } else if (candidateDir) {
    const pass = candidateDir === 'long' ? filter.pass_long : filter.pass_short
    color = pass ? 'var(--long)' : 'var(--short)'
    title = `${label}: ${pass ? 'allows' : 'blocks'} ${candidateDir === 'long' ? 'long' : 'short'}`
  }

  if (filter.value !== undefined) {
    title += ` (${filter.value}${filter.threshold !== undefined ? ` / threshold ${filter.threshold}` : ''})`
  }

  return (
    <div className="lamp" title={title}>
      <span className="lamp-dot" style={{ background: color }} />
      <span className="lamp-label">{label}</span>
    </div>
  )
}
