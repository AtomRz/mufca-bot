import React, { useMemo } from 'react'

// Short labels shown under the lamps.
const SIGNAL_LABELS = { mfi: 'MFI', andean: 'AND', ut_bot: 'UT' }
const FILTER_LABELS = {
  frama: 'FRAMA',
  chop: 'CHOP',
  atr: 'ATR',
  htf: 'HTF',
  fake_break: 'BRK',
  liq_sweep: 'SWEEP',
  rr: 'R:R',
}

/**
 * 🆕 Signal (MFI/Andean/UT Bot) and filter lamps in the top bar.
 *
 * The logic is identical to signals.check_signals (see app/chart_data.py's
 * get_market_pulse) — the frontend here only colors what the backend already
 * computed, it doesn't add any interpretation of the signals of its own.
 *
 * Filter direction ("which filter is against the signal") is determined by a
 * majority vote among the signal lamps themselves (MFI/Andean/UT Bot), not
 * by the overall FRAMA trend in the top bar — if MFI and Andean are both
 * currently "bullish", the filters are colored for long, even if the
 * overall trend is still bearish.
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
  } else if (filter.value_long !== undefined || filter.value_short !== undefined) {
    // 🆕 R:R lamp: risk/reward differ for long and short, there's no single
    // value — show the value for the current candle direction (candidateDir),
    // and if there's no direction yet, show both at once.
    const thresholdSuffix = filter.threshold !== undefined ? ` / min ${filter.threshold}` : ''
    if (candidateDir === 'long') {
      title += ` (${filter.value_long}${thresholdSuffix})`
    } else if (candidateDir === 'short') {
      title += ` (${filter.value_short}${thresholdSuffix})`
    } else {
      title += ` (long ${filter.value_long} / short ${filter.value_short}${thresholdSuffix})`
    }
  }

  return (
    <div className="lamp" title={title}>
      <span className="lamp-dot" style={{ background: color }} />
      <span className="lamp-label">{label}</span>
    </div>
  )
}
