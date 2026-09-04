import os
import json
import threading
import logging
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# =====================================================================
# ⚙️  BASIC SETTINGS
# =====================================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
# Discord is entirely optional: with no token, or DISCORD_ENABLED=false, the
# bot runs scanner + web dashboard only — no gateway connection is attempted,
# no channel messages, no !commands. See bot.py's ensure_engine_started() /
# main.py for how startup no longer depends on Discord's on_ready firing.
DISCORD_ENABLED = bool(DISCORD_TOKEN) and os.getenv("DISCORD_ENABLED", "true").strip().lower() != "false"

# 🆕 Basic Auth for the web UI. If neither is set, the dashboard stays open
# (for local development), but a loud warning is logged on startup.
WEB_USERNAME = os.getenv("WEB_USERNAME", "")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "general")

# 🆕 Push notifications to Android via Firebase Cloud Messaging.
# The service account file is NOT committed to the repo (it's public!) — it's
# placed manually on the server, in the same persistent volume as
# signals_history.json.
FIREBASE_CREDENTIALS_PATH = os.getenv("FIREBASE_CREDENTIALS_PATH", "/app/data/firebase-credentials.json")

DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)

# =====================================================================
# 🔑  EXTERNAL API KEYS
# =====================================================================
ETHERSCAN_API_KEY  = os.getenv("ETHERSCAN_API_KEY", "")
COINGECKO_API_KEY  = os.getenv("COINGECKO_API_KEY", "")   # Demo key (free)

# =====================================================================
# 📡  ON-CHAIN SETTINGS
# =====================================================================
ONCHAIN_FLOW_THRESHOLD_ETH    = 5_000   # ETH — threshold for a "normal" signal
ONCHAIN_FLOW_THRESHOLD_LARGE_ETH = 20_000  # ETH — threshold for a "large" signal
ONCHAIN_ENABLED               = bool(ETHERSCAN_API_KEY and COINGECKO_API_KEY)

# =====================================================================
# 🔒  THREAD-SAFE FILE OPERATIONS
# =====================================================================

_file_locks: dict = {}
_file_locks_lock = threading.Lock()

def get_file_lock(filename: str) -> threading.Lock:
    with _file_locks_lock:
        if filename not in _file_locks:
            _file_locks[filename] = threading.Lock()
        return _file_locks[filename]

def safe_json_load(filename: str, default: dict = None) -> dict:
    if default is None:
        default = {}
    lock = get_file_lock(filename)
    with lock:
        try:
            with open(filename, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return default  # normal scenario — first run, no file yet
        except Exception as e:
            # 🆕 FIX: previously ANY error (corrupted JSON, no read permission,
            # disk unavailable) was silently swallowed and returned default —
            # the same behavior as an honest "file doesn't exist yet". The
            # difference is that the former is a real data loss/unavailability
            # worth knowing about, not something to silently continue past
            # with an empty config.
            logger.error(f"[CONFIG] Failed to load {filename}, using default: {e}")
            return default

def safe_json_save(filename: str, data: dict):
    lock = get_file_lock(filename)
    with lock:
        try:
            temp_file = filename + ".tmp"
            with open(temp_file, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(temp_file, filename)
        except Exception as e:
            logger.error(f"Failed to save {filename}: {e}")

# 🆕 Refresh interval for on-chain data (Etherscan/CoinGecko), configurable
# from the web UI following the same pattern as SCAN_INTERVAL_SECONDS — live
# changes without a container restart. Used to be hardcoded to 3600s in two
# places (onchain.py _cache_get + bot.py market_scanner), now there's a
# single source of truth.
# Lives here (not in the ON-CHAIN SETTINGS block above) because it depends on
# safe_json_load/safe_json_save, defined just above.
ONCHAIN_INTERVAL_OPTIONS = (900, 1800, 3600)  # 15m / 30m / 1h
ONCHAIN_INTERVAL_FILE = os.path.join(DATA_DIR, "onchain_interval.json")

def load_onchain_interval() -> int:
    data = safe_json_load(ONCHAIN_INTERVAL_FILE, {"seconds": 3600})
    seconds = data.get("seconds", 3600)
    return seconds if seconds in ONCHAIN_INTERVAL_OPTIONS else 3600

def save_onchain_interval(seconds: int):
    if seconds not in ONCHAIN_INTERVAL_OPTIONS:
        raise ValueError(f"onchain interval must be one of {ONCHAIN_INTERVAL_OPTIONS}, got {seconds}")
    safe_json_save(ONCHAIN_INTERVAL_FILE, {"seconds": seconds})

ONCHAIN_CACHE_TTL = load_onchain_interval()   # seconds — balance refresh interval (default 1h)

# =====================================================================
# 📉  DERIVATIVES (Funding Rate + Open Interest) — futures-only
# =====================================================================
# Only meaningful for perpetual futures (MARKET_MODE == "futures") — funding
# rate and open interest don't exist for spot trading. Persisted toggle
# (default ON, unlike the opt-in Hurst filter) — this only ever ADDS a bias
# adjustment on top of on-chain, the same class of signal Atom is already
# running, not a new gating mechanism that could block trades outright.
DERIVATIVES_FILE = os.path.join(DATA_DIR, "derivatives.json")

def load_derivatives_enabled() -> bool:
    data = safe_json_load(DERIVATIVES_FILE, {"enabled": True})
    return bool(data.get("enabled", True))

def save_derivatives_enabled(enabled: bool):
    safe_json_save(DERIVATIVES_FILE, {"enabled": bool(enabled)})

DERIVATIVES_ENABLED = load_derivatives_enabled()

DERIVATIVES_INTERVAL_OPTIONS = (900, 1800, 3600)  # 15m / 30m / 1h — same choices as on-chain
DERIVATIVES_INTERVAL_FILE = os.path.join(DATA_DIR, "derivatives_interval.json")

def load_derivatives_interval() -> int:
    data = safe_json_load(DERIVATIVES_INTERVAL_FILE, {"seconds": 900})
    seconds = data.get("seconds", 900)
    return seconds if seconds in DERIVATIVES_INTERVAL_OPTIONS else 900

def save_derivatives_interval(seconds: int):
    if seconds not in DERIVATIVES_INTERVAL_OPTIONS:
        raise ValueError(f"derivatives interval must be one of {DERIVATIVES_INTERVAL_OPTIONS}, got {seconds}")
    safe_json_save(DERIVATIVES_INTERVAL_FILE, {"seconds": seconds})

# 🆕 Shorter default than on-chain (15m vs 1h) — funding rate and OI move
# meaningfully within a single 1h/4h trading timeframe, unlike ETH exchange
# balances, which only shift enough to matter over longer windows.
DERIVATIVES_CACHE_TTL = load_derivatives_interval()

# Funding rate beyond this (as a fraction, e.g. 0.0005 = 0.05%) is read as
# "crowd is one-sided" — contrarian bias against that side. Typical Gate.io
# perpetual funding sits in a much narrower band most of the time; this
# threshold marks a genuine outlier, not routine funding noise.
FUNDING_RATE_EXTREME_THRESHOLD = 0.0005

# Open Interest change (as a fraction of the previous snapshot) beyond this,
# within one DERIVATIVES_CACHE_TTL window, is read as a meaningful shift in
# positioning (not just normal noise) — feeds into lev_delta the same way
# on-chain flow strength does.
OI_DELTA_THRESHOLD = 0.03

# =====================================================================
# 📊  PAIRS TO SCAN
# =====================================================================
DEFAULT_TICKERS = ["BTC/USDT", "ETH/USDT"]
PAIRS_FILE = os.path.join(DATA_DIR, "pairs.json")

def load_tickers() -> List[str]:
    data = safe_json_load(PAIRS_FILE, {"tickers": DEFAULT_TICKERS})
    return data.get("tickers", DEFAULT_TICKERS)

def save_tickers(tickers: List[str]):
    safe_json_save(PAIRS_FILE, {"tickers": tickers})

TICKERS: List[str] = load_tickers()

# =====================================================================
# ⏱️  TIMEFRAME SETTINGS
# =====================================================================
TIMEFRAMES = ["1h", "4h"]

# =====================================================================
# 🔧  FILTER TOGGLES (mirrors Pine Script input.bool)
# 🆕 These used to be plain True constants hardcoded in the code — they
# couldn't be turned off from the UI. Now, like HTF_BIAS/UT_HEIKIN_ASHI, they
# persist to JSON and are read via _cfg.ENABLE_X (NOT a bare import —
# otherwise, after a runtime change via /api/config/filters, the old imported
# copies in signals.py and chart_data.py would stay frozen at their original
# value).
# =====================================================================
FILTERS_FILE = os.path.join(DATA_DIR, "filter_toggles.json")

DEFAULT_FILTER_TOGGLES = {
    "frama": True,       # use_frama_filter
    "chop": True,        # use_chop
    "atr": True,         # use_atr_f
    "htf": True,         # enable_mtf_bias
    "fake_break": True,  # 🆕 used to always apply unconditionally, no input.bool equivalent
    "liq_sweep": True,   # 🆕 used to always apply unconditionally, no input.bool equivalent
    "hurst": False,      # 🆕 off by default — new, unproven filter; opt-in until validated live
    "spread": False,     # 🆕 off by default — starts in warm-up/logging-only mode, see SPREAD section below
}

def load_filter_toggles() -> dict:
    data = safe_json_load(FILTERS_FILE, DEFAULT_FILTER_TOGGLES)
    # in case the file is missing some keys (e.g. after a new filter was added)
    return {**DEFAULT_FILTER_TOGGLES, **data}

def save_filter_toggles(toggles: dict):
    safe_json_save(FILTERS_FILE, toggles)

_filter_toggles = load_filter_toggles()
ENABLE_FRAMA_FILTER      = _filter_toggles["frama"]       # use_frama_filter
ENABLE_CHOP_FILTER       = _filter_toggles["chop"]        # use_chop
ENABLE_ATR_FILTER        = _filter_toggles["atr"]         # use_atr_f
ENABLE_MTF_BIAS          = _filter_toggles["htf"]         # enable_mtf_bias
ENABLE_FAKE_BREAK_FILTER = _filter_toggles["fake_break"]
ENABLE_LIQ_SWEEP_FILTER  = _filter_toggles["liq_sweep"]
ENABLE_HURST_FILTER      = _filter_toggles["hurst"]
ENABLE_SPREAD_FILTER     = _filter_toggles["spread"]

# =====================================================================
# 📈  HURST EXPONENT (regime-clarity filter)
# =====================================================================
# A statistically distinct "second opinion" alongside CHOP — see
# indicators.calculate_hurst() for the full explanation. Direction-agnostic:
# rejects signals when the market is close to a random walk (neither
# trending nor mean-reverting in a statistically meaningful way), regardless
# of which side the signal is on. Not split per-track (A vs U) — that's a
# further refinement worth trying later once this baseline version has been
# validated live.
HURST_WINDOW = 100          # bars used to estimate the rolling Hurst exponent
# 🆕 R/S Hurst estimation is known to be biased upward on finite samples
# (a pure random walk tends to measure somewhat above the theoretical 0.5,
# not near it — confirmed empirically against synthetic random-walk data
# during testing). MIN_DEVIATION is set conservatively with that bias in
# mind, not as a naive "0.5 ± X".
HURST_MIN_DEVIATION = 0.12  # reject if abs(hurst - 0.5) < this

# =====================================================================
# 📏  ORDER BOOK SPREAD (live liquidity gate) — futures & spot
# =====================================================================
# Order book depth isn't part of OHLCV, so unlike Hurst/CHOP/FRAMA this
# can't be backtested against history — the bot only ever sees the spread
# at the moment it fetches the book, not what it was on some past bar. That
# means this filter can only gate the LIVE scan path (bot.py -> check_signals'
# spread_info param), never backtest_history(): there, spread_info is always
# None and this gate is a structural no-op — not a bug, just the nature of
# the data.
#
# Two independent conditions, either one can trip the gate once enabled:
#   1) "eats too much of this signal's own SL risk" — needs no warm-up,
#      computable from the very first reading (spread_pct vs this signal's
#      sl_distance_pct).
#   2) "anomalously wide vs this pair's own recent history" — needs
#      SPREAD_MIN_SAMPLES_FOR_ANOMALY samples before it's meaningful; until
#      then it's simply skipped (treated as "not an anomaly"), not blocked
#      outright and not treated as a NaN-style failure. This is the
#      "warm-up" period Atom asked for: with the toggle OFF, bot.py still
#      fetches + records the order book every scan tick (see spread.py), so
#      by the time the toggle is flipped ON, the rolling-median baseline
#      per pair is already populated instead of starting cold.
#
# A flat percent-of-price threshold was deliberately NOT used (see chat
# discussion) — a major like BTC and a thin alt don't share a normal spread
# range, so a single absolute cutoff would be miscalibrated for one side of
# that split no matter where it's set. Both conditions below are relative
# instead: relative to this signal's own risk, and relative to this pair's
# own recent behavior.
SPREAD_HISTORY_FILE = os.path.join(DATA_DIR, "spread_history.json")
SPREAD_HISTORY_MAX_SAMPLES = 500          # rolling samples kept per ticker
SPREAD_MIN_SAMPLES_FOR_ANOMALY = 30       # below this, condition 2) is skipped (still warming up)
SPREAD_ANOMALY_MULT = 3.0                 # condition 2): current spread > this x rolling median
SPREAD_SL_EAT_MAX_PCT = 0.15              # condition 1): spread_pct > this x sl_distance_pct
SPREAD_HISTORY_SAVE_INTERVAL = 300        # seconds — throttled disk write, same pattern as OI baseline

# =====================================================================
# 🎯  SIGNAL TRACKS
# =====================================================================
# Single source of truth for the set of signal tracks — previously "a"/"u"
# were hardcoded as a literal 2-tuple in ~10 places across signals.py,
# bot.py, discord_commands.py, etc. Adding "b" (Breakout) here and nowhere
# else would have meant hunting down every one of those spots by hand and
# risking missing one (exactly the class of bug already caught twice this
# session with !remove not clearing derivatives/spread caches). New code
# should iterate TRACKS / look up TRACK_LABELS instead of hardcoding.
TRACKS = ("a", "u", "b")
TRACK_LABELS = {"a": "A", "u": "U", "b": "B"}                     # single-letter, used in "A BUY", "U SELL", etc.
TRACK_NAMES = {"a": "Andean+MFI", "u": "UT Bot", "b": "Breakout"}  # descriptive, used in "(Andean+MFI)", etc.
TRACK_FULL_NAMES = {"a": "Andean", "u": "UT Bot", "b": "Breakout"} # used in Discord/web track selectors

# =====================================================================
# 🚀  BREAKOUT TRACK (track "b") — volatility-squeeze + volume/range expansion
# =====================================================================
# Catches the pattern A/U structurally lag on: a long low-volatility
# consolidation followed by a sudden, volume-backed directional break — by
# the time FRAMA's slope turns or MFI/Andean confluence lines up, several
# bars of the move are often already gone (see chat: Atom's screenshot of
# exactly this pattern). Draws on a few classic volume/breakout
# methodologies rather than inventing one from scratch:
#   - Wyckoff (Sign of Strength / Effort vs Result): wide-spread, high-volume
#     bar closing near its extreme is the tell, not volume in isolation.
#   - VSA (Volume Spread Analysis, Tom Williams): explicitly pairs volume
#     WITH bar range — big volume on a narrow range can mean absorption
#     (bearish/bullish trap), not genuine strength, so both must expand
#     together.
#   - NR7 / volatility squeeze (Crabel, and the common Bollinger/Keltner
#     "squeeze" variant): requires a genuine compression BEFORE the breakout
#     bar, not just any big candle — otherwise this would just re-detect
#     ordinary volatility in the middle of an existing trend.
#   - Darvas box: the breakout must clear the actual high/low of the
#     pre-squeeze range, not just look big relative to ATR.
#
# Deliberately bypasses the CHOP and FRAMA-direction filters for this track
# specifically (see the filter_long_b/filter_short_b comment in signals.py)
# — CHOP would still read "choppy" from the tail of the just-ended squeeze
# right as the breakout starts, and FRAMA's slope is exactly what's lagging
# in this pattern; gating on either would defeat the track's purpose. ATR
# bounds, HTF bias, fake-break/liquidity-sweep, Hurst, and spread filters
# still apply — those catch different failure modes (manipulation wicks,
# thin-book execution risk) that a strong squeeze breakout is not immune to.
BREAKOUT_FILE = os.path.join(DATA_DIR, "breakout_config.json")
_BREAKOUT_DEFAULTS = {
    "BREAKOUT_LOOKBACK": 20,             # bars defining the pre-breakout range (Darvas box high/low)
    "BREAKOUT_SQUEEZE_WINDOW": 60,       # longer window the ATR% percentile is measured against
    "BREAKOUT_SQUEEZE_PERCENTILE": 0.25, # ATR% just before the breakout bar must be in the bottom 25% of BREAKOUT_SQUEEZE_WINDOW
    "BREAKOUT_VOL_SPIKE_MULT": 2.5,      # breakout bar's volume vs its own 20-bar SMA (relative volume)
    "BREAKOUT_RANGE_ATR_MULT": 1.5,      # breakout bar's (high-low) vs ATR14 — "Result" side of Wyckoff's effort-vs-result
    "BREAKOUT_CLOSE_LOC_MIN": 0.70,      # close must sit in the top/bottom 30% of the breakout bar's own range
}

def load_breakout_config() -> dict:
    data = safe_json_load(BREAKOUT_FILE, _BREAKOUT_DEFAULTS)
    # in case the file is from before this was persisted — fill in any missing keys with defaults
    return {**_BREAKOUT_DEFAULTS, **data}

def save_breakout_config(data: dict):
    safe_json_save(BREAKOUT_FILE, data)

_breakout = load_breakout_config()
BREAKOUT_LOOKBACK = _breakout["BREAKOUT_LOOKBACK"]
BREAKOUT_SQUEEZE_WINDOW = _breakout["BREAKOUT_SQUEEZE_WINDOW"]
BREAKOUT_SQUEEZE_PERCENTILE = _breakout["BREAKOUT_SQUEEZE_PERCENTILE"]
BREAKOUT_VOL_SPIKE_MULT = _breakout["BREAKOUT_VOL_SPIKE_MULT"]
BREAKOUT_RANGE_ATR_MULT = _breakout["BREAKOUT_RANGE_ATR_MULT"]
BREAKOUT_CLOSE_LOC_MIN = _breakout["BREAKOUT_CLOSE_LOC_MIN"]

# =====================================================================
# 📈  INDICATOR PARAMETERS
# =====================================================================
ATR_PERIOD = 14
ATR_MIN = 0.3
ATR_MAX = 4.5
CHOP_LENGTH = 14
CHOP_FILE = os.path.join(DATA_DIR, "chop_threshold.json")

def load_chop() -> dict:
    data = safe_json_load(CHOP_FILE, {"1h": 55.0, "4h": 61.8})
    return data

def save_chop(data: dict):
    safe_json_save(CHOP_FILE, data)

CHOP_THRESHOLD: dict = load_chop()

INDICATOR_FILE = os.path.join(DATA_DIR, "indicator_config.json")
_INDICATOR_DEFAULTS = {
    "FRAMA_LEN": 22,
    "FRAMA_MULT": 2.1,
    "MFI_LEN": 8,
    "MFI_TRAINING": 800,
    "AND_LEN": 23,
    "AND_SIG_LEN": 6,
    "LOOKBACK": 3,
    "UT_SENSITIVITY": 1.0,
    "UT_PERIOD": 10,
    "BB_PERIOD": 20,
    "BB_STDDEV": 2.0,
    "SR_PIVOT_WINDOW": 10,
    "SR_MAX_LEVELS": 4,
}

def load_indicators() -> dict:
    data = safe_json_load(INDICATOR_FILE, _INDICATOR_DEFAULTS)
    # in case the file is from an older version — fill in any missing keys with defaults
    merged = {**_INDICATOR_DEFAULTS, **data}
    return merged

def save_indicators(data: dict):
    safe_json_save(INDICATOR_FILE, data)

_indicators = load_indicators()
FRAMA_LEN = _indicators["FRAMA_LEN"]
FRAMA_MULT = _indicators["FRAMA_MULT"]
MFI_LEN = _indicators["MFI_LEN"]
MFI_TRAINING = _indicators["MFI_TRAINING"]
AND_LEN = _indicators["AND_LEN"]
AND_SIG_LEN = _indicators["AND_SIG_LEN"]
LOOKBACK = _indicators["LOOKBACK"]
UT_SENSITIVITY = _indicators["UT_SENSITIVITY"]
UT_PERIOD = _indicators["UT_PERIOD"]
BB_PERIOD = _indicators["BB_PERIOD"]
BB_STDDEV = _indicators["BB_STDDEV"]
SR_PIVOT_WINDOW = _indicators["SR_PIVOT_WINDOW"]
SR_MAX_LEVELS = _indicators["SR_MAX_LEVELS"]
# 🆕 Minimum history depth for finding S/R pivots in get_chart_data() —
# independent of the UI's selected barsLimit (100/200/300/500 bars). Levels
# formed deeper than the visible window still need to be included in the
# candidate sample, otherwise they're physically unreachable at a small
# barsLimit.
SR_MIN_LOOKBACK = 900

# =====================================================================
# 📊  VOLUME PROFILE (POC / Value Area)
# =====================================================================
# Approximated from OHLCV — see chart.calc_volume_profile() for why this is
# a TPO-style approximation rather than a tick-level "real" volume profile.
VP_FILE = os.path.join(DATA_DIR, "volume_profile.json")
_VP_DEFAULTS = {
    "enabled": True,
    "bins": 50,               # price buckets across the lookback window's range
    "lookback": 300,          # bars used to build the profile (independent of chart display limit)
    "value_area_pct": 0.70,   # fraction of volume that defines the Value Area around POC
    "show_histogram": True,   # False = keep the POC line + Value Area band, drop the histogram bars
}

def load_vp_config() -> dict:
    data = safe_json_load(VP_FILE, _VP_DEFAULTS)
    # fill in any keys missing from an older config file with defaults
    return {**_VP_DEFAULTS, **data}

def save_vp_config(data: dict):
    safe_json_save(VP_FILE, data)

_vp_config = load_vp_config()
VP_ENABLED = _vp_config["enabled"]
VP_BINS = _vp_config["bins"]
VP_LOOKBACK = _vp_config["lookback"]
VP_VALUE_AREA_PCT = _vp_config["value_area_pct"]
VP_SHOW_HISTOGRAM = _vp_config["show_histogram"]

COOLDOWN_BARS = 2
MAX_ALLOWED_LEV = 10
TARGET_RISK_DEP = 5.0
# 🆕 Minimum R:R to open a trade (used to be a local MIN_RR=1.5 inside
# check_signals() — moved into config since it's now also used in
# get_market_pulse() for the topbar R:R indicator; single source of truth.
MIN_RR = 1.5

# =====================================================================
# 🎯  SL-MOVE MODE AFTER TP1
# =====================================================================
# "breakeven" — SL = entry (zero risk on the remainder, but more often catches
#               a final shakeout retrace before continuing to TP2, especially
#               in a choppy market)
# "half_tp1"  — SL = entry + (TP1 - entry) / 2 (tighter than breakeven —
#               already in profit; triggers more often on noise, but every
#               trigger locks in a small guaranteed profit instead of zero)
TP1_SL_MODE_FILE = os.path.join(DATA_DIR, "tp1_sl_mode.json")

def load_tp1_sl_mode() -> str:
    data = safe_json_load(TP1_SL_MODE_FILE, {"tp1_sl_mode": "breakeven"})
    mode = data.get("tp1_sl_mode", "breakeven")
    return mode if mode in ("breakeven", "half_tp1") else "breakeven"

def save_tp1_sl_mode(mode: str):
    if mode not in ("breakeven", "half_tp1"):
        raise ValueError(f"Unknown TP1_SL_MODE: {mode!r}")
    safe_json_save(TP1_SL_MODE_FILE, {"tp1_sl_mode": mode})

TP1_SL_MODE = load_tp1_sl_mode()

# =====================================================================
# 🔕  DISCORD NOTIFICATIONS TOGGLE
# =====================================================================
# When off, the bot skips sending signal/TP1 messages to the Discord channel —
# the gateway connection itself stays up, so slash-commands like !status still
# work. Scanner, web dashboard, WebSocket push, and Android push notifications
# are completely unaffected — this only gates the app.bot.py channel.send() calls.
DISCORD_NOTIFICATIONS_FILE = os.path.join(DATA_DIR, "discord_notifications.json")

def load_discord_notifications_enabled() -> bool:
    data = safe_json_load(DISCORD_NOTIFICATIONS_FILE, {"enabled": True})
    return bool(data.get("enabled", True))

def save_discord_notifications_enabled(enabled: bool):
    safe_json_save(DISCORD_NOTIFICATIONS_FILE, {"enabled": bool(enabled)})

DISCORD_NOTIFICATIONS_ENABLED = load_discord_notifications_enabled()

# =====================================================================
# ⏱️  SCAN INTERVAL
# =====================================================================
# Signals only ever form on a closed bar (anti-repainting, idx=-2), so on a 1h
# timeframe a new signal can't appear more than once an hour no matter how
# often we poll. Faster polling still helps with two things: catching TP1/SL
# hits sooner (checked against the forming bar's live high/low) and fresher
# numbers in the web dashboard's topbar/pulse. 15s floor keeps a full scan
# cycle (network fetch + indicators + occasional chart render) comfortably
# inside the interval even for several pairs — benchmarked indicator compute
# time alone is ~50ms/pair-timeframe, network I/O dominates.
SCAN_INTERVAL_OPTIONS = (15, 30, 60, 180)  # seconds
SCAN_INTERVAL_FILE = os.path.join(DATA_DIR, "scan_interval.json")

def load_scan_interval() -> int:
    data = safe_json_load(SCAN_INTERVAL_FILE, {"seconds": 60})
    seconds = data.get("seconds", 60)
    return seconds if seconds in SCAN_INTERVAL_OPTIONS else 60

def save_scan_interval(seconds: int):
    if seconds not in SCAN_INTERVAL_OPTIONS:
        raise ValueError(f"scan interval must be one of {SCAN_INTERVAL_OPTIONS}, got {seconds}")
    safe_json_save(SCAN_INTERVAL_FILE, {"seconds": seconds})

SCAN_INTERVAL_SECONDS = load_scan_interval()

# =====================================================================
# ⛓️  ON-CHAIN BIAS CACHE (persisted so a restart doesn't lose it / delay it)
# =====================================================================
# The bias itself already has its own TTL logic inside onchain.py (hourly), but
# it previously lived only in a bot.py module-level variable — reset to None on
# every restart, so the web dashboard (and Discord embeds) had nothing to show
# until the next hourly refresh completed. Persisting it means a restart just
# picks up wherever it left off.
ONCHAIN_BIAS_CACHE_FILE = os.path.join(DATA_DIR, "onchain_bias_cache.json")

def load_onchain_bias_cache() -> dict:
    """Returns {"bias": dict|None, "last_fetch": float}."""
    return safe_json_load(ONCHAIN_BIAS_CACHE_FILE, {"bias": None, "last_fetch": 0.0})

def save_onchain_bias_cache(bias: Optional[dict], last_fetch: float):
    safe_json_save(ONCHAIN_BIAS_CACHE_FILE, {"bias": bias, "last_fetch": last_fetch})

# =====================================================================
# 🎨  CHART COLORS (web UI + !chart) — configurable from Settings
# =====================================================================
COLORS_FILE = os.path.join(DATA_DIR, "chart_colors.json")
_COLOR_DEFAULTS = {
    "frama": "#e8a33d",
    "bb": "#7c8797",
    "support": "#45d0a5",
    "resistance": "#f2637a",
    "poc": "#e6c619",
    "mfi_line": "#8b93ff",
    "mfi_overbought": "#f2637a",
    "mfi_oversold": "#45d0a5",
    "candle_up": "#45d0a5",
    "candle_down": "#f2637a",
    "tp_line": "#45d0a5",
    "sl_line": "#f2637a",
    "signal_long": "#45d0a5",
    "signal_short": "#f2637a",
}

def load_colors() -> dict:
    data = safe_json_load(COLORS_FILE, _COLOR_DEFAULTS)
    return {**_COLOR_DEFAULTS, **data}

def save_colors(data: dict):
    safe_json_save(COLORS_FILE, data)

CHART_COLORS: dict = load_colors()

# =====================================================================
# 📱  REGISTERED DEVICES (Android push, FCM tokens)
# =====================================================================
DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")

def load_devices() -> dict:
    """{fcm_token: {"device_name": str, "registered_at": iso str}}"""
    return safe_json_load(DEVICES_FILE, {})

def save_devices(data: dict):
    safe_json_save(DEVICES_FILE, data)

# =====================================================================
# 🎯  ADAPTIVE TP
# =====================================================================
SIGNAL_HISTORY_LIMIT = 25
TP_PERCENTILE = 0.75
SAFE_TP_PERCENTILE = 0.50
USE_SAFE_TP = False
MIN_TP_PCT = 0.3
MAX_TP_PCT = 8.0
MAX_HOLD_BARS = 20

# =====================================================================
# 🛑  ADAPTIVE SL (based on historical MAE)
# =====================================================================
SL_ADAPTIVE_ENABLED   = True    # Enable adaptive SL
SL_MAE_PERCENTILE     = 0.85    # MAE percentile (85% — covers most retraces)
SL_MAE_BUFFER         = 0.002   # Extra margin added on top of the percentile (0.2%)
SL_MIN_HISTORY        = 10      # Minimum winning trades to activate adaptive SL
SL_FALLBACK_PCT       = 0.015   # Fallback SL when there's not enough history (1.5% of price)
# 🆕 FIX: TP has an ATR cap (calculate_adaptive_tp), the adaptive SL didn't —
# an unbounded MAE percentile could give an unreasonably wide stop on outliers in the history.
SL_MAX_ATR_MULT        = 4.0    # Adaptive SL can't be farther than 4×ATR from entry
# 🆕 FIX (Kimi review #10): this used to be hardcoded as entry*0.999/entry*1.001
# directly in calculate_adaptive_sl — this is the MINIMUM SL distance from
# entry (a floor, not a cap): if the adaptive MAE said to put SL at 0.03%, it
# still got pushed out to 0.1%. That can be suboptimal on low-volatility
# altcoins — moved into config so it can be tuned without editing code.
SL_MIN_DISTANCE_PCT    = 0.001   # Minimum SL distance from entry (0.1%)

# 🆕 TP FEEDBACK LOOP — auto-adjustment based on the real hit rate
TP_HIT_RATE_TARGET = 0.35      # Target TP hit rate (35%)
TP_AUTO_ADJUST = True          # Enable percentile auto-adjustment
TP_CAPTURE_RATE = 0.70         # Realistic MFE capture rate (0.5-0.8)
TP_ADJUST_MIN_PCT = 0.30       # Minimum percentile after adjustment
TP_ADJUST_MAX_PCT = 0.85       # Maximum percentile after adjustment

# TP config persistence
TP_CONFIG_FILE = os.path.join(DATA_DIR, "tp_config.json")

def load_tp_config():
    """Loads the TP config from file."""
    global TP_PERCENTILE, SAFE_TP_PERCENTILE, USE_SAFE_TP, SIGNAL_HISTORY_LIMIT
    global TP_HIT_RATE_TARGET, TP_AUTO_ADJUST, TP_CAPTURE_RATE
    global TP_ADJUST_MIN_PCT, TP_ADJUST_MAX_PCT
    data = safe_json_load(TP_CONFIG_FILE, {})
    if not data:
        return
    TP_PERCENTILE        = data.get("tp_percentile",        TP_PERCENTILE)
    SAFE_TP_PERCENTILE   = data.get("safe_tp_percentile",   SAFE_TP_PERCENTILE)
    USE_SAFE_TP          = data.get("use_safe_tp",          USE_SAFE_TP)
    SIGNAL_HISTORY_LIMIT = data.get("signal_history_limit", SIGNAL_HISTORY_LIMIT)
    TP_HIT_RATE_TARGET   = data.get("tp_hit_rate_target",   TP_HIT_RATE_TARGET)
    TP_AUTO_ADJUST       = data.get("tp_auto_adjust",       TP_AUTO_ADJUST)
    TP_CAPTURE_RATE      = data.get("tp_capture_rate",      TP_CAPTURE_RATE)
    TP_ADJUST_MIN_PCT    = data.get("tp_adjust_min_pct",    TP_ADJUST_MIN_PCT)
    TP_ADJUST_MAX_PCT    = data.get("tp_adjust_max_pct",    TP_ADJUST_MAX_PCT)

def save_tp_config():
    """Saves the current TP config to file."""
    safe_json_save(TP_CONFIG_FILE, {
        "tp_percentile":        TP_PERCENTILE,
        "safe_tp_percentile":   SAFE_TP_PERCENTILE,
        "use_safe_tp":          USE_SAFE_TP,
        "signal_history_limit": SIGNAL_HISTORY_LIMIT,
        "tp_hit_rate_target":   TP_HIT_RATE_TARGET,
        "tp_auto_adjust":       TP_AUTO_ADJUST,
        "tp_capture_rate":      TP_CAPTURE_RATE,
        "tp_adjust_min_pct":    TP_ADJUST_MIN_PCT,
        "tp_adjust_max_pct":    TP_ADJUST_MAX_PCT,
    })

# Load on startup
load_tp_config()

# =====================================================================
# 🕯️  HEIKIN ASHI FOR UT BOT
# =====================================================================
UT_HA_FILE = os.path.join(DATA_DIR, "ut_ha.json")

def load_ut_ha() -> bool:
    data = safe_json_load(UT_HA_FILE, {"ut_heikin_ashi": False})
    return data.get("ut_heikin_ashi", False)

def save_ut_ha(enabled: bool):
    safe_json_save(UT_HA_FILE, {"ut_heikin_ashi": enabled})

UT_HEIKIN_ASHI = load_ut_ha()

# =====================================================================
# 🧬  HTF BIAS
# =====================================================================
HTF_FILE = os.path.join(DATA_DIR, "htf_bias.json")
HTF_CACHE_TTL_SECONDS = 300

def load_htf() -> str:
    data = safe_json_load(HTF_FILE, {"htf": "1d"})
    return data.get("htf", "1d")

def save_htf(htf: str):
    safe_json_save(HTF_FILE, {"htf": htf})

HTF_BIAS = load_htf()

# =====================================================================
# 📡  TRADING MODE
# =====================================================================
MODE_FILE = os.path.join(DATA_DIR, "mode.json")

def load_mode() -> str:
    data = safe_json_load(MODE_FILE, {"mode": "futures"})
    return data.get("mode", "futures")

def save_mode(mode: str):
    safe_json_save(MODE_FILE, {"mode": mode})

MARKET_MODE = load_mode()

# =====================================================================
# 📊  HISTORY FILES
# =====================================================================
SIGNALS_HISTORY_FILE = os.path.join(DATA_DIR, "signals_history.json")

# 🆕 Snapshot of bot.state (active positions a_active_trade/u_active_trade,
# etc.) — separate from signals_history.json. Signal history already survived
# restarts before; but the fact that "this position is still open, here's its
# TP/SL/tp1_hit" only lived in the process's memory and was lost on every
# container restart. See state.py.
BOT_STATE_FILE = os.path.join(DATA_DIR, "bot_state_snapshot.json")
