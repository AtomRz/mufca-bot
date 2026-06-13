# MUFCA Bot [AtomDC] v3.1

A Discord trading signal bot for Gate.io (Spot & Futures) that replicates the **MUFCA v1.5 Elite** Pine Script indicator logic in Python. Monitors ETH/USDT and BTC/USDT (and any custom pairs) across multiple timeframes, fires signals with adaptive TP/SL, and tracks trade outcomes.

---

## Features

- **Dual-track signals** — A-track (Andean Oscillator + MFI K-means) and U-track (UT Bot)
- **HTF Bias filter** — only longs in daily bull market, shorts in daily bear market
- **FRAMA channel** — dynamic trend/SL reference
- **CHOP filter** — per-timeframe sideways market detection (1h: 55, 4h: 61.8)
- **Fake breakout & liquidity sweep filters**
- **Adaptive TP** — self-learning from historical signal MFE (percentile-based), with aggressive/safe modes
- **Trade tracking** — TP/SL hit detection, MAE/MFE recording, force-close after N bars
- **Startup backtest** — auto-populates signal history from 3000 bars on startup
- **HTF cache** — 5-minute cache for HTF bias requests to reduce API load
- **Fully containerized** — Docker + GitHub Actions CI/CD for TrueNAS Scale deployment

---

## Stack

- Python 3.11
- `discord.py` — bot framework
- `ccxt` — Gate.io API (spot & futures)
- `pandas`, `numpy` — indicator calculations
- Docker + GitHub Actions

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
python mufca_v3.py
```

---

## Persistent files

| File | Purpose |
|---|---|
| `pairs.json` | Scanned pairs list |
| `signals_history.json` | Historical signal MFE/MAE for adaptive TP |
| `htf_bias.json` | Active HTF timeframe setting |
| `ut_ha.json` | UT Bot Heikin Ashi toggle |
| `mode.json` | Spot / Futures mode |

---

## Discord Commands

### 📊 Status & Info

| Command | Description |
|---|---|
| `!status` | Scanner status — all pairs, active positions, TP mode |
| `!pairs` | List currently scanned pairs |
| `!debug` | Debug info — scan count, signal history size, active trades |

### 🔍 Scanning

| Command | Description |
|---|---|
| `!scan [TICKER] [TF]` | Manual scan. Example: `!scan ETH/USDT 4h` |
| `!add SOL/USDT` | Add pair to scanner |
| `!remove SOL/USDT` | Remove pair from scanner |

### ⚙️ Configuration

| Command | Description |
|---|---|
| `!mode spot` \| `!mode futures` | Switch between Gate.io Spot and Futures |
| `!htf 1d` | Change HTF bias timeframe (`1h`, `4h`, `1d`, `1w`, etc.) |
| `!utha on` \| `!utha off` | Enable/disable Heikin Ashi candles for UT Bot |
| `!chop` | Show current CHOP thresholds per timeframe |
| `!chop 1h 55` | Set CHOP threshold for 1h (default: 55) |
| `!chop 4h 61.8` | Set CHOP threshold for 4h (default: 61.8) |

### 🎯 Adaptive TP

| Command | Description |
|---|---|
| `!tpconfig` | Show full TP configuration |
| `!tpconfig mode safe` | Switch to safe mode (50th percentile — higher win rate) |
| `!tpconfig mode aggressive` | Switch to aggressive mode (75th percentile — higher profit) |
| `!tpconfig limit 30` | Set signal history limit (default: 25) |
| `!tpconfig percentile 70` | Change aggressive percentile |
| `!tpconfig safe 50` | Change safe percentile |
| `!tp long ETH/USDT 4h` | Preview adaptive TP/SL for a pair without firing a signal |

### 📚 History & Stats

| Command | Description |
|---|---|
| `!signals` | Signal history summary for all pairs |
| `!signals ETH/USDT` | Signal history for a specific pair |
| `!signals ETH/USDT 4h long` | Detailed signal history for pair/TF/side |
| `!history` | Trade history (TP/SL hits) for all pairs |
| `!history ETH/USDT 4h` | Trade history for specific pair/TF |

### 🧪 Testing

| Command | Description |
|---|---|
| `!sim long ETH/USDT 4h` | Simulate a signal entry+exit (adds to history for TP training) |
| `!forcerun long ETH/USDT 4h` | Force-fire a signal bypassing all filters (testing only) |
| `!reset yes` | Clear all signal history and re-run backtest |

---

## Indicator Parameters

| Parameter | Value | Description |
|---|---|---|
| ATR Period | 14 | Volatility measurement |
| ATR Min/Max | 0.3% / 4.5% | Volatility filter range |
| CHOP Threshold | 1h: 55, 4h: 61.8 | Sideways market filter |
| FRAMA Length | 22 | Fractal Adaptive MA period |
| FRAMA Multiplier | 2.1 | Channel width in ATR |
| MFI Length | 8 | Money Flow Index period |
| MFI Training | 800 | K-means clustering history size |
| Andean Length | 23 | Andean Oscillator period |
| Andean Signal | 6 | Signal line smoothing |
| Confirmation Window | 3 | MFI + Andean must align within N bars |
| UT Sensitivity | 1.0 | UT Bot ATR multiplier |
| UT Period | 10 | UT Bot ATR period |
| Cooldown Bars | 2 | Minimum bars between signals (per track) |
| HTF Bias | 1D | Higher timeframe for trend filter |
| Max Hold Bars | 20 | Force-close after N bars |
| Max Leverage | 10x | Safety cap on recommended leverage |
| SL Risk | 5% | Target risk per trade for leverage calc |

---

## Adaptive TP Logic

On startup the bot runs a backtest over 3000 historical bars per pair/timeframe, recording the Max Favorable Excursion (MFE) of each signal. Live signals use a percentile of this distribution as TP target:

- **Aggressive mode** (default): 75th percentile — TP reached in ~25% of historical moves
- **Safe mode**: 50th percentile (median) — TP reached in ~50% of historical moves

If fewer than 3 closed signals exist in history, falls back to fixed 2:1 R:R.

---

## Signal Logic (Pine Script parity)

Signals fire only on a **confirmed closed bar** (`iloc[-2]`), never on the live bar. This prevents repainting.

**Filter chain for LONG:**
1. FRAMA direction = BULL (close > FRAMA upper band)
2. FRAMA slope > 0
3. CHOP index < threshold (trend confirmed)
4. ATR% within min/max range
5. HTF bias = BULL (daily close > daily FRAMA)
6. No fake breakout (high > 10-bar high but closed below)
7. No liquidity sweep short (high > 5-bar high, closed below, bearish bar)

**A-track** fires when MFI crosses oversold level AND Andean oscillator crosses signal line within `LOOKBACK` bars of each other.

**U-track** fires when UT Bot trailing stop is crossed (only on new bar, not every scan).

---

## Architecture

```
market_scanner (every 20s)
├── get_htf_bias()          — cached 5min per ticker
├── check_signals()         — indicators on iloc[-2]
│   ├── check_tp_sl_hit()   — every scan
│   ├── bars_in_trade++     — only on new bar (is_new_bar)
│   └── fire signals        — only on new bar for U-track
└── send Discord embed      — only on new bar_time
```

---

## CI/CD

GitHub Actions workflow builds a Docker image on push to `main` and deploys to TrueNAS Scale via SSH. Secrets required: `TRUENAS_HOST`, `TRUENAS_USER`, `TRUENAS_SSH_KEY`.
