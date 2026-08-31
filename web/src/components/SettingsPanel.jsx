import React, { useState, useEffect } from 'react'
import { api } from '../api'

const HTF_OPTIONS = ['1h', '2h', '4h', '6h', '12h', '1d', '3d', '1w']

const FILTER_TOGGLES = [
  { key: 'frama', label: 'FRAMA trend + slope' },
  { key: 'chop', label: 'CHOP' },
  { key: 'atr', label: 'ATR' },
  { key: 'htf', label: 'HTF bias' },
  { key: 'fake_break', label: 'Fake breakout' },
  { key: 'liq_sweep', label: 'Liquidity sweep' },
  {
    key: 'hurst',
    label: 'Hurst regime clarity',
    hint: 'Direction-agnostic: rejects both long and short signals when the market is statistically close to a random walk (Hurst exponent near 0.5) — a complement to CHOP, not a replacement. Off by default until validated live.',
  },
]

function Toggle({ checked, onChange, disabled }) {
  return (
    <label className="switch">
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="switch-track" />
    </label>
  )
}

// Checks the whole Android push pipeline at once: server → Firebase → device,
// instead of waiting for a real signal to test it.
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
          // 🆕 FIX: client-side range check — the server validates the same
          // thing (this isn't the only protection), but it's better to tell
          // the user right away instead of making them wait for a round
          // trip to the server for an obvious mistake
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

  // 🆕 FIX: if config was updated FROM OUTSIDE (a WS config_changed event
  // from another client — e.g. the Android app changed CHOP while the web
  // was open), any unsaved local draft becomes a lie — it shows whatever
  // the user once started typing, not the actual current value from the server.
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
  const saveVp = (field, value) => run(`vp_${field}`, () => api.setVolumeProfile({ [field]: value }))

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
            <div className="field">
              <label title="Where to move SL after closing 50% at TP1">SL after TP1</label>
              <select
                value={config.tp1_sl_mode}
                disabled={busy === 'tp1_sl_mode'}
                onChange={(e) => run('tp1_sl_mode', () => api.setTp1SlMode(e.target.value))}
              >
                <option value="breakeven">Breakeven (SL = entry)</option>
                <option value="half_tp1">Half-way to TP1 (SL &gt; entry)</option>
              </select>
            </div>
            <div className="field">
              <label title="How often the bot re-fetches candles and re-checks for signals. Signals only ever form on a closed bar, so this mainly affects how fast TP1/SL hits are caught and how fresh the dashboard's live numbers are.">Scan interval</label>
              <select
                value={config.scan_interval_seconds}
                disabled={busy === 'scan_interval'}
                onChange={(e) => run('scan_interval', () => api.setScanInterval(Number(e.target.value)))}
              >
                {(config.scan_interval_options || [15, 30, 60, 180]).map((s) => (
                  <option key={s} value={s}>{s < 60 ? `${s}s` : `${s / 60}m`}</option>
                ))}
              </select>
            </div>
            <div className="field">
              <label title="How often the bot re-fetches on-chain data (Etherscan exchange balances + CoinGecko Fear&Greed/dominance). Lower is fresher but flow deltas over a shorter window tend to be noisier.">On-chain interval</label>
              <select
                value={config.onchain_interval_seconds}
                disabled={busy === 'onchain_interval'}
                onChange={(e) => run('onchain_interval', () => api.setOnchainInterval(Number(e.target.value)))}
              >
                {(config.onchain_interval_options || [900, 1800, 3600]).map((s) => (
                  <option key={s} value={s}>{s < 3600 ? `${s / 60}m` : `${s / 3600}h`}</option>
                ))}
              </select>
            </div>
            <div className="toggle-row">
              <span className="row-label" title="Funding rate + open interest bias, per pair — only has any effect in Futures mode. See Signal Filters below for the direction-agnostic Hurst regime filter, a separate feature.">
                Derivatives bias (funding + OI)
              </span>
              <Toggle
                checked={config.derivatives_enabled ?? true}
                disabled={busy === 'derivatives_enabled'}
                onChange={(v) => run('derivatives_enabled', () => api.setDerivativesEnabled(v))}
              />
            </div>
            <div className="field">
              <label title="How often the bot re-fetches funding rate and open interest. Shorter default than on-chain — both move meaningfully within a single 1h/4h trading timeframe.">Derivatives interval</label>
              <select
                value={config.derivatives_interval_seconds}
                disabled={busy === 'derivatives_interval'}
                onChange={(e) => run('derivatives_interval', () => api.setDerivativesInterval(Number(e.target.value)))}
              >
                {(config.derivatives_interval_options || [900, 1800, 3600]).map((s) => (
                  <option key={s} value={s}>{s < 3600 ? `${s / 60}m` : `${s / 3600}h`}</option>
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
            <h3 className="panel-title">Notifications</h3>
            <div className="toggle-row">
              <span className="row-label" title="Discord gateway stays connected — commands like !status keep working. Only the channel messages (signals, TP1) are suppressed. WebSocket and Android push are unaffected.">
                Discord signal notifications
              </span>
              <Toggle
                checked={config.discord_notifications_enabled}
                disabled={busy === 'discord_notifications'}
                onChange={(v) => run('discord_notifications', () => api.setDiscordNotifications(v))}
              />
            </div>
            <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
              Off = the bot stops posting signal/TP1 messages to the Discord channel. Scanning, the web
              dashboard, and Android push all keep working exactly the same either way.
            </p>
          </div>

          <div className="panel">
            <h3 className="panel-title">Signal Filters</h3>
            {FILTER_TOGGLES.map(({ key, label, hint }) => (
              <div className="toggle-row" key={key}>
                <span className="row-label" title={hint}>{label}</span>
                <Toggle
                  checked={config.filter_toggles?.[key] ?? true}
                  disabled={busy === `filter_${key}`}
                  onChange={(v) => run(`filter_${key}`, () => api.setFilterToggle(key, v))}
                />
              </div>
            ))}
            <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
              Off = filter is skipped entirely (always passes) when deciding whether a signal fires. Matches the lamp row in the topbar.
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
                    onClick={() => {
                      if (!window.confirm(`Stop tracking ${p}?`)) return
                      // Equivalent of Discord's !remove vs !delsignals: can
                      // stop scanning while keeping the accumulated adaptive
                      // TP/SL statistics (in case the pair comes back), or
                      // fully wipe its signal history.
                      const purge = window.confirm(
                        `Also delete accumulated signal history for ${p}? This cannot be undone.\n\nOK — delete history\nCancel — keep history (in case you re-add ${p} later)`
                      )
                      run(`rm_${p}`, () => api.removePair(p, purge))
                    }}
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
            <h3 className="panel-title">Volume Profile</h3>
            <div className="toggle-row">
              <span className="row-label" title="Approximated from OHLCV (no tick data) — see the POC/Value Area overlay on the Chart tab.">
                Enabled
              </span>
              <Toggle
                checked={config.volume_profile?.enabled ?? true}
                disabled={busy === 'vp_enabled'}
                onChange={(v) => saveVp('enabled', v)}
              />
            </div>
            <NumberField
              label="Bins" hint="10–200, price buckets across the lookback window"
              value={config.volume_profile?.bins ?? 50}
              min={10} max={200} busyKey="vp_bins" busy={busy}
              onSave={(v) => saveVp('bins', v)}
            />
            <NumberField
              label="Lookback" hint="50–2000 bars, independent of the chart's display limit"
              value={config.volume_profile?.lookback ?? 300}
              min={50} max={2000} busyKey="vp_lookback" busy={busy}
              onSave={(v) => saveVp('lookback', v)}
            />
            <NumberField
              label="Value Area %" hint="50–95, share of volume that defines the band around POC"
              value={Math.round((config.volume_profile?.value_area_pct ?? 0.70) * 100)}
              min={50} max={95} busyKey="vp_value_area_pct" busy={busy}
              onSave={(v) => saveVp('value_area_pct', v / 100)}
            />
            <div className="toggle-row">
              <span className="row-label" title="Off keeps the POC line and Value Area band, just drops the horizontal histogram bars.">
                Show histogram
              </span>
              <Toggle
                checked={config.volume_profile?.show_histogram ?? true}
                disabled={busy === 'vp_show_histogram'}
                onChange={(v) => saveVp('show_histogram', v)}
              />
            </div>
            <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
              POC (Point of Control) and Value Area are a different kind of level from pivot support/resistance
              above — they mark where volume actually concentrated, not local price extremes. Complementary, not a replacement.
            </p>
          </div>

          <div className="panel">
            <h3 className="panel-title">Chart Colors</h3>
            <ColorField label="FRAMA" value={colors.frama} onSave={(v) => saveColor('frama', v)} />
            <ColorField label="Bollinger Bands" value={colors.bb} onSave={(v) => saveColor('bb', v)} />
            <ColorField label="Support" value={colors.support} onSave={(v) => saveColor('support', v)} />
            <ColorField label="Resistance" value={colors.resistance} onSave={(v) => saveColor('resistance', v)} />
            <ColorField label="POC / Value Area" value={colors.poc || '#e6c619'} onSave={(v) => saveColor('poc', v)} />
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
