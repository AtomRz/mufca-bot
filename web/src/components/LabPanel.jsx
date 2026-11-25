import React from 'react'
import { useI18n } from '../i18n'
import AutoFilterPanel from './AutoFilterPanel'
import VolumeGatePanel from './VolumeGatePanel'
import IndicatorOptimizerPanel from './IndicatorOptimizerPanel'
import AutopsyPanel from './AutopsyPanel'
import MissedSimPanel from './MissedSimPanel'
import WalkForwardPanel from './WalkForwardPanel'

// Lab: everything that RUNS a simulation over the history (optimizers, testers, autopsies).
// They used to sit in Settings between the plain switches; the switches stay there.
// Each panel keeps its own scope fields (pair / timeframe / bars) and shares the single
// simulation slot, so only one of them can run at a time.
export default function LabPanel({ config, onChanged }) {
  const { t } = useI18n()
  if (!config) return <div className="panel">{t('Loading…')}</div>
  return (
    <div>
      <p style={{ fontSize: 12, color: 'var(--text-faint)', margin: '0 0 12px' }}>
        {t('Optimizers and testers. Most only measure; the ones with an Apply option say so on their button. Only one simulation runs at a time.')}
      </p>
      <AutopsyPanel config={config} />
      <MissedSimPanel config={config} />
      <AutoFilterPanel config={config} onChanged={onChanged} />
      <VolumeGatePanel config={config} onChanged={onChanged} />
      <IndicatorOptimizerPanel config={config} />
      <WalkForwardPanel config={config} />
    </div>
  )
}
