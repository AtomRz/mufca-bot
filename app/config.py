import os
import json
import threading
import logging
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# =====================================================================
# ⚙️  БАЗОВЫЕ НАСТРОЙКИ
# =====================================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
# Discord is entirely optional: with no token, or DISCORD_ENABLED=false, the
# bot runs scanner + web dashboard only — no gateway connection is attempted,
# no channel messages, no !commands. See bot.py's ensure_engine_started() /
# main.py for how startup no longer depends on Discord's on_ready firing.
DISCORD_ENABLED = bool(DISCORD_TOKEN) and os.getenv("DISCORD_ENABLED", "true").strip().lower() != "false"

# 🆕 Basic Auth для веб-морды. Если оба не заданы — дашборд остаётся открытым
# (для локальной разработки), но при старте пишется громкий warning в лог.
WEB_USERNAME = os.getenv("WEB_USERNAME", "")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "general")

# 🆕 Push-уведомления на Android через Firebase Cloud Messaging.
# Файл сервисного аккаунта НЕ коммитится в репо (он публичный!) — кладётся
# вручную на сервер, в тот же персистентный volume, что signals_history.json.
FIREBASE_CREDENTIALS_PATH = os.getenv("FIREBASE_CREDENTIALS_PATH", "/app/data/firebase-credentials.json")

DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)

# =====================================================================
# 🔑  ВНЕШНИЕ API КЛЮЧИ
# =====================================================================
ETHERSCAN_API_KEY  = os.getenv("ETHERSCAN_API_KEY", "")
COINGECKO_API_KEY  = os.getenv("COINGECKO_API_KEY", "")   # Demo key (бесплатный)

# =====================================================================
# 📡  ON-CHAIN НАСТРОЙКИ
# =====================================================================
ONCHAIN_FLOW_THRESHOLD_ETH    = 5_000   # ETH — порог "normal" сигнала
ONCHAIN_FLOW_THRESHOLD_LARGE_ETH = 20_000  # ETH — порог "large" сигнала
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
            return default  # нормальный сценарий — первый запуск, файла ещё нет
        except Exception as e:
            # 🆕 FIX: раньше ЛЮБАЯ ошибка (битый JSON, нет прав на чтение, диск
            # недоступен) молча проглатывалась и возвращала default — то же самое
            # поведение, что и для честного "файла ещё нет". Разница в том, что
            # первое — это реальная потеря/недоступность данных, о которой стоит
            # знать, а не тихо продолжать с пустым конфигом.
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

# 🆕 Интервал обновления on-chain данных (Etherscan/CoinGecko), настраивается
# из веб-морды по тому же паттерну, что SCAN_INTERVAL_SECONDS — живая смена
# без рестарта контейнера. Раньше было захардкожено 3600с в двух местах
# (onchain.py _cache_get + bot.py market_scanner), теперь один источник правды.
# Живёт здесь (не в блоке ON-CHAIN НАСТРОЙКИ выше), потому что зависит от
# safe_json_load/safe_json_save, определённых чуть выше.
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

ONCHAIN_CACHE_TTL = load_onchain_interval()   # секунд — обновление балансов (default 1h)

# =====================================================================
# 📊  ПАРЫ ДЛЯ СКАНИРОВАНИЯ
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
# ⏱️  НАСТРОЙКИ ТАЙМФРЕЙМОВ
# =====================================================================
TIMEFRAMES = ["1h", "4h"]

# =====================================================================
# 🔧  FILTER TOGGLES (зеркало Pine Script input.bool)
# 🆕 Раньше это были просто константы True, зашитые в код — их нельзя было
# выключить из UI. Теперь, как HTF_BIAS/UT_HEIKIN_ASHI, персистятся в JSON и
# правятся через _cfg.ENABLE_X (НЕ bare import — иначе после runtime-смены
# через /api/config/filters старые импортированные копии в signals.py и
# chart_data.py останутся замороженными на исходном значении).
# =====================================================================
FILTERS_FILE = os.path.join(DATA_DIR, "filter_toggles.json")

DEFAULT_FILTER_TOGGLES = {
    "frama": True,       # use_frama_filter
    "chop": True,        # use_chop
    "atr": True,         # use_atr_f
    "htf": True,         # enable_mtf_bias
    "fake_break": True,  # 🆕 раньше применялся безусловно, без input.bool-аналога
    "liq_sweep": True,   # 🆕 раньше применялся безусловно, без input.bool-аналога
}

def load_filter_toggles() -> dict:
    data = safe_json_load(FILTERS_FILE, DEFAULT_FILTER_TOGGLES)
    # на случай если в файле не все ключи (например после добавления нового фильтра)
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

# =====================================================================
# 📈  ПАРАМЕТРЫ ИНДИКАТОРОВ
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
    # на случай если файл со старой версией — подмешиваем недостающие ключи дефолтами
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

COOLDOWN_BARS = 2
MAX_ALLOWED_LEV = 10
TARGET_RISK_DEP = 5.0

# =====================================================================
# 🎯  РЕЖИМ ПЕРЕНОСА SL ПОСЛЕ TP1
# =====================================================================
# "breakeven" — SL = entry (нулевой риск по остатку, но чаще ловит финальный
#               откат-выбивание перед продолжением к TP2, особенно на chop-рынке)
# "half_tp1"  — SL = entry + (TP1 - entry) / 2 (жёстче безубытка — уже в плюсе,
#               чаще срабатывает на шуме, но каждое срабатывание фиксирует
#               небольшой гарантированный профит вместо нуля)
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
# 🎨  ЦВЕТА ГРАФИКА (веб-морда + !chart) — настраиваются из Settings
# =====================================================================
COLORS_FILE = os.path.join(DATA_DIR, "chart_colors.json")
_COLOR_DEFAULTS = {
    "frama": "#e8a33d",
    "bb": "#7c8797",
    "support": "#45d0a5",
    "resistance": "#f2637a",
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
# 📱  ЗАРЕГИСТРИРОВАННЫЕ УСТРОЙСТВА (Android push, FCM токены)
# =====================================================================
DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")

def load_devices() -> dict:
    """{fcm_token: {"device_name": str, "registered_at": iso str}}"""
    return safe_json_load(DEVICES_FILE, {})

def save_devices(data: dict):
    safe_json_save(DEVICES_FILE, data)

# =====================================================================
# 🎯  АДАПТИВНЫЙ ТП
# =====================================================================
SIGNAL_HISTORY_LIMIT = 25
TP_PERCENTILE = 0.75
SAFE_TP_PERCENTILE = 0.50
USE_SAFE_TP = False
MIN_TP_PCT = 0.3
MAX_TP_PCT = 8.0
MAX_HOLD_BARS = 20

# =====================================================================
# 🛑  АДАПТИВНЫЙ SL (на основе исторического MAE)
# =====================================================================
SL_ADAPTIVE_ENABLED   = True    # Включить адаптивный SL
SL_MAE_PERCENTILE     = 0.85    # Перцентиль MAE (85% — покрываем большинство откатов)
SL_MAE_BUFFER         = 0.002   # Дополнительный отступ за перцентиль (0.2%)
SL_MIN_HISTORY        = 10      # Минимум выигрышных сделок для активации адаптивного SL
SL_FALLBACK_PCT       = 0.015   # Fallback SL когда истории недостаточно (1.5% от цены)
# 🆕 FIX: у TP есть ATR-кап (calculate_adaptive_tp), у адаптивного SL его не было —
# перцентиль MAE без ограничения мог дать неадекватно широкий стоп при выбросах в истории.
SL_MAX_ATR_MULT        = 4.0    # Адаптивный SL не может быть дальше 4×ATR от входа

# 🆕 TP FEEDBACK LOOP — автоподстройка на основе реального hit rate
TP_HIT_RATE_TARGET = 0.35      # Целевой процент достижения TP (35%)
TP_AUTO_ADJUST = True          # Включить автоподстройку перцентиля
TP_CAPTURE_RATE = 0.70         # Realistic MFE capture rate (0.5-0.8)
TP_ADJUST_MIN_PCT = 0.30       # Минимальный перцентиль после корректировки
TP_ADJUST_MAX_PCT = 0.85       # Максимальный перцентиль после корректировки

# Персистенс TP конфига
TP_CONFIG_FILE = os.path.join(DATA_DIR, "tp_config.json")

def load_tp_config():
    """Загружает TP конфиг из файла."""
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
    """Сохраняет текущий TP конфиг в файл."""
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

# Загружаем при старте
load_tp_config()

# =====================================================================
# 🕯️  HEIKIN ASHI ДЛЯ UT BOT
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
# 📡  РЕЖИМ ТОРГОВЛИ
# =====================================================================
MODE_FILE = os.path.join(DATA_DIR, "mode.json")

def load_mode() -> str:
    data = safe_json_load(MODE_FILE, {"mode": "futures"})
    return data.get("mode", "futures")

def save_mode(mode: str):
    safe_json_save(MODE_FILE, {"mode": mode})

MARKET_MODE = load_mode()

# =====================================================================
# 📊  ФАЙЛЫ ИСТОРИИ
# =====================================================================
SIGNALS_HISTORY_FILE = os.path.join(DATA_DIR, "signals_history.json")

# 🆕 Снапшот bot.state (активные позиции a_active_trade/u_active_trade и т.п.) —
# отдельно от signals_history.json. История сигналов и раньше переживала рестарт,
# а вот сам факт "эта позиция ещё открыта, вот её TP/SL/tp1_hit" жил только в
# памяти процесса и терялся при каждом перезапуске контейнера. См. state.py.
BOT_STATE_FILE = os.path.join(DATA_DIR, "bot_state_snapshot.json")
