# MUFCA Bot [AtomDC] v4.0

A Discord trading signal bot for Gate.io (Spot & Futures) that replicates the **MUFCA v1.5 Elite** Pine Script indicator in Python. Scans BTC/USDT and ETH/USDT (and any custom pairs) across 1h and 4h timeframes, fires signals with adaptive TP/SL, and tracks trade outcomes.

---

## Features

- **Dual-track signals** — A-track (Andean Oscillator + MFI K-means) and U-track (UT Bot)
- **HTF Bias filter** — longs only in daily bull market, shorts only in daily bear market (cached 5 min)
- **FRAMA channel** — fractal adaptive MA for trend direction and SL reference
- **Per-timeframe CHOP filter** — 1h: 55, 4h: 61.8 (configurable via `!chop`)
- **Fake breakout & liquidity sweep filters**
- **Adaptive TP** — self-learning from historical MFE with hybrid regime-aware logic:
  - ≥10 signals in current regime → uses regime-specific percentile
  - 5–9 signals → blends regime + general history with discount
  - <5 signals → uses all history with exit-type weighting (TP=1.0, SL=0.6, cancelled=0.4)
  - ATR cap (3×ATR max) to prevent unrealistic targets
- **Aggressive / Safe TP modes** — 75th vs 50th percentile, switchable via `!tpconfig mode`
- **R:R and bar-low/high guard** — skips signals with R:R < 1.0 or SL already hit by bar
- **Trade tracking** — TP/SL hit detection, MAE/MFE recording, force-close after N bars
- **Startup backtest** — auto-populates signal history from 3000 bars using fixed R:R 2.0
- **Thread-safe file I/O** — atomic writes via `.tmp` files, per-file mutex locks
- **Retry fetch** — exponential backoff on Gate.io rate limits
- **Full data persistence** — all settings and history survive container restarts
- **Multi-module architecture** — `config`, `indicators`, `signals`, `state`, `bot`, `utils`

---

## Stack

- Python 3.11
- `discord.py` — bot framework
- `ccxt` — Gate.io API (spot & futures)
- `pandas`, `numpy` — indicator calculations
- Docker + GitHub Actions CI/CD

---

## Project Structure

```
mufca_bot/
├── main.py          # Entry point
├── config.py        # Settings, file I/O helpers, thread locks
├── indicators.py    # ATR, CHOP, FRAMA, MFI, Andean, UT Bot, Heikin Ashi, K-Means
├── signals.py       # Signal logic, filters, backtest, HTF bias
├── state.py         # Signal history, MFE/MAE tracking, adaptive TP
├── bot.py           # All Discord commands
├── utils.py         # safe_fetch_ohlcv, parse_ohlcv, Timer cache
├── requirements.txt
└── .env
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
DISCORD_TOKEN=your_discord_bot_token
CHANNEL_NAME=general
```

### 2. Run with Docker

```bash
docker compose up -d
```

### 3. Run locally

```bash
pip install -r requirements.txt
python main.py
```

---

## Persistent Data

All files are stored in `/app/data/` — mount this path as a host volume to persist across restarts.

| File | Purpose |
|---|---|
| `signals_history.json` | Historical MFE/MAE data for adaptive TP |
| `mode.json` | Spot / Futures mode |
| `htf_bias.json` | Active HTF timeframe |
| `ut_ha.json` | UT Bot Heikin Ashi toggle |
| `pairs.json` | Scanned pairs list |

**Docker Compose volume example:**
```yaml
volumes:
  - /mnt/apps/mufca-data:/app/data
```

---

## Discord Commands

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
| `!tp long ETH/USDT 4h` | Preview adaptive TP/SL without firing a signal |

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

## Indicator Parameters

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

---

## Adaptive TP Logic

On startup the bot backtests 3000 historical bars per pair/timeframe using **fixed R:R 2.0** to accumulate clean MFE history. Live signals then use a percentile of this distribution as TP target.

**Hybrid regime-aware logic:**
1. ≥10 signals in current market regime (TREND/CHAOS/NORMAL) → uses only regime signals
2. 5–9 regime signals → blends with general history at 0.85× discount
3. <5 regime signals → uses all signals with exit-type weighting
4. ATR cap: TP cannot exceed 3×ATR from entry

**Modes:**
- **Aggressive** (default): 75th percentile
- **Safe**: 50th percentile (median)

Fallback to fixed R:R 2.0 when fewer than 3 closed signals exist.

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
market_scanner (every 20s)
├── get_htf_bias()              — cached 5 min per ticker
├── safe_fetch_ohlcv()          — retry with exponential backoff
├── check_signals()             — all indicators on iloc[-2]
│   ├── check_tp_sl_hit()       — every scan
│   ├── bars_in_trade++         — only on new bar
│   ├── R:R + bar guard         — skip bad signals
│   └── calculate_combined_tp() — hybrid regime-aware adaptive TP
└── send Discord embed           — only on new bar_time
```

---

## CI/CD

GitHub Actions builds a Docker image on push to `main` and deploys to TrueNAS Scale via SSH.

Required secrets: `TRUENAS_HOST`, `TRUENAS_USER`, `TRUENAS_SSH_KEY`.
