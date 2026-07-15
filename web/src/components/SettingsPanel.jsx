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

  return (
    <div className="grid-2">
      <div>
        <div className="panel">
          <h3 className="panel-title">Режим торговли</h3>
          {error && <div className="error-banner">{error}</div>}
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
      </div>

      <div>
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
          <div className="field">
            <label>Aggressive percentile (10–99)</label>
            <div style={{ display: 'flex', gap: 8 }}>
              <input
                type="number" min={10} max={99}
                defaultValue={Math.round(config.tp_config.tp_percentile * 100)}
                onBlur={(e) => run('tp_pct', () => api.setTpConfig('percentile', e.target.value))}
              />
            </div>
          </div>
          <div className="field">
            <label>Safe percentile (10–99)</label>
            <input
              type="number" min={10} max={99}
              defaultValue={Math.round(config.tp_config.safe_tp_percentile * 100)}
              onBlur={(e) => run('tp_safe', () => api.setTpConfig('safe', e.target.value))}
            />
          </div>
          <div className="field">
            <label>История сигналов, лимит (5–200)</label>
            <input
              type="number" min={5} max={200}
              defaultValue={config.tp_config.signal_history_limit}
              onBlur={(e) => run('tp_limit', () => api.setTpConfig('limit', e.target.value))}
            />
          </div>
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
    </div>
  )
}
