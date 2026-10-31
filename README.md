# MUFCA Bot [AtomDC] v4.0

A Discord trading signal bot for Gate.io (Spot & Futures). Scans BTC/USDT and ETH/USDT (and any custom pairs) across 1h and 4h timeframes, fires signals with adaptive TP/SL, and tracks trade outcomes.

---

## Features

- **Dual-track signals** — A-track (Andean Oscillator + MFI K-means) and U-track (UT Bot)
- **HTF Bias filter** — longs only in daily bull market, shorts only in daily bear market (cached 5 min)
- **FRAMA channel** — fractal adaptive MA for trend direction and SL reference
- **Per-timeframe CHOP filter** — 1h: 55, 4h: 61.8 (configurable via `!chop` or the web dashboard)
- **Fake breakout & liquidity sweep filters**
- **Adaptive TP (two-level)** — self-learning from historical MFE with hybrid regime-aware logic:
  - ≥10 signals in current regime → uses regime-specific percentile
  - 5–9 signals → blends regime + general history with discount
  - <5 signals → uses all history with exit-type weighting (TP=1.0, SL_after_TP1=0.8, SL=0.6, cancelled=0.4)
  - ATR cap (2×ATR max) to prevent unrealistic targets
  - **TP1** — pure statistical percentile (no R:R cap), target for closing 50% of the position
  - **TP2** — same distribution but with a minimum R:R 1.5 cap, target for the remaining 50%
- **Adaptive SL** — based on historical MAE of winning trades: 85th percentile of true MAE (price went against the trade but recovered without hitting stop) + a small buffer; falls back to the opposite FRAMA line when there isn't enough history
- **SL after TP1** — once TP1 hits, SL moves to a configurable fraction of the entry→TP1 distance: breakeven (0%), quarter (25%), half (50%, current default), or three-quarter (75%), selectable live from the web dashboard (`tp1_sl_mode.json`) with no restart needed. The same fraction-based formula (`TP1_SL_MODE_FRACTIONS`) is shared by the live path and the backtest, so `!tp1sim` can compare all four against real history without a separate simulation engine
- **TP hit-rate auto-adjust (`TP_AUTO_ADJUST`)** — feedback loop that nudges the TP percentile up/down based on recent real hit-rate vs. target
- **Aggressive / Safe TP modes** — 75th vs 50th percentile, switchable via `!tpconfig mode`
- **On-chain module** — Etherscan (exchange ETH wallet in/outflows) + CoinGecko (Fear & Greed, BTC dominance), refresh interval configurable (15m/30m/1h, default 1h) from the web dashboard; adjusts confidence score, TP/SL multipliers and leverage. Fully optional — disabled automatically if API keys aren't set
- **Derivatives module (futures only)** — funding rate + open interest bias from Gate.io itself (no external API), refresh interval configurable (15m/30m/1h, default 15m — funding/OI move faster than on-chain within a single trading timeframe); extreme funding rate is read as a contrarian signal (crowd one-sided), rising/falling OI as new positioning building/unwinding, scaled by how far OI moved past `OI_DELTA_THRESHOLD`. Combines with on-chain bias with a hard cap on the resulting TP/SL multiplier so the two sources can't compound into an extreme neither would produce alone. On by default; toggle from the web dashboard or `!derivatives`/`!reset_cache`
- **Hurst regime filter** — rolling Hurst exponent (numba-JIT accelerated) as a statistically distinct "second opinion" alongside CHOP: rejects signals when the market is close to a random walk (neither trending nor mean-reverting) regardless of side. **Off by default** — new, opt-in until validated live from the web dashboard's filter toggles
- **Order book spread filter** — live-only gate (order book depth isn't in OHLCV, so unlike every other filter here it can't be backtested — see `spread.py`) against Gate.io's own top-of-book, on both spot and futures. Two relative conditions, not a flat percent-of-price cutoff: spread eating more than `SPREAD_SL_EAT_MAX_PCT` of *this signal's own SL distance*, or spread reading anomalously wide vs. *this pair's own recent rolling median*. **Off by default, but always collecting** — the bot fetches and records the order book every scan tick regardless of the toggle, so the per-pair baseline is already warmed up whenever you decide to switch it on. Preview live with `!spread [pair]` before enabling
- **Volume flow score** — OBV-based, score-based confidence/leverage adjustment
- **Candlestick chart generation (`!chart`)** — dark-themed PNG with FRAMA bands, Bollinger Bands, support/resistance levels, signal arrows, volume panel and K-means MFI panel, attached directly to Discord signal embeds
- **Volume Profile (POC / Value Area)** — TPO-style approximation from OHLCV (see `market_structure.calc_volume_profile()` for why it's an approximation rather than tick-level "real" volume), rendered as a POC line + Value Area band (+ optional histogram) on both the Discord PNG chart and the web dashboard's canvas chart overlay. On by default, configurable bin count/lookback/Value Area % from the web dashboard
- **Market Structure module (`market_structure.py`)** — single shared implementation of Volume Profile, support/resistance, and demand/supply zone detection, used identically by the Discord chart, the web chart, and the live signal path (`get_market_structure()`), so none of them can ever compute different levels for the same market. All structural calculations use only confirmed (closed) bars — the still-forming candle is explicitly excluded before any of them run
  - **Support/Resistance** — pivot-based, clustered by proximity (single-linkage: compared against the nearest already-accepted level in the cluster, not a fixed anchor, so a chain of close pivots merges into one level instead of splintering), each level carries a `touches` count as a significance measure
  - **Demand/Supply zones** — a tight consolidation ("base") immediately followed by a strong, high-volume displacement move out of it; each zone is scored (tightness of the base, size of the displacement, volume confirmation) and tracked as `fresh` / `tested` / `weakened` / `broken` as price revisits it later. Rendered as colored bands with a centered label on both the Discord PNG and the web chart (canvas overlay — `lightweight-charts` has no native primitive for arbitrary rectangles/text). **On by default**, fully configurable (ATR period, lookback, base/impulse bar counts, max zones kept, base tightness, minimum displacement, minimum volume ratio) or toggled off entirely from the web dashboard — informational only right now, not yet read by confidence scoring or TP capping, so disabling it also skips the extra per-bar work on the live scan, not just the chart overlay
- **Relative Strength module (`relative_strength.py`)** — trend of an asset's price ratio against BTC (the natural benchmark for everything else; BTC is self-excluded from comparing against itself), using a plain N-bar return + SMA on the ratio rather than another adaptive layer stacked on top of an already highly adaptive system. Reuses the asset's own already-fetched OHLCV, fetches only the BTC side, and aligns the two series by timestamp (not position) since they're fetched independently
- **TP obstacle cap** — the adaptive TP2 target is capped against the nearest *significant* S/R or Volume Profile level between entry and the raw target (an S/R level needs a minimum touch count to count; POC/VAH/VAL always count), but only if the cap still clears the minimum R:R — a weak or too-close level can never turn a good signal into a nonsensical target. Live-path only, deliberately not applied in the startup/manual backtest, so the adaptive TP training distribution isn't contaminated by its own capping policy. The raw (uncapped) target and the capping reason/level are both recorded alongside the executed trade
- **Confidence scoring (`calc_confidence()`)** — a 0–100 informational score attached to every signal (regime/ATR/FRAMA/signal-confluence/HTF-bias/on-chain components, plus small ±5 `relative_strength`/`volume_profile` nudges from the two modules above), logged into signal history for later analysis. **Does not gate entry** — nothing in the codebase currently thresholds on this score; it exists so a new component's real predictive value can be checked against actual outcomes instead of assumed
- **Raw-outcome tracking (`update_raw_outcome()`)** — continues measuring MFE/MAE for a closed trade for up to `MAX_HOLD_BARS` past its actual exit, independent of whatever TP/SL policy closed it. Exists because a trade's outcome is normally truncated the instant it closes, which makes any *change* to TP/SL policy partly self-referential (the "before" data already reflects the old policy) — raw MFE/MAE is a policy-free read of what the market actually did next, used to validate whether a policy change (or a new confidence component) is really an improvement
- **Historical experiment tooling (`!components`, `!compsim`, `!tp1sim`, matching web dashboard controls)** — rather than waiting weeks for enough live signals to evaluate an idea, these dry-run a backtest against real historical OHLCV and report the same win-rate/PnL breakdown, split by track and market regime:
  - **`!components` / `!compsim`** — does `relative_strength`/`volume_profile` actually correlate with trade outcome, or just add noise? `!components` reads live signal history as it accumulates; `!compsim` replays real historical data (reconstructing both components point-in-time at each simulated entry) instead of waiting
  - **`!tp1sim`** — compares breakeven / quarter / half (current default) / three-quarter SL-after-TP1 policies against real history, tracking (and separately reporting) trades that reached TP1 but timed out before resolving either way, since a looser post-TP1 SL needs more bars to resolve than a tighter one and is otherwise silently under-counted relative to it
  - All of the above are dry-run: nothing is written to `signals_history.json`, and no live setting is changed as a side effect
- **Entry switches + shadow tracking** — Settings → *Entry: A/U-track long/short* turns one track+side off (in TREND only, or in every regime) without a redeploy; open trades are untouched. A blocked signal is not forgotten: it is run through the normal SL/TP/R:R pipeline as a dry run and stored in `data/shadow_signals.json` (never in `signals_history.json`, so it can't train adaptive TP/SL or skew hit-rate stats), then its outcome is replayed from the bars that follow (closed-bar model like the backtest, one position per track + `COOLDOWN_BARS`). `!components shadow` / the Components tab's *Include shadow* box report those outcomes as separate rows, never pooled with real trades — so a switched-off combination can still be re-checked on live data. A **SHDW** lamp in the web top bar shows it at a glance: amber = an Entry switch is active (glows with the number of open shadow positions, flashes when a new one is recorded), dim = switches off again but records kept, hidden = nothing to show; hover for per-track counts and the last record time, click to jump to Insights The backtests (`!compsim`/`!tp1sim`) ignore the switches
- **Web dashboard** — FastAPI + React, served from the same container on port `8585`. Status, Chart, History, and Insights tabs (all live-updated over WebSocket) — Insights groups on-chain/derivatives bias, order book spread, confidence-component analysis, and the historical-experiment tools (`!tp1sim`/`!compsim` equivalents) under one tab with a sub-nav — plus a Settings panel to edit market mode, HTF bias, CHOP thresholds, adaptive TP, SL-after-TP1 mode, Discord notification toggle, scan/on-chain refresh intervals, all indicator parameters (FRAMA/MFI/Andean/UT Bot), Volume Profile, demand/supply zones, and chart colors — no redeploy needed
- **Signal & filter lamps** — top-bar indicator showing live MFI/Andean/UT Bot signal state plus every filter's pass/block direction (including an R:R lamp), computed with the exact same functions the scanner itself uses, so the lamps never disagree with what actually opens a trade
- **Android push notifications** — Firebase Cloud Messaging, device registration/dedup via the web dashboard's Settings panel or the Android app, test-push endpoint to verify the full server → Firebase → device pipeline
- **R:R and bar-low/high guard** — skips signals with R:R < 1.5 (configurable via `MIN_RR`) or SL already hit by bar
- **Trade tracking** — TP1/TP2/SL hit detection, MAE/MFE recording (saved immediately on every change, not throttled), force-close after N bars
- **Startup backtest** — auto-populates signal history from 3000 bars, using the same adaptive TP logic as live signals (`calculate_combined_tp`) so backtest and live are trained on the same distribution
- **Thread-safe file I/O** — atomic writes via `.tmp` files, per-ticker/timeframe async locks
- **Retry fetch** — exponential backoff on Gate.io rate limits
- **Graceful shutdown** — on `SIGTERM`/`SIGINT` the bot flushes all in-memory signal history and active-position state to disk and closes the Discord connection cleanly before exiting
- **Full data persistence** — all settings and history survive container restarts
- **Multi-module architecture** — `config`, `indicators`, `volume_indicators`, `market_structure`, `relative_strength`, `signals`, `state`, `onchain`, `derivatives`, `spread`, `push`, `bot`, `discord_commands`, `chart`, `chart_data`, `web_api`, `embeds`, `utils`

---

## Stack

**Backend**
- Python 3.11
- `discord.py` — bot framework
- `ccxt` — Gate.io API (spot & futures)
- `pandas`, `numpy` — indicator calculations
- `numba` — JIT-compiled rolling Hurst exponent (~500x faster than the pandas/apply equivalent); falls back to pure Python automatically if unavailable
- `matplotlib`, `mplfinance` — candlestick chart rendering (Discord `!chart`)
- `aiohttp` — async Etherscan/CoinGecko requests for the on-chain module
- `firebase-admin` — Android push notifications (Firebase Cloud Messaging)
- `FastAPI` + `uvicorn` — web API, runs in the same asyncio event loop as the Discord bot

**Frontend (`web/`)**
- React 18 + Vite
- `lightweight-charts` — candlestick/indicator chart, WebSocket-driven live updates

**Infra**
- Docker (multi-stage: Node build → Python runtime, single image/container) + GitHub Actions CI/CD

---

## Project Structure

```
mufca-bot/
├── app/
│   ├── main.py               # Entry point — runs Discord bot + web API together, graceful shutdown
│   ├── config.py             # Settings, file I/O helpers, thread locks
│   ├── indicators.py         # ATR, CHOP, FRAMA, MFI, Andean, UT Bot, Heikin Ashi, K-Means
│   ├── volume_indicators.py  # OBV-based volume flow score (confidence & leverage)
│   ├── market_structure.py   # Volume Profile, support/resistance, demand/supply zones — single shared implementation for Discord chart, web chart, and the live signal path
│   ├── relative_strength.py  # Asset-vs-BTC ratio trend (relative strength confidence component)
│   ├── signals.py            # Signal logic, filters, backtest, HTF bias, adaptive TP/SL, TP-obstacle cap, historical-experiment runners (run_tp1_mode_comparison, run_component_backtest)
│   ├── state.py              # Signal history, MFE/MAE tracking, raw-outcome tracking, TP1/TP2 adaptive TP, confidence-component analysis
│   ├── onchain.py            # Etherscan + CoinGecko on-chain bias
│   ├── derivatives.py         # Gate.io funding rate + open interest bias (futures only)
│   ├── spread.py              # Order book spread collection + live liquidity gate (spot & futures)
│   ├── shadow.py              # Shadow tracking of entries an Entry switch blocked (outcome replay, kept out of signals_history)
│   ├── sim_jobs.py            # Background jobs for !tp1sim/!compsim and /api/tp1sim, /api/compsim (one at a time, progress, cancel)
│   ├── sim_pool.py            # Process pool that runs those backtests on all CPU cores (SIM_WORKERS)
│   ├── sim_worker.py          # Worker process entry point used by sim_pool.py
│   ├── push.py               # Android push notifications (Firebase Cloud Messaging)
│   ├── chart.py              # Candlestick chart (PNG) generation for Discord `!chart`
│   ├── chart_data.py         # Same indicator data as chart.py, as JSON for the web dashboard
│   ├── web_api.py            # FastAPI app — status/chart/config/devices endpoints + WebSocket live feed
│   ├── embeds.py             # Discord embed builders for signals
│   ├── discord_commands.py   # All Discord command handlers
│   ├── bot.py                # Bot setup, scanner loop, command registration
│   └── utils.py              # safe_fetch_ohlcv, parse_ohlcv, format_price, round_price, Timer cache
├── web/                       # React + Vite dashboard, built into app/static/ at image build time
│   ├── src/
│   │   ├── App.jsx
│   │   ├── api.js             # REST + WebSocket client
│   │   └── components/        # StatusPanel, ChartPanel, HistoryPanel, InsightsPanel
│   │                           # (Bias/Spread/Components/Simulate sub-views),
│   │                           # SettingsPanel, SignalLamps, LoginScreen
│   └── package.json
├── requirements.txt
├── Dockerfile                 # multi-stage: builds web/ with Node, copies dist/ into the Python image
├── docker-compose.yml
└── .env.example
```

---

## Setup

### 1. Clone & configure

```bash
git clone https://github.com/AtomRz/mufca-bot
cd mufca-bot
cp .env.example .env
```

Edit `.env`:

```env
# Optional — Discord is not required to run the bot (see "Running without
# Discord" below). Leave DISCORD_TOKEN empty to disable it entirely, or set
# DISCORD_ENABLED=false to keep the token configured but temporarily disabled.
DISCORD_TOKEN=your_discord_bot_token
DISCORD_ENABLED=true
CHANNEL_NAME=general

# Optional — enables the on-chain bias module (confidence/TP/SL/leverage adjustments).
# If either key is missing, on-chain analysis is automatically disabled.
ETHERSCAN_API_KEY=
COINGECKO_API_KEY=

# Optional — Android push notifications (Firebase Cloud Messaging).
# Place the service account JSON at the path below (not committed to the repo).
FIREBASE_CREDENTIALS_PATH=/app/data/firebase-credentials.json
```

### 2. Run with Docker

```bash
docker compose up -d
```

The web dashboard is served on **`http://<host>:8585`** by the same container (built into the image from `web/` — no separate frontend container needed).

> **Hardening (optional env vars, see `.env.example`):** `WEB_TRUST_PROXY_HEADERS=1` makes the login lockout use `CF-Connecting-IP`/`X-Forwarded-For` — set it only when all traffic goes through a reverse proxy or Cloudflare Tunnel and port 8585 is not reachable directly (otherwise those headers are forgeable). `WEB_ENABLE_DOCS=1` re-enables `/docs` and `/openapi.json`, which are off by default.

### 3. Run locally

```bash
pip install -r requirements.txt
python main.py
```

### Running without Discord

Discord is entirely optional. The scanner, adaptive TP/SL engine, web dashboard,
WebSocket live feed, and Android push notifications all work identically with
or without it — only the Discord channel messages and `!commands` depend on it.

To run without Discord:
- leave `DISCORD_TOKEN` empty in `.env`, **or**
- keep the token set but add `DISCORD_ENABLED=false`

Either way, no gateway connection is attempted at startup — the container goes
straight to the scanner + web dashboard. This is a startup-time setting: edit
`.env` and restart the container to apply it (it isn't a live web toggle,
since establishing/tearing down the Discord gateway connection isn't something
that can be safely flipped at runtime without a restart).

If Discord *is* enabled, there's a separate, independent toggle in the web
dashboard's Settings panel — **"Discord signal notifications"** — that mutes
just the signal/TP1 channel messages while keeping the gateway connected (so
`!status` and other commands keep working). That one *is* a live toggle, no
restart needed.

---

## Persistent Data

All files are stored in `/app/data/` — mount this path as a host volume to persist across restarts.

| File | Purpose |
|---|---|
| `signals_history.json` | Historical MFE/MAE data for adaptive TP/SL |
| `bot_state_snapshot.json` | Active position snapshot — survives non-graceful restarts (OOM kill, `docker stop -t 0`) |
| `mode.json` | Spot / Futures mode |
| `htf_bias.json` | Active HTF timeframe |
| `ut_ha.json` | UT Bot Heikin Ashi toggle |
| `pairs.json` | Scanned pairs list |
| `chop_threshold.json` | Per-timeframe CHOP thresholds |
| `filter_toggles.json` | Per-filter on/off state — FRAMA, CHOP, ATR, HTF, fake breakout, liquidity sweep, Hurst, order book spread (editable from the web dashboard) |
| `tp_config.json` | TP mode, percentiles, history limit, auto-adjust state |
| `tp1_sl_mode.json` | SL-after-TP1 mode: breakeven / quarter / half (default) / three-quarter of the way to TP1 (editable from the web dashboard) |
| `discord_notifications.json` | Discord channel notifications on/off (editable from the web dashboard; independent of `DISCORD_ENABLED` — see "Running without Discord" above) |
| `shock_guard_block_entries.json` | Market Shock Guard: block new entries during a shock on/off (editable from the web Settings tab; off = warn only) |
| `scan_interval.json` | Scanner poll interval — 15/30/60/180s (editable from the web dashboard) |
| `onchain_interval.json` | On-chain data refresh interval — 15m/30m/1h (editable from the web dashboard) |
| `derivatives.json` | Derivatives module on/off (funding rate + open interest bias, editable from the web dashboard) |
| `derivatives_interval.json` | Derivatives data refresh interval — 15m/30m/1h (editable from the web dashboard) |
| `derivatives_oi_baseline.json` | Previous-cycle open interest per ticker, used to compute the OI delta; written on a throttled interval (not on every fetch) and flushed immediately on graceful shutdown |
| `volume_profile.json` | Volume Profile on/off, bin count, lookback bars, Value Area %, histogram visibility (editable from the web dashboard) |
| `zones.json` | Demand/supply zone detector on/off + all 8 detection parameters (ATR period, lookback, base/impulse bars, max zones, base tightness, min displacement, min volume ratio — editable from the web dashboard). Disabling also skips the detector's per-bar work on the live scan path, not just the chart overlay |
| `spread_history.json` | Rolling per-pair order book spread samples (up to `SPREAD_HISTORY_MAX_SAMPLES`), used for the spread filter's own-history anomaly check; written on a throttled interval and flushed immediately on graceful shutdown — collected regardless of whether the filter is switched on |
| `indicator_config.json` | FRAMA/MFI/Andean/UT Bot parameters (editable from the web dashboard) |
| `chart_colors.json` | Chart color scheme (editable from the web dashboard) |
| `onchain_baseline.json` / `onchain_baseline_ts.json` | Previous-cycle exchange ETH balances + timestamp, used to compute inflow/outflow delta — survives restarts so a restart doesn't delay flow analysis by a full cycle |
| `closure_notified.json` | Trade-closure notification dedup set, capped at the last 2000 entries |
| `devices.json` | Registered Android devices (FCM tokens) for push notifications |

Note: on-chain data other than the baseline balances above (Fear & Greed, BTC dominance, etc.) is cached in-memory only (TTL = the configured on-chain interval) and is not persisted to disk — it rebuilds automatically after a restart.

**Docker Compose volume example:**
```yaml
volumes:
  - /mnt/apps/mufca-data:/app/data
```

---

## Discord Commands

> Everything in this section requires Discord to be enabled (`DISCORD_TOKEN` set and
> `DISCORD_ENABLED` not `false`) — see "Running without Discord" above. Without it,
> use the web dashboard instead; it covers the same functionality.

### 📊 Status & Info

| Command | Description |
|---|---|
| `!status` | Scanner status — all pairs, active trades, TP mode |
| `!pairs` | List currently scanned pairs |
| `!debug` | Debug info — scan count, history size, active trades, volume overview |

### 🔍 Scanning

| Command | Description |
|---|---|
| `!scan [TICKER] [TF]` | Manual scan. Example: `!scan ETH/USDT 4h` |
| `!add SOL/USDT` | Add pair to scanner (auto-runs backtest) |
| `!remove SOL/USDT` | Remove pair from scanner (also clears its derivatives cache/OI baseline and spread history) |
| `!delsignals SOL/USDT [TF] [yes]` | Delete accumulated signal history for a pair (optionally one timeframe only). Preview first, then confirm with `yes` |

### ⚙️ Configuration

| Command | Description |
|---|---|
| `!mode spot` \| `!mode futures` | Switch Gate.io market type |
| `!htf 1d` | Change HTF bias timeframe (`1h`, `4h`, `1d`, `1w`, etc.) |
| `!utha on` \| `!utha off` | Enable/disable Heikin Ashi for UT Bot |
| `!chop` | Show CHOP thresholds per timeframe |
| `!chop 1h 55` | Set CHOP threshold for 1h (default: 55) |
| `!chop 4h 61.8` | Set CHOP threshold for 4h (default: 61.8) |

### 🎯 Adaptive TP

| Command | Description |
|---|---|
| `!tpconfig` | Show full TP configuration |
| `!tpconfig mode safe` | Switch to safe mode (50th %ile — higher win rate) |
| `!tpconfig mode aggressive` | Switch to aggressive mode (75th %ile — higher profit) |
| `!tpconfig limit 30` | Set signal history limit (default: 25) |
| `!tpconfig percentile 70` | Change aggressive percentile |
| `!tpconfig safe 50` | Change safe percentile |
| `!tp long ETH/USDT 4h` | Preview adaptive TP1/TP2/SL without firing a signal |

### 📈 Charts, On-Chain & Derivatives

| Command | Description |
|---|---|
| `!chart [PAIR] [TF] [LIMIT]` | Candlestick PNG with FRAMA, BB, S/R, volume & MFI panels. Example: `!chart ETH 4h 100` |
| `!onchain` | Show current on-chain bias (exchange flows, Fear & Greed, BTC dominance) |
| `!derivatives [PAIR]` | Show current derivatives bias — funding rate + open interest (futures mode only). Example: `!derivatives ETH/USDT` |
| `!spread [PAIR]` | Show current order book spread + filter status/warm-up progress, works even while the filter is off. Example: `!spread ETH/USDT` |
| `!spread [PAIR] reset [yes]` | Reset one pair's spread warm-up history (preview first, confirm with `yes`) |
| `!reset_cache` | Clear the HTF bias, on-chain, and derivatives data caches and force a fresh fetch (deliberately does not touch spread history — see `!spread reset`) |

### 📚 History & Stats

| Command | Description |
|---|---|
| `!signals` | Signal history summary for all pairs |
| `!signals ETH/USDT` | Signal history for a specific pair |
| `!signals ETH/USDT 4h long` | Detailed history for pair/TF/side |
| `!history` | Live trade history for all pairs (in-memory, resets on restart) |
| `!history ETH/USDT 4h` | Trade history for specific pair/TF |
| `!components [min_n] [split] [shadow]` | Does the `relative_strength`/`volume_profile` confidence component actually correlate with outcome, on live signal history so far? (default `min_n=30`). `split` also buckets by track and regime; `shadow` adds separate rows for entries an Entry switch blocked (see below) |

### 🧬 Historical Experiments

Dry-run — replay real historical OHLCV instead of waiting on live signals to accumulate. Nothing is written to `signals_history.json`, no live setting is changed.

| Command | Description |
|---|---|
| `!compsim [pair] [tf] [bars] [min_n]` | `!components`, sourced from a backtest instead of live history — reconstructs `relative_strength`/`volume_profile` point-in-time at each simulated entry |
| `!tp1sim [pair] [tf] [bars]` | Compares breakeven/quarter/half/three-quarter SL-after-TP1 on real historical data, split by track/regime, with timed-out (reached TP1, never resolved within `MAX_HOLD_BARS`) trades tracked separately so a looser SL isn't silently under-counted relative to a tighter one |

Both run as a **background job on all CPU cores** (see `SIM_WORKERS` in `.env.example`) and only one simulation runs at a time — a second request is refused until the first finishes. The bot posts a progress line about every 90 s. Worker processes run at lower priority, so the live scanner is not starved.

### 🧪 Testing

| Command | Description |
|---|---|
| `!forcerun long ETH/USDT 4h` | Force-fire signal bypassing all filters (always opens on the A-track, by design — see `discord_commands.py`) |
| `!reset yes` | Clear all signal history and re-run backtest |

---

## Web Dashboard

FastAPI + React, served from the same container as the bot on port **`8585`** — no separate deployment, no duplicate exchange fetches (the dashboard reads the same `bot.state` and `config` the scanner already computed each scan cycle).

**Tabs:**
- **Status** — scanner stats, active A-track/U-track trades per pair/timeframe, live-updated over WebSocket
- **Chart** — candlestick chart (FRAMA channel, Bollinger Bands, support/resistance, demand/supply zones with state labels, Volume Profile, volume, MFI with K-means overbought/oversold bands), per pair/timeframe/track, auto-refreshes on each scan tick and new signal
- **History** — win-rate/PnL/MFE/MAE summary per pair/timeframe/side/track, plus a grand-total row and a drill-down into individual closed trades
- **Insights** — sub-nav across four views:
  - **Bias** — on-chain bias snapshot (ETH exchange flow, Fear & Greed, BTC dominance) and derivatives bias (funding rate, open interest), plus the resulting TP/SL/leverage multipliers
  - **Spread** — live order book spread + filter status/warm-up progress
  - **Components** — `!components`' report, in-browser
  - **Simulate** — `!tp1sim`/`!compsim`, in-browser: pick a pair/timeframe/bar count, run, see the result as a table instead of Discord text. Runs as a background job with a progress bar and a Cancel button; the result is kept for a few hours, so it survives a reload or a tab switch (`POST /api/tp1sim` / `/api/compsim` return `202` + a job id, poll `GET /api/sim-jobs/{id}`; `DELETE` cancels)
- **Settings** — everything below is editable at runtime, no redeploy or restart required:
  - Market mode (spot/futures), HTF bias, UT Bot Heikin Ashi toggle
  - SL-after-TP1 mode (breakeven / quarter / half / three-quarter of the way to TP1)
  - Discord signal notifications on/off
  - Scanner poll interval (15/30/60/180s), on-chain refresh interval (15m/30m/1h), and derivatives refresh interval (15m/30m/1h)
  - Derivatives module on/off (futures only — funding rate + open interest bias)
  - Signal filter toggles (FRAMA, CHOP, ATR, HTF, fake breakout, liquidity sweep, Hurst regime filter, order book spread filter — Hurst and spread off by default)
  - Per-timeframe CHOP threshold
  - Adaptive TP mode/percentiles/history limit
  - Tracked pairs (add/remove, with an option to also purge accumulated signal history)
  - All indicator parameters — FRAMA length/multiplier, MFI length/K-means training size, Andean length/signal smoothing/confirmation window, UT Bot sensitivity/ATR period, Bollinger Bands length/StdDev, S/R pivot window/max levels shown
  - Chart colors (FRAMA, Bollinger, support, resistance, MFI line, MFI overbought/oversold, candle up/down, TP/SL lines, long/short signal markers)
  - Volume Profile — on/off, bin count, lookback bars, Value Area %, histogram visibility
  - Demand/Supply zones — on/off (also gates the live-path computation, not just the chart), ATR period, lookback, base/impulse bar counts, max zones kept, base tightness, minimum displacement, minimum volume ratio
  - Android push — registered device count/names, send a test push

Changing market mode, HTF bias, or any indicator parameter resets active position tracking state — same behavior as the equivalent Discord commands, since these changes affect what "warmed up" and "in-progress signal" mean for the scanner.

**Top bar:** live connection status, market mode, HTF bias, CHOP/trend/suggested-leverage pulse for the pair selected on the Chart tab, and a row of signal/filter lamps (MFI, Andean, UT Bot, plus every filter's pass/block state — including R:R) computed with the exact same logic the scanner uses to decide whether a trade actually opens.

**REST API** (all under `/api/`): `status`, `health`, `pairs` (GET/POST/DELETE), `chart`, `pulse`, `derivatives`, `spread`, `shadow` (lamp status), `components` (`?split=1&shadow=1`), `tp1sim` (POST), `compsim` (POST), `config` (GET full config; POST `mode`/`htf`/`tp1-sl-mode`/`entry-block`/`discord-notifications`/`scan-interval`/`onchain-interval`/`derivatives-enabled`/`derivatives-interval`/`utha`/`filters`/`chop`/`tpconfig`/`indicators`/`volume-profile`/`zones`/`colors`), `onchain`, `history/summary`, `history/records`, `devices` (GET/POST/DELETE, plus `devices/test-push`), `ws-ticket`. **WebSocket** `/ws/live` (short-lived ticket auth) pushes `signal`, `tp1_hit`, `limit_filled`, `limit_cancelled`, `scan_tick`, and `config_changed` events.

---

## Indicator Parameters

Defaults below (all editable at runtime from the **web dashboard → Settings**, persisted in `indicator_config.json`):

| Parameter | Value | Description |
|---|---|---|
| ATR Period | 14 | Wilder's RMA — matches Pine Script `ta.atr()` |
| ATR Min/Max | 0.3% / 4.5% | Volatility filter range |
| CHOP Threshold | 1h: 55, 4h: 61.8 | Sideways market filter (per timeframe) |
| FRAMA Length | 22 | Fractal Adaptive MA period |
| FRAMA Multiplier | 2.1 | Channel width |
| MFI Length | 8 | Money Flow Index period |
| MFI Training | 800 | K-means clustering history |
| Andean Length | 23 | Andean Oscillator period |
| Andean Signal | 6 | Signal line smoothing |
| Confirmation Window | 3 | MFI + Andean must align within N bars |
| UT Sensitivity | 1.0 | UT Bot ATR multiplier |
| UT Period | 10 | UT Bot ATR period |
| Bollinger Period | 20 | Bollinger Bands length |
| Bollinger StdDev | 2.0 | Bollinger Bands standard deviation multiplier |
| S/R Pivot Window | 10 | Bars needed on each side to confirm a pivot |
| S/R Max Levels | 4 | Max support/resistance levels shown per side |
| Cooldown Bars | 2 | Min bars between signals per track |
| HTF Bias | 1D | Higher timeframe for trend filter |
| Max Hold Bars | 20 | Force-close after N bars |
| Max Leverage | 10x | Safety cap |
| SL Risk | 5% | Target risk per trade for leverage calc |
| SL MAE Percentile | 85% | Percentile of historical winning-trade MAE used for adaptive SL |
| SL MAE Buffer | 0.2% | Extra padding added on top of the MAE percentile |
| SL Min Distance | 0.1% | Minimum SL distance from entry, regardless of adaptive/ATR calculation |
| Min R:R | 1.5 | Minimum R:R required to open a signal; also the minimum R:R cap applied to TP2 |
| Hurst Window | 100 | Bars used to estimate the rolling Hurst exponent (filter off by default) |
| Hurst Min Deviation | 0.12 | Reject if `abs(hurst - 0.5) < this` (`trending_only`: if `hurst - 0.5 < this`). The estimate is Anis–Lloyd bias-corrected, so pure noise reads ≈0.5 at any window; the legacy uncorrected R/S read ≈0.58–0.61 on noise, which is why this value used to be set so high — turn off "Anis–Lloyd correction" in web Settings → Hurst Filter (or set env `HURST_BIAS_CORRECTION=0` as the first-boot default) to get that estimate back. Re-tune after switching: on the corrected scale a window of 100–150 with 0.05–0.08 is a sensible start, while windows under ~50 stay very noisy even when corrected (sd of H ≈ 0.35 at 21–23 bars) |
| Funding Rate Threshold | 0.05% | Funding rate beyond this (either direction) reads as "crowd one-sided" — contrarian bias against that side |
| OI Delta Threshold | 3% | Open interest change beyond this within one derivatives refresh window reads as a meaningful positioning shift, not routine noise |
| Combined Mult Cap | 1.20 | Hard cap on the on-chain × derivatives combined TP/SL multiplier, so the two independent sources can't compound past what either was calibrated for on its own |
| Spread SL-Eat Max | 15% | Order book spread filter: block if spread exceeds this fraction of the signal's own SL distance (live scan only, off by default) |
| Spread Anomaly Mult | 3.0x | Order book spread filter: block if spread exceeds this multiple of the pair's own rolling median spread (needs `Spread Min Samples` warm-up) |
| Spread Min Samples | 30 | Minimum warm-up samples per pair before the anomaly condition activates; the SL-eat condition needs no warm-up |
| Scan Interval | 60s | Scanner poll interval — 15/30/60/180s |
| On-chain Interval | 1h | On-chain data refresh interval — 15m/30m/1h |
| Derivatives Interval | 15m | Derivatives (funding/OI) data refresh interval — 15m/30m/1h |

---

## Adaptive TP / SL Logic

On startup the bot backtests 3000 historical bars per pair/timeframe using the **same adaptive TP logic as live** (`calculate_combined_tp`) to accumulate clean, consistently-trained MFE/MAE history — backtest and live signals are never trained on different TP distributions. Live signals then use percentiles of this distribution as TP/SL targets.

**Hybrid regime-aware TP logic:**
1. ≥10 signals in current market regime (TREND/CHAOS/NORMAL) → uses only regime signals
2. 5–9 regime signals → blends with general history at 0.85× discount
3. <5 regime signals → uses all signals with exit-type weighting
4. ATR cap: TP cannot exceed 2×ATR from entry

**Two-level TP:**
- **TP1** — statistical percentile of MFE, no R:R cap, closes 50% of the position
- **TP2** — same percentile logic but with a minimum R:R of 1.5, closes the remaining 50%
- If on-chain adjustment changes TP2, TP1 is recomputed proportionally so it never ends up past TP2

**SL after TP1:** once TP1 hits, SL moves to either breakeven (entry) or halfway to TP1 (configurable, live, from the web dashboard). A trade that closes this way is labeled `sl_after_tp1` — a distinct, partial-success outcome (weighted between a full "tp" and a full "sl" in the hit-rate/percentile calibration), not counted as a plain loss.

**Modes:**
- **Aggressive** (default): 75th percentile
- **Safe**: 50th percentile (median)

Fallback to fixed R:R 2.0 when fewer than 3 closed signals exist.

**TP hit-rate auto-adjust:** once at least 15 recent signals are recorded and `TP_AUTO_ADJUST` is on, the active percentile is nudged up or down automatically to keep the real hit-rate close to the configured target.

**Adaptive SL:** the stop is set from the 85th percentile of historical MAE on *winning* trades, plus a small fixed buffer. If there isn't enough history yet, SL falls back to the opposite FRAMA line.

**On-chain & derivatives adjustment:** when enabled, exchange-flow bias and market-wide sentiment from `onchain.py`, combined with funding rate + open interest bias from `derivatives.py` (futures only), can multiply TP/SL distances and shift leverage before the signal is finalized. The two sources' TP/SL multipliers are combined multiplicatively then hard-capped (`COMBINED_MULT_CAP = 1.20`) so they can't compound past what either alone was calibrated for; downstream safety caps also ensure the combined result can never push R:R below the configured minimum.

---

## Signal Logic (Pine Script parity)

All signals fire on the **last confirmed closed bar** (`iloc[-2]`) — no repainting.

**Filter chain for LONG:**
1. FRAMA direction = BULL
2. FRAMA slope > 0
3. CHOP index < threshold
4. ATR% within min/max range
5. HTF daily bias = BULL
6. No fake breakout (high > 10-bar high but closed below)
7. No liquidity sweep short
8. R:R ≥ 1.5 (`MIN_RR`)
9. Bar low above SL
10. *(off by default)* Hurst regime filter — direction-agnostic, rejects when the market reads as statistically close to a random walk
11. *(off by default; live scan path only, not backtestable)* Order book spread filter — rejects when the current spread eats too much of this signal's own SL distance, or reads anomalously wide vs. the pair's recent history

Each of steps 1–2, 3, 4, 5, 6, 7, 10, 11 can be individually toggled on/off from the web dashboard's Settings panel (Signal Filters) — the top-bar filter lamps reflect these toggles live.

**A-track** — MFI crosses oversold AND Andean crosses signal line within `LOOKBACK=3` bars.

**U-track** — UT Bot trailing stop crossover, only on new bar.


**UT Bot fix** — ATR always calculated from regular candles even in Heikin Ashi mode.

---

## Architecture

```
main.py (single asyncio event loop)
├── Discord bot (bot.start, optional — see "Running without Discord")
│   └── market_scanner (every 15–180s, configurable)
│       ├── get_htf_bias()              — cached 5 min per ticker
│       ├── safe_fetch_ohlcv()          — retry with exponential backoff
│       ├── check_signals()             — all indicators on iloc[-2]
│       │   ├── check_tp_sl_hit()       — every scan
│       │   ├── update_signal_mae_mfe() — saved immediately on change
│       │   ├── bars_in_trade++         — only on new bar
│       │   ├── R:R + bar guard         — skip bad signals
│       │   └── calculate_combined_tp() — hybrid regime-aware adaptive TP
│       ├── live TP1/TP2/SL check       — via fetch_ticker, catches intrabar hits
│       ├── send Discord embed          — only on new bar_time
│       ├── send Android push           — via Firebase Cloud Messaging
│       └── broadcast_event()           — pushes signal/tp1_hit/scan_tick to web dashboard over WebSocket
└── Web API (uvicorn, port 8585) — reads the same state/config, no duplicate fetches

SIGTERM/SIGINT → flush signals_history.json + bot_state_snapshot.json to disk → close Discord connection → exit
```

---

## CI/CD

GitHub Actions builds a Docker image on push to `main` and deploys to TrueNAS Scale via SSH.

Required secrets: `TRUENAS_HOST`, `TRUENAS_USER`, `TRUENAS_SSH_KEY`.
