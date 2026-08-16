# MUFCA Bot [AtomDC] v4.0

A Discord trading signal bot for Gate.io (Spot & Futures) that replicates the **MUFCA v1.5 Elite** Pine Script indicator in Python. Scans BTC/USDT and ETH/USDT (and any custom pairs) across 1h and 4h timeframes, fires signals with adaptive TP/SL, and tracks trade outcomes.

---

## Features

- **Dual-track signals** — A-track (Andean Oscillator + MFI K-means) and U-track (UT Bot)
- **HTF Bias filter** — longs only in daily bull market, shorts only in daily bear market (cached 5 min)
- **FRAMA channel** — fractal adaptive MA for trend direction and SL reference
- **Per-timeframe CHOP filter** — 1h: 55, 4h: 61.8 (configurable via `!chop`)
- **Fake breakout & liquidity sweep filters**
- **Adaptive TP (two-level)** — self-learning from historical MFE with hybrid regime-aware logic:
  - ≥10 signals in current regime → uses regime-specific percentile
  - 5–9 signals → blends regime + general history with discount
  - <5 signals → uses all history with exit-type weighting (TP=1.0, SL=0.6, cancelled=0.4)
  - ATR cap (3×ATR max) to prevent unrealistic targets
  - **TP1** — pure statistical percentile (no R:R cap), target for closing 50% of the position
  - **TP2** — same distribution but with a minimum R:R 1.5 cap, target for the remaining 50%
- **Adaptive SL** — based on historical MAE of winning trades: 85th percentile of true MAE (price went against the trade but recovered without hitting stop) + a small buffer; falls back to the opposite FRAMA line when there isn't enough history
- **TP hit-rate auto-adjust (`TP_AUTO_ADJUST`)** — feedback loop that nudges the TP percentile up/down based on recent real hit-rate vs. target
- **Aggressive / Safe TP modes** — 75th vs 50th percentile, switchable via `!tpconfig mode`
- **On-chain module** — Etherscan (exchange ETH wallet in/outflows) + CoinGecko (Fear & Greed, BTC dominance, volume change), hourly cache; adjusts confidence score, TP/SL multipliers and leverage. Fully optional — disabled automatically if API keys aren't set
- **Volume flow score** — OBV-based, score-based confidence/leverage adjustment
- **Candlestick chart generation (`!chart`)** — dark-themed PNG with FRAMA bands, Bollinger Bands, support/resistance levels, signal arrows, volume panel and K-means MFI panel, attached directly to Discord signal embeds
- **Web dashboard** — FastAPI + React, served from the same container on port `8585`. Live status (per-pair/timeframe active trades), an interactive chart (candles, FRAMA channel, Bollinger Bands, support/resistance, volume, MFI with overbought/oversold bands) pushed over WebSocket, and a Settings UI to edit market mode, HTF bias, CHOP thresholds, adaptive TP, all indicator parameters (FRAMA/MFI/Andean/UT Bot), and chart colors — no redeploy needed
- **R:R and bar-low/high guard** — skips signals with R:R < 1.0 or SL already hit by bar
- **Trade tracking** — TP1/TP2/SL hit detection, MAE/MFE recording (saved immediately on every change, not throttled), force-close after N bars
- **Startup backtest** — auto-populates signal history from 3000 bars, using the same adaptive TP logic as live signals (`calculate_combined_tp`) so backtest and live are trained on the same distribution
- **Thread-safe file I/O** — atomic writes via `.tmp` files, per-ticker/timeframe async locks
- **Retry fetch** — exponential backoff on Gate.io rate limits
- **Graceful shutdown** — on `SIGTERM`/`SIGINT` the bot flushes all in-memory signal history to disk and closes the Discord connection cleanly before exiting
- **Full data persistence** — all settings and history survive container restarts
- **Multi-module architecture** — `config`, `indicators`, `signals`, `state`, `bot`, `discord_commands`, `chart`, `chart_data`, `web_api`, `embeds`, `onchain`, `volume_indicators`, `utils`

---

## Stack

**Backend**
- Python 3.11
- `discord.py` — bot framework
- `ccxt` — Gate.io API (spot & futures)
- `pandas`, `numpy` — indicator calculations
- `matplotlib`, `mplfinance` — candlestick chart rendering (Discord `!chart`)
- `aiohttp` — async Etherscan/CoinGecko requests for the on-chain module
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
│   ├── signals.py            # Signal logic, filters, backtest, HTF bias, adaptive TP/SL
│   ├── state.py              # Signal history, MFE/MAE tracking, TP1/TP2 adaptive TP
│   ├── onchain.py            # Etherscan + CoinGecko on-chain bias
│   ├── chart.py              # Candlestick chart (PNG) generation for Discord `!chart`
│   ├── chart_data.py         # Same indicator data as chart.py, as JSON for the web dashboard
│   ├── web_api.py            # FastAPI app — status/chart/config endpoints + WebSocket live feed
│   ├── embeds.py             # Discord embed builders for signals
│   ├── discord_commands.py   # All Discord command handlers
│   ├── bot.py                # Bot setup, scanner loop, command registration
│   └── utils.py              # safe_fetch_ohlcv, parse_ohlcv, Timer cache
├── web/                       # React + Vite dashboard, built into app/static/ at image build time
│   ├── src/
│   │   ├── App.jsx
│   │   ├── api.js             # REST + WebSocket client
│   │   └── components/        # StatusPanel, ChartPanel, SettingsPanel
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
```

### 2. Run with Docker

```bash
docker compose up -d
```

The web dashboard is served on **`http://<host>:8585`** by the same container (built into the image from `web/` — no separate frontend container needed).

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
| `tp_config.json` | TP mode, percentiles, history limit, auto-adjust state |
| `tp1_sl_mode.json` | SL-after-TP1 mode: breakeven or half-way-to-TP1 (editable from the web dashboard) |
| `discord_notifications.json` | Discord channel notifications on/off (editable from the web dashboard; independent of `DISCORD_ENABLED` — see "Running without Discord" above) |
| `indicator_config.json` | FRAMA/MFI/Andean/UT Bot parameters (editable from the web dashboard) |
| `chart_colors.json` | Chart color scheme (editable from the web dashboard) |
| `onchain_baseline.json` | Previous-cycle exchange ETH balances, used to compute inflow/outflow delta — survives restarts so a restart doesn't delay flow analysis by a full cycle |
| `closure_notified.json` | Trade-closure notification dedup set, capped at the last 2000 entries |

Note: on-chain data other than the baseline balances above (Fear & Greed, BTC dominance, etc.) is cached in-memory only (TTL 1h) and is not persisted to disk — it rebuilds automatically after a restart.

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
| `!debug` | Debug info — scan count, history size, active trades |

### 🔍 Scanning

| Command | Description |
|---|---|
| `!scan [TICKER] [TF]` | Manual scan. Example: `!scan ETH/USDT 4h` |
| `!add SOL/USDT` | Add pair to scanner (auto-runs backtest) |
| `!remove SOL/USDT` | Remove pair from scanner |

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

### 📈 Charts & On-Chain

| Command | Description |
|---|---|
| `!chart [PAIR] [TF] [LIMIT]` | Candlestick PNG with FRAMA, BB, S/R, volume & MFI panels. Example: `!chart ETH 4h 100` |
| `!onchain` | Show current on-chain bias (exchange flows, Fear & Greed, BTC dominance) |
| `!resetcache` | Clear the on-chain data cache and force a fresh fetch |

### 📚 History & Stats

| Command | Description |
|---|---|
| `!signals` | Signal history summary for all pairs |
| `!signals ETH/USDT` | Signal history for a specific pair |
| `!signals ETH/USDT 4h long` | Detailed history for pair/TF/side |
| `!history` | Live trade history for all pairs (in-memory, resets on restart) |
| `!history ETH/USDT 4h` | Trade history for specific pair/TF |

### 🧪 Testing

| Command | Description |
|---|---|
| `!sim long ETH/USDT 4h` | Simulate signal entry+exit (adds to TP training history) |
| `!forcerun long ETH/USDT 4h` | Force-fire signal bypassing all filters |
| `!reset yes` | Clear all signal history and re-run backtest |

---

## Web Dashboard

FastAPI + React, served from the same container as the bot on port **`8585`** — no separate deployment, no duplicate exchange fetches (the dashboard reads the same `bot.state` and `config` the scanner already computed each 60s pass).

**Tabs:**
- **Status** — scanner stats, active A-track/U-track trades per pair/timeframe, live-updated over WebSocket
- **Chart** — candlestick chart (FRAMA channel, Bollinger Bands, support/resistance, volume, MFI with K-means overbought/oversold bands), per pair/timeframe/track, auto-refreshes on each scan tick and new signal
- **Settings** — everything below is editable at runtime, no redeploy or restart required:
  - Market mode (spot/futures), HTF bias, UT Bot Heikin Ashi toggle
  - Per-timeframe CHOP threshold
  - Adaptive TP mode/percentiles/history limit
  - Tracked pairs (add/remove)
  - All indicator parameters — FRAMA length/multiplier, MFI length/K-means training size, Andean length/signal smoothing/confirmation window, UT Bot sensitivity/ATR period
  - Chart colors (FRAMA, Bollinger, support, resistance, MFI line, MFI overbought/oversold, candle up/down)

Changing market mode, HTF bias, or any indicator parameter resets active position tracking state — same behavior as the equivalent Discord commands, since these changes affect what "warmed up" and "in-progress signal" mean for the scanner.

**REST API** (all under `/api/`): `status`, `pairs`, `chart`, `config` (GET full config; POST `mode`/`htf`/`utha`/`chop`/`tpconfig`/`indicators`/`colors`). **WebSocket** `/ws/live` pushes `signal`, `scan_tick`, and `config_changed` events.

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
| Cooldown Bars | 2 | Min bars between signals per track |
| HTF Bias | 1D | Higher timeframe for trend filter |
| Max Hold Bars | 20 | Force-close after N bars |
| Max Leverage | 10x | Safety cap |
| SL Risk | 5% | Target risk per trade for leverage calc |
| SL MAE Percentile | 85% | Percentile of historical winning-trade MAE used for adaptive SL |
| SL MAE Buffer | 0.2% | Extra padding added on top of the MAE percentile |
| Min TP2 R:R | 1.5 | Minimum R:R cap applied to TP2 |

---

## Adaptive TP / SL Logic

On startup the bot backtests 3000 historical bars per pair/timeframe using the **same adaptive TP logic as live** (`calculate_combined_tp`) to accumulate clean, consistently-trained MFE/MAE history — backtest and live signals are never trained on different TP distributions. Live signals then use percentiles of this distribution as TP/SL targets.

**Hybrid regime-aware TP logic:**
1. ≥10 signals in current market regime (TREND/CHAOS/NORMAL) → uses only regime signals
2. 5–9 regime signals → blends with general history at 0.85× discount
3. <5 regime signals → uses all signals with exit-type weighting
4. ATR cap: TP cannot exceed 3×ATR from entry

**Two-level TP:**
- **TP1** — statistical percentile of MFE, no R:R cap, closes 50% of the position
- **TP2** — same percentile logic but with a minimum R:R of 1.5, closes the remaining 50%
- If on-chain adjustment changes TP2, TP1 is recomputed proportionally so it never ends up past TP2

**Modes:**
- **Aggressive** (default): 75th percentile
- **Safe**: 50th percentile (median)

Fallback to fixed R:R 2.0 when fewer than 3 closed signals exist.

**TP hit-rate auto-adjust:** once at least 15 recent signals are recorded and `TP_AUTO_ADJUST` is on, the active percentile is nudged up or down automatically to keep the real hit-rate close to the configured target.

**Adaptive SL:** the stop is set from the 85th percentile of historical MAE on *winning* trades (synthetic `!sim` entries are excluded so they don't skew real market behavior), plus a small fixed buffer. If there isn't enough history yet, SL falls back to the opposite FRAMA line.

**On-chain adjustment:** when `ONCHAIN_ENABLED`, exchange-flow bias and market-wide sentiment from `onchain.py` can multiply TP/SL distances and shift leverage before the signal is finalized, with safety caps so on-chain data can never push R:R below the configured minimum.

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
8. R:R ≥ 1.0
9. Bar low above SL

**A-track** — MFI crosses oversold AND Andean crosses signal line within `LOOKBACK=3` bars.

**U-track** — UT Bot trailing stop crossover, only on new bar.

**UT Bot fix** — ATR always calculated from regular candles even in Heikin Ashi mode.

---

## Architecture

```
main.py (single asyncio event loop)
├── Discord bot (bot.start)
│   └── market_scanner (every 60s)
│       ├── get_htf_bias()              — cached 5 min per ticker
│       ├── safe_fetch_ohlcv()          — retry with exponential backoff
│       ├── check_signals()             — all indicators on iloc[-2]
│       │   ├── check_tp_sl_hit()       — every scan
│       │   ├── update_signal_mae_mfe() — saved immediately on change
│       │   ├── bars_in_trade++         — only on new bar
│       │   ├── R:R + bar guard         — skip bad signals
│       │   └── calculate_combined_tp() — hybrid regime-aware adaptive TP
│       ├── send Discord embed          — only on new bar_time
│       └── broadcast_event()           — pushes signal/scan_tick to web dashboard over WebSocket
└── Web API (uvicorn, port 8585) — reads the same state/config, no duplicate fetches

SIGTERM/SIGINT → flush signals_history.json to disk → close Discord connection → exit
```

---

## CI/CD

GitHub Actions builds a Docker image on push to `main` and deploys to TrueNAS Scale via SSH.

Required secrets: `TRUENAS_HOST`, `TRUENAS_USER`, `TRUENAS_SSH_KEY`.
