import React, { useState, useEffect, useRef } from 'react'
import { api } from '../api'

const HTF_OPTIONS = ['1h', '2h', '4h', '6h', '12h', '1d', '3d', '1w']

const FILTER_TOGGLES = [
  { key: 'frama', label: 'FRAMA trend', hint: 'Direction of the FRAMA channel only. The FRAMA slope check (SLOPE lamp) is always applied and cannot be switched off.' },
  { key: 'chop', label: 'CHOP' },
  { key: 'atr', label: 'ATR' },
  { key: 'htf', label: 'HTF bias' },
  { key: 'fake_break', label: 'Fake breakout' },
  { key: 'liq_sweep', label: 'Liquidity sweep' },
  {
    key: 'hurst',
    label: 'Hurst regime clarity',
    hint: 'Rejects signals when the market lacks a statistically clear regime (see Mode below) — a complement to CHOP, not a replacement. Tune Window/Mode/Deviation in the Hurst Filter panel below once enabled.',
  },
  {
    key: 'spread',
    label: 'Order book spread',
    hint: 'Live-only, not backtestable (order book depth isn\'t part of OHLCV history). Rejects a signal if the current spread eats too much of that signal\'s own SL distance, or reads anomalously wide vs. this pair\'s recent history. Collection runs in the background regardless of this toggle — preview any pair\'s warm-up progress with !spread [pair] in Discord before switching on.',
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
        // 🆕 FIX: defaultValue is only read on mount, so after a change made
        // elsewhere (another client, a WS config_changed refresh, or a value the
        // server clamped) the field kept showing the old number and the next
        // blur could silently write it back. key={value} remounts it on change.
        key={value}
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
  const timer = useRef(null)
  // 🆕 FIX: the draft was initialised once and never followed the saved value
  // (config changed from another client, or the server rejected/normalised it).
  useEffect(() => { setDraft(value) }, [value])
  useEffect(() => () => clearTimeout(timer.current), [])
  const commit = (v) => {
    clearTimeout(timer.current)
    if (v !== value) onSave(v)
  }
  return (
    <div className="toggle-row">
      <span className="row-label">{label}</span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span className="row-value mono">{draft}</span>
        <input
          type="color"
          value={draft}
          onChange={(e) => {
            const v = e.target.value
            setDraft(v)
            // 🆕 FIX: saving only on blur never fired in some mobile pickers
            // (the dialog closes without blurring the input), so the colour was
            // silently lost. Save shortly after the last change as well; blur
            // still saves immediately.
            clearTimeout(timer.current)
            timer.current = setTimeout(() => commit(v), 800)
          }}
          onBlur={(e) => commit(e.target.value)}
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
  const [hurstWindowDraft, setHurstWindowDraft] = useState({})
  // 🆕 inline two-step removal instead of window.confirm(): an Android WebView
  // without onJsConfirm makes confirm() return false, so the "×" did nothing.
  const [removing, setRemoving] = useState(null)

  // 🆕 FIX: if config was updated FROM OUTSIDE (a WS config_changed event
  // from another client — e.g. the Android app changed CHOP while the web
  // was open), any unsaved local draft becomes a lie — it shows whatever
  // the user once started typing, not the actual current value from the server.
  useEffect(() => {
    setChopDraft({})
    setHurstWindowDraft({}) // 🆕 was never cleared, so it kept showing a stale number
  }, [config])

  if (!config) return <div className="empty-state">Loading…</div>

  const run = async (key, fn) => {
    setBusy(key)
    setError(null)
    try {
      await fn()
      onChanged()
      return true
    } catch (e) {
      setError(e.message)
      return false
    } finally {
      setBusy(null)
    }
  }

  const chopValue = (t) => chopDraft[t] ?? config.chop_threshold[t]
  const hurstWindowValue = (t) => hurstWindowDraft[t] ?? config.hurst_window[t]
  const ind = config.indicators
  const colors = config.colors

  const saveIndicator = (field, value) => run(`ind_${field}`, () => api.setIndicators({ [field]: value }))
  const saveColor = (field, value) => run(`col_${field}`, () => api.setColors({ [field]: value }))
  const saveVp = (field, value) => run(`vp_${field}`, () => api.setVolumeProfile({ [field]: value }))
  const saveZones = (field, value) => run(`zones_${field}`, () => api.setZones({ [field]: value }))

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
                <option value="quarter_tp1">1/4 of the way to TP1</option>
                <option value="half_tp1">Half-way to TP1 (SL &gt; entry)</option>
                <option value="three_quarter_tp1">3/4 of the way to TP1</option>
              </select>
            </div>
            <div className="toggle-row">
              <span className="row-label" title="Replays the 1m candles since the last check against SL / TP1 / TP2 on every scan, so a wick that touches a level between two live-price polls is no longer missed (e.g. TP1 touched but SL never moved). Costs one extra 1m candle fetch per open trade per scan. Off = old behavior: live price poll + closed-bar check only.">
                Intrabar 1m TP/SL check
              </span>
              <Toggle
                checked={config.intrabar_check_enabled ?? true}
                disabled={busy === 'intrabar_check'}
                onChange={(v) => run('intrabar_check', () => api.setIntrabarCheck(v))}
              />
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
            <h3 className="panel-title">Market Shock Guard</h3>
            <div className="toggle-row">
              <span className="row-label" title="A market shock is a move of 5x ATR or more over the last 4 closed bars (news cascades, exchange hacks). Off = warn only (alert + dashboard banner). On = also stop opening new positions while the shock lasts.">
                Block new entries during a shock
              </span>
              <Toggle
                checked={config.shock_guard_block_entries ?? false}
                disabled={busy === 'shock_guard_block'}
                onChange={(v) => run('shock_guard_block', () => api.setShockGuardBlock(v))}
              />
            </div>
            <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
              Off (default) = a shock only raises an alert and a banner on the Status tab. On = the bot also
              skips new entries while the move lasts. Open positions are never touched.
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
                    onClick={() => {
                      const v = parseFloat(chopValue(tf))
                      // 🆕 an empty/invalid field used to send NaN (JSON null) and surface a raw 422
                      if (!Number.isFinite(v)) { setError(`CHOP ${tf}: enter a number between 20 and 90`); return }
                      run(`chop_${tf}`, () => api.setChop(tf, v))
                    }}
                  >
                    Save
                  </button>
                </div>
              </div>
            ))}
          </div>

          <div className="panel">
            <h3 className="panel-title">Hurst Filter</h3>
            <div className="field">
              <label title="Only Hurst > 0.5 + Min Deviation passes. Matches this bot's trend-following A/U entries — mean-reverting regimes (Hurst < 0.5) tend to hurt them, not just add noise. Use Symmetric only if you've added a mean-reversion-style entry elsewhere.">
                Mode
              </label>
              <select
                value={config.hurst_mode}
                disabled={busy === 'hurst_mode'}
                onChange={(e) => run('hurst_mode', () => api.setHurstConfig({ hurst_mode: e.target.value }))}
              >
                <option value="trending_only">Trending Only (recommended)</option>
                <option value="symmetric">Symmetric (trend + mean-rev)</option>
              </select>
            </div>
            <div className="toggle-row">
              <span className="row-label" title="Anis-Lloyd small-sample correction of the R/S Hurst estimate. On: pure noise reads ~0.5 at any window. Off: legacy uncorrected estimate (noise reads ~0.58-0.61) that older windows / Min Deviation were tuned on. Applies from the next scan.">
                Anis-Lloyd correction
              </span>
              <Toggle
                checked={config.hurst_bias_correction ?? true}
                disabled={busy === 'hurst_bias_correction'}
                onChange={(v) => run('hurst_bias_correction', () => api.setHurstBiasCorrection(v))}
              />
            </div>
            <NumberField
              label="Min Deviation from 0.5" hint="0–0.5 — higher = stricter, fewer but more selective signals"
              value={config.hurst_min_deviation} min={0} max={0.5} step={0.01}
              busyKey="hurst_min_dev" busy={busy}
              onSave={(v) => run('hurst_min_dev', () => api.setHurstConfig({ hurst_min_deviation: v }))}
            />
            {Object.keys(config.hurst_window).map((tf) => (
              <div className="field" key={tf}>
                <label title="Bars used for the rolling R/S estimate. The optimal value is pair/TF-specific, not universal — adjust it here and watch A/U Win Rate after a batch of trades closes.">
                  {tf} window (18–300)
                </label>
                <div style={{ display: 'flex', gap: 8 }}>
                  <input
                    type="number"
                    min={18}
                    max={300}
                    step={1}
                    value={hurstWindowValue(tf)}
                    onChange={(e) => setHurstWindowDraft({ ...hurstWindowDraft, [tf]: e.target.value })}
                  />
                  <button
                    className="btn"
                    disabled={busy === `hurst_window_${tf}`}
                    onClick={() => {
                      const v = parseInt(hurstWindowValue(tf), 10)
                      if (!Number.isFinite(v)) { setError(`Hurst window ${tf}: enter a whole number between 18 and 300`); return }
                      run(`hurst_window_${tf}`, () => api.setHurstWindow(tf, v))
                    }}
                  >
                    Save
                  </button>
                </div>
              </div>
            ))}
            <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
              Only applies when the "Hurst regime clarity" toggle above (Signal Filters) is on.
            </p>
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
                    onClick={() => setRemoving(removing === p ? null : p)}
                    disabled={busy === `rm_${p}`}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
            {removing && config.pairs.includes(removing) && (
              <div className="panel" style={{ marginTop: 10, borderColor: 'var(--short)' }}>
                <p style={{ margin: '0 0 8px', fontSize: 13 }}>
                  Stop tracking <b>{removing}</b>? Keep its accumulated signal history in case you re-add it later,
                  or delete the history too (cannot be undone).
                </p>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <button
                    className="btn"
                    disabled={busy === `rm_${removing}`}
                    onClick={() => { const p = removing; setRemoving(null); run(`rm_${p}`, () => api.removePair(p, false)) }}
                  >
                    Remove, keep history
                  </button>
                  <button
                    className="btn"
                    style={{ color: 'var(--short)' }}
                    disabled={busy === `rm_${removing}`}
                    onClick={() => { const p = removing; setRemoving(null); run(`rm_${p}`, () => api.removePair(p, true)) }}
                  >
                    Remove + delete history
                  </button>
                  <button className="btn" onClick={() => setRemoving(null)}>Cancel</button>
                </div>
              </div>
            )}
            <div className="add-pair-form">
              <input
                placeholder="DOGE/USDT"
                value={newPair}
                onChange={(e) => setNewPair(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && newPair.trim()) {
                    run('add_pair', () => api.addPair(newPair.trim())).then((ok) => { if (ok) setNewPair('') })
                  }
                }}
              />
              <button
                className="btn primary"
                disabled={!newPair.trim() || busy === 'add_pair'}
                onClick={() => run('add_pair', () => api.addPair(newPair.trim())).then((ok) => { if (ok) setNewPair('') })}
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
            <h3 className="panel-title">Demand / Supply Zones</h3>
            <div className="toggle-row">
              <span className="row-label" title="A tight consolidation ('base') followed by a strong, high-volume move out of it. Disabling also skips the detector's per-bar work on the live scan, not just the chart overlay.">
                Enabled
              </span>
              <Toggle
                checked={config.zones?.enabled ?? true}
                disabled={busy === 'zones_enabled'}
                onChange={(v) => saveZones('enabled', v)}
              />
            </div>
            <NumberField
              label="ATR period" hint="5–50, for base-tightness and displacement-strength checks"
              value={config.zones?.atr_period ?? 14}
              min={5} max={50} busyKey="zones_atr_period" busy={busy}
              onSave={(v) => saveZones('atr_period', v)}
            />
            <NumberField
              label="Lookback" hint="50–2000 confirmed bars searched for zones"
              value={config.zones?.lookback ?? 300}
              min={50} max={2000} busyKey="zones_lookback" busy={busy}
              onSave={(v) => saveZones('lookback', v)}
            />
            <NumberField
              label="Base bars" hint="2–10, the consolidation window before a displacement move"
              value={config.zones?.base_bars ?? 4}
              min={2} max={10} busyKey="zones_base_bars" busy={busy}
              onSave={(v) => saveZones('base_bars', v)}
            />
            <NumberField
              label="Impulse bars" hint="1–10, bars checked after the base for a displacement move"
              value={config.zones?.impulse_bars ?? 3}
              min={1} max={10} busyKey="zones_impulse_bars" busy={busy}
              onSave={(v) => saveZones('impulse_bars', v)}
            />
            <NumberField
              label="Max zones" hint="1–20, kept per side (demand/supply) after merging and scoring"
              value={config.zones?.max_zones ?? 5}
              min={1} max={20} busyKey="zones_max_zones" busy={busy}
              onSave={(v) => saveZones('max_zones', v)}
            />
            <NumberField
              label="Max base ATR" hint="0.1–5.0, base must be tighter than this many ATRs to count as a base"
              value={config.zones?.max_base_atr ?? 1.6}
              min={0.1} max={5.0} step={0.1} busyKey="zones_max_base_atr" busy={busy}
              onSave={(v) => saveZones('max_base_atr', v)}
            />
            <NumberField
              label="Min displacement ATR" hint="0.1–5.0, minimum move out of the base to count as an impulse"
              value={config.zones?.min_displacement_atr ?? 1.1}
              min={0.1} max={5.0} step={0.1} busyKey="zones_min_displacement_atr" busy={busy}
              onSave={(v) => saveZones('min_displacement_atr', v)}
            />
            <NumberField
              label="Min volume ratio" hint="0.5–5.0, displacement bars' avg volume vs. the rolling mean, minimum to count"
              value={config.zones?.min_volume_ratio ?? 1.15}
              min={0.5} max={5.0} step={0.1} busyKey="zones_min_volume_ratio" busy={busy}
              onSave={(v) => saveZones('min_volume_ratio', v)}
            />
            <p style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 10 }}>
              Shown on the Chart tab (Discord and web) as colored bands with a state label (fresh/tested/weakened/broken).
              Not yet read by confidence scoring or TP capping — informational only, same as Volume Profile above.
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
