import React, { useMemo } from 'react'
import { useI18n } from '../i18n'

// Short labels shown under the lamps.
const SIGNAL_LABELS = { mfi: 'MFI', andean: 'AND', ut_bot: 'UT' }
const FILTER_LABELS = {
  frama: 'FRAMA',
  slope: 'SLOPE',
  chop: 'CHOP',
  atr: 'ATR',
  htf: 'HTF',
  fake_break: 'BRK',
  liq_sweep: 'SWEEP',
  hurst: 'HURST',
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
  const { t } = useI18n()
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
    <div className="signal-lamps" title={t('Signals and filters for the pair/tf selected on the Chart tab')}>
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
  const { t } = useI18n()
  const color = state === 'bull' ? 'var(--long)' : state === 'bear' ? 'var(--short)' : 'var(--text-dim)'
  const title = state === 'bull' ? t('{label}: long', { label }) : state === 'bear' ? t('{label}: short', { label }) : t('{label}: neutral', { label })
  return (
    <div className="lamp" title={title}>
      <span className="lamp-dot" style={{ background: color, boxShadow: state !== 'neutral' ? `0 0 6px ${color}` : 'none' }} />
      <span className="lamp-label">{label}</span>
    </div>
  )
}

function FilterDot({ label, filter, candidateDir }) {
  const { t } = useI18n()
  if (!filter) return null

  let color = 'var(--text-dim)'
  let title = t('{label}: no clear signal direction yet', { label })

  if (!filter.enabled) {
    color = 'var(--text-dim)'
    title = t('{label}: filter disabled in settings', { label })
  } else if (candidateDir) {
    const pass = candidateDir === 'long' ? filter.pass_long : filter.pass_short
    color = pass ? 'var(--long)' : 'var(--short)'
    title = candidateDir === 'long'
      ? (pass ? t('{label}: allows long', { label }) : t('{label}: blocks long', { label }))
      : (pass ? t('{label}: allows short', { label }) : t('{label}: blocks short', { label }))
  }

  if (filter.value !== undefined) {
    title += ' ' + (filter.threshold !== undefined
      ? t('({value} / threshold {threshold})', { value: filter.value, threshold: filter.threshold })
      : `(${filter.value})`)
  } else if (filter.value_long !== undefined || filter.value_short !== undefined) {
    // 🆕 R:R lamp: risk/reward differ for long and short, there's no single
    // value — show the value for the current candle direction (candidateDir),
    // and if there's no direction yet, show both at once.
    const hasMin = filter.threshold !== undefined
    const th = filter.threshold
    if (candidateDir === 'long' || candidateDir === 'short') {
      const v = candidateDir === 'long' ? filter.value_long : filter.value_short
      title += ' ' + (hasMin ? t('({value} / min {min})', { value: v, min: th }) : `(${v})`)
    } else {
      title += ' ' + (hasMin
        ? t('(long {long} / short {short} / min {min})', { long: filter.value_long, short: filter.value_short, min: th })
        : t('(long {long} / short {short})', { long: filter.value_long, short: filter.value_short }))
    }
  }

  return (
    <div className="lamp" title={title}>
      <span className="lamp-dot" style={{ background: color }} />
      <span className="lamp-label">{label}</span>
    </div>
  )
}
