import React, { useState, useEffect } from 'react'
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

// Проверяет всю цепочку Android push разом: сервер → Firebase → устройство,
// вместо того чтобы ждать реального сигнала для проверки.
function PushPanel({ busy, run }) {
  const [devices, setDevices] = useState(null)
  const [result, setResult] = useState(null)

  useEffect(() => {
    api.getDevices().then((d) => setDevices(Object.values(d.devices))).catch(() => {})
  }, [])

  const sendTest = () =>
    run('test_push', () =>
      api.testPush().then((r) => {
        setResult(r)
        return r
      }),
    )

  return (
    <div className="panel">
      <h3 className="panel-title">Android Push Notifications</h3>
      <div className="row">
        <span className="row-label">Registered devices</span>
        <span className="row-value">{devices === null ? '…' : devices.length}</span>
      </div>
      {devices && devices.length > 0 && (
        <div className="chip-row" style={{ marginTop: 8 }}>
          {devices.map((d, i) => (
            <span className="chip" key={i}>{d.device_name}</span>
          ))}
        </div>
      )}
      <button
        className="btn"
        style={{ marginTop: 12 }}
        disabled={busy === 'test_push'}
        onClick={sendTest}
      >
        {busy === 'test_push' ? 'Sending…' : 'Send test push'}
      </button>
      {result && (
        <p style={{ fontSize: 12, marginTop: 10, color: result.skipped ? 'var(--short)' : 'var(--long)' }}>
          {result.skipped === 'firebase_not_configured' &&
            'Firebase not configured on the server — place firebase-credentials.json and set FIREBASE_CREDENTIALS_PATH in .env.'}
          {result.skipped === 'no_devices_registered' &&
            'No devices registered yet — open the Android app once to register this phone.'}
          {!result.skipped && `Sent to ${result.sent} device(s)${result.failed ? `, ${result.failed} failed` : ''}.`}
        </p>
      )}
    </div>
  )
}

// Numeric field, saved on blur — shared pattern for every indicator parameter
function NumberField({ label, hint, value, min, max, step = 1, busyKey, busy, onSave }) {
  const [warn, setWarn] = useState(null)
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
          if (Number.isNaN(v)) return
          // 🆕 FIX: клиентская проверка диапазона — сервер валидирует то же самое
          // (это не единственная защита), но лучше сказать пользователю сразу,
          // а не заставлять ждать round-trip к серверу ради очевидной ошибки
          if (v < min || v > max) {
            setWarn(`Must be between ${min} and ${max}`)
            e.target.value = value
            return
          }
          setWarn(null)
          if (v !== value) onSave(v)
        }}
      />
      {warn && <p style={{ fontSize: 11, color: 'var(--short)', margin: '4px 0 0' }}>{warn}</p>}
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

  // 🆕 FIX: если config обновился ИЗВНЕ (WS-событие config_changed от другого
  // клиента — например Android-приложение поменяло CHOP, пока открыт веб), любой
  // несохранённый локальный черновик становится враньём — показывает то, что
  // пользователь когда-то начал печатать, а не актуальное значение с сервера.
  useEffect(() => {
    setChopDraft({})
  }, [config])

  if (!config) return <div className="empty-state">Loading…</div>

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
            <h3 className="panel-title">Trading Mode</h3>
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
              <span className="row-label">Heikin Ashi for UT Bot</span>
              <Toggle
                checked={config.ut_heikin_ashi}
                disabled={busy === 'utha'}
                onChange={(v) => run('utha', () => api.setUtha(v))}
              />
            </div>
            <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
              Changing mode/HTF resets active position-tracking state — same as the equivalent Discord commands.
            </p>
          </div>

          <div className="panel">
            <h3 className="panel-title">CHOP Threshold</h3>
            {Object.keys(config.chop_threshold).map((tf) => (
              <div className="field" key={tf}>
                <label>{tf} (20–90, lower = trending)</label>
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
                    Save
                  </button>
                </div>
              </div>
            ))}
          </div>

          <div className="panel">
            <h3 className="panel-title">Adaptive TP</h3>
            <div className="field">
              <label>Mode</label>
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
              label="Signal history limit" hint="5–200"
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
            <h3 className="panel-title">Tracked Pairs</h3>
            <div className="chip-row">
              {config.pairs.map((p) => (
                <span className="chip" key={p}>
                  {p}
                  <button
                    title="Remove pair"
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
                Add
              </button>
            </div>
          </div>

          <PushPanel busy={busy} run={run} />
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
              Heikin Ashi Candles toggle is above, in the "Trading Mode" panel.
            </p>
          </div>

          <div className="panel">
            <h3 className="panel-title">Bollinger Bands</h3>
            <NumberField label="Length" hint="5–100" value={ind.bb_period} min={5} max={100}
              busyKey="ind_bb_period" busy={busy} onSave={(v) => saveIndicator('bb_period', v)} />
            <NumberField label="StdDev" hint="0.5–5.0" value={ind.bb_stddev} min={0.5} max={5} step={0.1}
              busyKey="ind_bb_stddev" busy={busy} onSave={(v) => saveIndicator('bb_stddev', v)} />
            <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
              Basis MA type and source are fixed (SMA of Close) — not configurable yet.
            </p>
          </div>

          <div className="panel">
            <h3 className="panel-title">S&amp;R Power Channel</h3>
            <NumberField label="Lookback Length" hint="3–50, pivot confirmation window" value={ind.sr_pivot_window} min={3} max={50}
              busyKey="ind_sr_pivot_window" busy={busy} onSave={(v) => saveIndicator('sr_pivot_window', v)} />
            <NumberField label="Max Levels Shown" hint="1–10, per support/resistance" value={ind.sr_max_levels} min={1} max={10}
              busyKey="ind_sr_max_levels" busy={busy} onSave={(v) => saveIndicator('sr_max_levels', v)} />
            <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
              A pivot needs Lookback Length bars <em>after</em> it to be confirmed — a fresh breakout won't show
              as resistance until price pulls back. This is standard pivot-based S/R behavior, not a bug.
              "Extend Bars" from the reference indicator doesn't apply here — our lines already span the full chart.
            </p>
            <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
              Changing any indicator parameter above resets active position-tracking state — same as mode/HTF.
            </p>
          </div>

          <div className="panel">
            <h3 className="panel-title">Chart Colors</h3>
            <ColorField label="FRAMA" value={colors.frama} onSave={(v) => saveColor('frama', v)} />
            <ColorField label="Bollinger Bands" value={colors.bb} onSave={(v) => saveColor('bb', v)} />
            <ColorField label="Support" value={colors.support} onSave={(v) => saveColor('support', v)} />
            <ColorField label="Resistance" value={colors.resistance} onSave={(v) => saveColor('resistance', v)} />
            <ColorField label="Take Profit line" value={colors.tp_line} onSave={(v) => saveColor('tp_line', v)} />
            <ColorField label="Stop Loss line" value={colors.sl_line} onSave={(v) => saveColor('sl_line', v)} />
            <ColorField label="Long signal marker" value={colors.signal_long} onSave={(v) => saveColor('signal_long', v)} />
            <ColorField label="Short signal marker" value={colors.signal_short} onSave={(v) => saveColor('signal_short', v)} />
            <ColorField label="MFI line" value={colors.mfi_line} onSave={(v) => saveColor('mfi_line', v)} />
            <ColorField label="MFI overbought" value={colors.mfi_overbought} onSave={(v) => saveColor('mfi_overbought', v)} />
            <ColorField label="MFI oversold" value={colors.mfi_oversold} onSave={(v) => saveColor('mfi_oversold', v)} />
            <ColorField label="Candle up" value={colors.candle_up} onSave={(v) => saveColor('candle_up', v)} />
            <ColorField label="Candle down" value={colors.candle_down} onSave={(v) => saveColor('candle_down', v)} />
          </div>
        </div>
      </div>
    </div>
  )
}
