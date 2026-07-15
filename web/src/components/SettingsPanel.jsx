import React, { useState } from 'react'
import { api } from '../api'

const HTF_OPTIONS = ['1h', '2h', '4h', '6h', '12h', '1d', '3d', '1w']

function Toggle({ checked, onChange, disabled }) {
  return (
    <label className="switch">
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="switch-track" />
    </label>
  )
}

// Числовое поле с сохранением по blur — общий паттерн для всех индикаторных параметров
function NumberField({ label, hint, value, min, max, step = 1, busyKey, busy, onSave }) {
  return (
    <div className="field">
      <label>{label}{hint ? ` (${hint})` : ''}</label>
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        defaultValue={value}
        disabled={busy === busyKey}
        onBlur={(e) => {
          const v = step === 1 ? parseInt(e.target.value, 10) : parseFloat(e.target.value)
          if (!Number.isNaN(v) && v !== value) onSave(v)
        }}
      />
    </div>
  )
}

function ColorField({ label, value, onSave }) {
  const [draft, setDraft] = useState(value)
  return (
    <div className="toggle-row">
      <span className="row-label">{label}</span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span className="row-value mono">{draft}</span>
        <input
          type="color"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={(e) => { if (e.target.value !== value) onSave(e.target.value) }}
          style={{ width: 32, height: 24, padding: 0, border: '1px solid var(--border)', borderRadius: 4, background: 'none', cursor: 'pointer' }}
        />
      </span>
    </div>
  )
}

export default function SettingsPanel({ config, onChanged }) {
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [newPair, setNewPair] = useState('')
  const [chopDraft, setChopDraft] = useState({})

  if (!config) return <div className="empty-state">Загрузка…</div>

  const run = async (key, fn) => {
    setBusy(key)
    setError(null)
    try {
      await fn()
      onChanged()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(null)
    }
  }

  const chopValue = (t) => chopDraft[t] ?? config.chop_threshold[t]
  const ind = config.indicators
  const colors = config.colors

  const saveIndicator = (field, value) => run(`ind_${field}`, () => api.setIndicators({ [field]: value }))
  const saveColor = (field, value) => run(`col_${field}`, () => api.setColors({ [field]: value }))

  return (
    <div>
      {error && <div className="error-banner">{error}</div>}
      <div className="grid-2">
        <div>
          <div className="panel">
            <h3 className="panel-title">Режим торговли</h3>
            <div className="field">
              <label>Market mode</label>
              <select
                value={config.mode}
                disabled={busy === 'mode'}
                onChange={(e) => run('mode', () => api.setMode(e.target.value))}
              >
                <option value="spot">Spot</option>
                <option value="futures">Futures</option>
              </select>
            </div>
            <div className="field">
              <label>HTF Bias</label>
              <select
                value={config.htf_bias}
                disabled={busy === 'htf'}
                onChange={(e) => run('htf', () => api.setHtf(e.target.value))}
              >
                {HTF_OPTIONS.map((h) => (
                  <option key={h} value={h}>{h}</option>
                ))}
              </select>
            </div>
            <div className="toggle-row">
              <span className="row-label">Heikin Ashi для UT Bot</span>
              <Toggle
                checked={config.ut_heikin_ashi}
                disabled={busy === 'utha'}
                onChange={(v) => run('utha', () => api.setUtha(v))}
              />
            </div>
            <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
              Смена mode/HTF сбрасывает активные позиционные состояния — как и в Discord-командах.
            </p>
          </div>

          <div className="panel">
            <h3 className="panel-title">CHOP Threshold</h3>
            {Object.keys(config.chop_threshold).map((tf) => (
              <div className="field" key={tf}>
                <label>{tf} (20–90, ниже = тренд)</label>
                <div style={{ display: 'flex', gap: 8 }}>
                  <input
                    type="number"
                    min={20}
                    max={90}
                    step={0.1}
                    value={chopValue(tf)}
                    onChange={(e) => setChopDraft({ ...chopDraft, [tf]: e.target.value })}
                  />
                  <button
                    className="btn"
                    disabled={busy === `chop_${tf}`}
                    onClick={() => run(`chop_${tf}`, () => api.setChop(tf, parseFloat(chopValue(tf))))}
                  >
                    Сохранить
                  </button>
                </div>
              </div>
            ))}
          </div>

          <div className="panel">
            <h3 className="panel-title">Adaptive TP</h3>
            <div className="field">
              <label>Режим</label>
              <select
                value={config.tp_config.use_safe_tp ? 'safe' : 'aggressive'}
                disabled={busy === 'tp_mode'}
                onChange={(e) => run('tp_mode', () => api.setTpConfig('mode', e.target.value))}
              >
                <option value="aggressive">Aggressive ⚡</option>
                <option value="safe">Safe 🛡️</option>
              </select>
            </div>
            <NumberField
              label="Aggressive percentile" hint="10–99"
              value={Math.round(config.tp_config.tp_percentile * 100)}
              min={10} max={99} busyKey="tp_pct" busy={busy}
              onSave={(v) => run('tp_pct', () => api.setTpConfig('percentile', v))}
            />
            <NumberField
              label="Safe percentile" hint="10–99"
              value={Math.round(config.tp_config.safe_tp_percentile * 100)}
              min={10} max={99} busyKey="tp_safe" busy={busy}
              onSave={(v) => run('tp_safe', () => api.setTpConfig('safe', v))}
            />
            <NumberField
              label="История сигналов, лимит" hint="5–200"
              value={config.tp_config.signal_history_limit}
              min={5} max={200} busyKey="tp_limit" busy={busy}
              onSave={(v) => run('tp_limit', () => api.setTpConfig('limit', v))}
            />
            <div className="row">
              <span className="row-label">Min / Max TP</span>
              <span className="row-value">{config.tp_config.min_tp_pct}% / {config.tp_config.max_tp_pct}%</span>
            </div>
            <div className="row">
              <span className="row-label">Max hold bars</span>
              <span className="row-value">{config.tp_config.max_hold_bars}</span>
            </div>
          </div>

          <div className="panel">
            <h3 className="panel-title">Отслеживаемые пары</h3>
            <div className="chip-row">
              {config.pairs.map((p) => (
                <span className="chip" key={p}>
                  {p}
                  <button
                    title="Убрать пару"
                    onClick={() => run(`rm_${p}`, () => api.removePair(p))}
                    disabled={busy === `rm_${p}`}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
            <div className="add-pair-form">
              <input
                placeholder="DOGE/USDT"
                value={newPair}
                onChange={(e) => setNewPair(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && newPair.trim()) {
                    run('add_pair', () => api.addPair(newPair.trim())).then(() => setNewPair(''))
                  }
                }}
              />
              <button
                className="btn primary"
                disabled={!newPair.trim() || busy === 'add_pair'}
                onClick={() => run('add_pair', () => api.addPair(newPair.trim())).then(() => setNewPair(''))}
              >
                Добавить
              </button>
            </div>
          </div>
        </div>

        <div>
          <div className="panel">
            <h3 className="panel-title">Andean + MFI</h3>
            <NumberField label="MFI Length" hint="2–50" value={ind.mfi_len} min={2} max={50}
              busyKey="ind_mfi_len" busy={busy} onSave={(v) => saveIndicator('mfi_len', v)} />
            <NumberField label="AI Training Size" hint="100–3000" value={ind.mfi_training} min={100} max={3000}
              busyKey="ind_mfi_training" busy={busy} onSave={(v) => saveIndicator('mfi_training', v)} />
            <NumberField label="Andean Length" hint="5–100" value={ind.and_len} min={5} max={100}
              busyKey="ind_and_len" busy={busy} onSave={(v) => saveIndicator('and_len', v)} />
            <NumberField label="Andean Signal Smoothing" hint="2–50" value={ind.and_sig_len} min={2} max={50}
              busyKey="ind_and_sig_len" busy={busy} onSave={(v) => saveIndicator('and_sig_len', v)} />
            <NumberField label="Confirmation Window" hint="1–20" value={ind.lookback} min={1} max={20}
              busyKey="ind_lookback" busy={busy} onSave={(v) => saveIndicator('lookback', v)} />
          </div>

          <div className="panel">
            <h3 className="panel-title">FRAMA Channel</h3>
            <NumberField label="FRAMA Length" hint="5–100" value={ind.frama_len} min={5} max={100}
              busyKey="ind_frama_len" busy={busy} onSave={(v) => saveIndicator('frama_len', v)} />
            <NumberField label="FRAMA Multiplier" hint="0.5–5.0" value={ind.frama_mult} min={0.5} max={5} step={0.1}
              busyKey="ind_frama_mult" busy={busy} onSave={(v) => saveIndicator('frama_mult', v)} />
          </div>

          <div className="panel">
            <h3 className="panel-title">UT Bot</h3>
            <NumberField label="Key Value (Sensitivity)" hint="0.1–10" value={ind.ut_sensitivity} min={0.1} max={10} step={0.1}
              busyKey="ind_ut_sensitivity" busy={busy} onSave={(v) => saveIndicator('ut_sensitivity', v)} />
            <NumberField label="ATR Period" hint="2–50" value={ind.ut_period} min={2} max={50}
              busyKey="ind_ut_period" busy={busy} onSave={(v) => saveIndicator('ut_period', v)} />
            <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
              Heikin Ashi Candles — переключатель выше, в блоке «Режим торговли».
            </p>
            <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
              Изменение любого параметра сбрасывает активные позиционные состояния — как mode/HTF.
            </p>
          </div>

          <div className="panel">
            <h3 className="panel-title">Цвета графика</h3>
            <ColorField label="FRAMA" value={colors.frama} onSave={(v) => saveColor('frama', v)} />
            <ColorField label="Bollinger Bands" value={colors.bb} onSave={(v) => saveColor('bb', v)} />
            <ColorField label="Support" value={colors.support} onSave={(v) => saveColor('support', v)} />
            <ColorField label="Resistance" value={colors.resistance} onSave={(v) => saveColor('resistance', v)} />
            <ColorField label="MFI линия" value={colors.mfi_line} onSave={(v) => saveColor('mfi_line', v)} />
            <ColorField label="MFI overbought" value={colors.mfi_overbought} onSave={(v) => saveColor('mfi_overbought', v)} />
            <ColorField label="MFI oversold" value={colors.mfi_oversold} onSave={(v) => saveColor('mfi_oversold', v)} />
            <ColorField label="Свеча вверх" value={colors.candle_up} onSave={(v) => saveColor('candle_up', v)} />
            <ColorField label="Свеча вниз" value={colors.candle_down} onSave={(v) => saveColor('candle_down', v)} />
          </div>
        </div>
      </div>
    </div>
  )
}
