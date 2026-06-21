import os
import json
import threading
import logging
from typing import List
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# =====================================================================
# ⚙️  БАЗОВЫЕ НАСТРОЙКИ
# =====================================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "general")
DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)

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
        except Exception:
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
# =====================================================================
ENABLE_FRAMA_FILTER = True   # use_frama_filter
ENABLE_CHOP_FILTER  = True   # use_chop
ENABLE_ATR_FILTER   = True   # use_atr_f
ENABLE_MTF_BIAS     = True   # enable_mtf_bias

# =====================================================================
# 📈  ПАРАМЕТРЫ ИНДИКАТОРОВ
# =====================================================================
ATR_PERIOD = 14
ATR_MIN = 0.3
ATR_MAX = 4.5
CHOP_LENGTH = 14
CHOP_THRESHOLD = {"1h": 55.0, "4h": 61.8}
FRAMA_LEN = 22
FRAMA_MULT = 2.1
MFI_LEN = 8
MFI_TRAINING = 800
AND_LEN = 23
AND_SIG_LEN = 6
LOOKBACK = 3
COOLDOWN_BARS = 2
UT_SENSITIVITY = 1.0
UT_PERIOD = 10
MAX_ALLOWED_LEV = 10
TARGET_RISK_DEP = 5.0

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