# 🤖 MUFCA Bot [AtomDC] v3.0

Discord bot for scanning cryptocurrency pairs on Gate.io, generating signals based on the **MUFCA [AtomDC]** indicator logic.

---

## 📊 Indicator Logic (identical to Pine Script)

| Component | Description |
|---|---|
| **FRAMA Channel** | Fractal Adaptive Moving Average — trend detection |
| **HTF Bias** | Higher timeframe filter (4H for 1H, 1D for 4H) |
| **Andean + MFI (A-track)** | KMeans clustering of MFI + Andean Oscillator crossovers |
| **UT Bot (U-track)** | ATR Trailing Stop — fast trend entries |
| **CHOP Filter** | Blocks signals during sideways market |
| **ATR Filter** | Volatility filter (min/max ATR%) |
| **Fake Breakout Filter** | Blocks false breakout signals |
| **Liquidity Sweep Filter** | Blocks signals after liquidity sweeps |
| **Cooldown** | Minimum 2 bars between signals on the same track |
| **Position Guard** | Prevents re-entry while already in a position |
| **AI Confidence** | 0–100% score based on filter confluence |

---

## 💬 Discord Commands

### 📋 Pair Management
| Command | Description | Example |
|---|---|---|
| `!pairs` | Show current list of scanned pairs | `!pairs` |
| `!add TICKER` | Add a pair to the scanner | `!add SOL/USDT` |
| `!remove TICKER` | Remove a pair from the scanner | `!remove SOL/USDT` |

### 🔍 Scanning
| Command | Description | Example |
|---|---|---|
| `!scan TICKER TF` | Manual signal request | `!scan ETH/USDT 1h` |
| `!status` | Show all pairs, positions and last bar times | `!status` |

### ⚙️ Exchange Settings
| Command | Description | Example |
|---|---|---|
| `!mode` | Show current market mode (spot/futures) | `!mode` |
| `!mode spot` | Switch to Gate.io spot market | `!mode spot` |
| `!mode futures` | Switch to Gate.io perpetual futures | `!mode futures` |

> ⚠️ Switching market mode resets all position states. Pair list and tickers remain unchanged.

---

## 🚀 Quick Start

### 1. Clone the repository
```bash
git clone https://github.com/AtomRz/mufca-bot.git
cd mufca-bot
```

### 2. Create .env file
```bash
cp .env.example .env
nano .env
```
```
DISCORD_TOKEN=your_token_here
CHANNEL_NAME=general
```

### 3. Run with Docker
```bash
docker compose up -d
```

### 4. View logs
```bash
docker compose logs -f
```

### 5. Update
```bash
git pull
docker compose up -d --build
```

---

## 🖥️ TrueNAS Scale (Custom App)

1. **Apps → Custom App**
2. Paste the contents of `docker-compose.yml`
3. **Environment Variables:**
   - `DISCORD_TOKEN` = your Discord bot token
   - `CHANNEL_NAME` = general
4. **Restart Policy:** `Unless Stopped`

---

## 📁 File Structure

```
mufca-bot/
├── mufca_v3.py        # Main bot code
├── Dockerfile         # Docker image
├── docker-compose.yml # Docker Compose config
├── requirements.txt   # Python dependencies
├── .env.example       # Example .env file
├── .gitignore         # Git ignore rules
├── pairs.json         # Saved pairs list (auto-created)
└── mode.json          # Saved market mode (auto-created)
```

---

## 📦 Dependencies

```
ccxt==4.3.89
discord.py==2.3.2
pandas==2.2.2
numpy==1.26.4
scikit-learn==1.5.0
python-dotenv==1.0.1
```

---

## ⚙️ Indicator Parameters (defaults)

| Parameter | Value |
|---|---|
| ATR Period | 14 |
| ATR Min % | 0.3 |
| ATR Max % | 4.5 |
| CHOP Length | 14 |
| CHOP Threshold | 61.8 |
| FRAMA Length | 22 |
| FRAMA Multiplier | 2.1 |
| MFI Length | 8 |
| MFI Training Size | 800 |
| Andean Length | 23 |
| Andean Signal Smoothing | 6 |
| Confirmation Window | 3 bars |
| Cooldown Bars | 2 |
| UT Sensitivity | 1.0 |
| UT ATR Period | 10 |
| Max Leverage | 10x |

---

*MUFCA [AtomDC] v3.0 — identical logic to the Pine Script indicator*
