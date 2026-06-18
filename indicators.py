import numpy as np
import pandas as pd
from typing import Tuple

# =====================================================================
# 📊  БАЗОВЫЕ ИНДИКАТОРЫ
# =====================================================================

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder's smoothing)."""
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()

def calculate_chop(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Choppiness Index."""
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr_sum = tr.rolling(window=length).sum()
    hh = df["high"].rolling(window=length).max()
    ll = df["low"].rolling(window=length).min()
    return 100 * np.log10(atr_sum / (hh - ll + 1e-12)) / np.log10(length)

def calculate_frama(df: pd.DataFrame, length: int = 22, mult: float = 2.1) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Fractal Adaptive Moving Average."""
    n = int(length / 2)
    
    hh1 = df["high"].rolling(window=n).max()
    ll1 = df["low"].rolling(window=n).min()
    n1 = (hh1 - ll1) / n
    
    hh2 = df["high"].shift(n).rolling(window=n).max()
    ll2 = df["low"].shift(n).rolling(window=n).min()
    n2 = (hh2 - ll2) / n
    
    hh3 = df["high"].rolling(window=length).max()
    ll3 = df["low"].rolling(window=length).min()
    n3 = (hh3 - ll3) / length
    
    with np.errstate(divide="ignore", invalid="ignore"):
        dimen = np.where(
            (n1 > 0) & (n2 > 0) & (n3 > 0),
            (np.log(n1 + n2 + 1e-8) - np.log(n3 + 1e-8)) / np.log(2.0),
            0.0,
        )
    
    alpha = np.clip(np.exp(-4.6 * (dimen - 1.0)), 0.01, 1.0)
    
    close = df["close"].values
    frama_ma = np.zeros(len(df))
    frama_ma[0] = close[0]
    for i in range(1, len(df)):
        frama_ma[i] = alpha[i] * close[i] + (1.0 - alpha[i]) * frama_ma[i - 1]
    
    fs = pd.Series(frama_ma, index=df.index)
    
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    fatr = tr.rolling(window=length).mean()
    
    fu = fs + fatr * mult
    fl = fs - fatr * mult
    
    fdir = np.zeros(len(df))
    for i in range(1, len(df)):
        if close[i] > fu.iloc[i]:
            fdir[i] = 1
        elif close[i] < fl.iloc[i]:
            fdir[i] = -1
        else:
            fdir[i] = fdir[i - 1]
    
    return fs, fu, fl, pd.Series(fdir, index=df.index)

def calculate_mfi(df: pd.DataFrame, length: int = 8) -> pd.Series:
    """Money Flow Index."""
    hlc3 = (df["high"] + df["low"] + df["close"]) / 3.0
    mf = hlc3 * df["volume"]
    pos = np.where(hlc3 > hlc3.shift(1), mf, 0.0)
    neg = np.where(hlc3 < hlc3.shift(1), mf, 0.0)
    pos_s = pd.Series(pos, index=df.index).rolling(window=length).sum()
    neg_s = pd.Series(neg, index=df.index).rolling(window=length).sum()
    ratio = pos_s / (neg_s + 1e-8)
    return 100.0 - (100.0 / (1.0 + ratio))

def calculate_andean(df: pd.DataFrame, length: int = 23, sig_len: int = 6) -> Tuple[pd.Series, pd.Series]:
    """Andean Oscillator."""
    alpha = 2.0 / (length + 1)
    c = df["close"].values
    o = df["open"].values
    
    u1 = np.zeros(len(df))
    u2 = np.zeros(len(df))
    l1 = np.zeros(len(df))
    l2 = np.zeros(len(df))
    
    u1[0] = c[0]
    u2[0] = c[0]**2
    l1[0] = c[0]
    l2[0] = c[0]**2
    
    for i in range(1, len(df)):
        u1[i] = max(c[i], o[i], u1[i-1] - (u1[i-1] - c[i]) * alpha)
        u2[i] = max(c[i]**2, o[i]**2, u2[i-1] - (u2[i-1] - c[i]**2) * alpha)
        l1[i] = min(c[i], o[i], l1[i-1] + (c[i] - l1[i-1]) * alpha)
        l2[i] = min(c[i]**2, o[i]**2, l2[i-1] + (c[i]**2 - l2[i-1]) * alpha)
    
    osc = pd.Series(
        np.sqrt(np.maximum(0, l2 - l1**2)) - np.sqrt(np.maximum(0, u2 - u1**2)),
        index=df.index,
    )
    return osc, osc.ewm(span=sig_len, adjust=False).mean()

# =====================================================================
# 🕯️  HEIKIN ASHI
# =====================================================================

def heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Преобразует обычные свечи в Heikin-Ashi."""
    ha = df.copy()
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    ha_open = pd.Series(index=df.index, dtype=float)
    ha_open.iloc[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i-1] + ha_close.iloc[i-1]) / 2.0
    ha["open"] = ha_open
    ha["high"] = pd.concat([df["high"], ha_open, ha_close], axis=1).max(axis=1)
    ha["low"] = pd.concat([df["low"], ha_open, ha_close], axis=1).min(axis=1)
    ha["close"] = ha_close
    return ha

# =====================================================================
# 🤖  UT BOT (ИСПРАВЛЕННЫЙ)
# =====================================================================

def calculate_ut_bot(
    df: pd.DataFrame,
    sensitivity: float = 1.0,
    period: int = 10,
    use_ha: bool = False
) -> Tuple[pd.Series, pd.Series]:
    """
    UT Bot by Kivanc Ozbilgic.
    
    ВАЖНО: ATR всегда рассчитывается от обычных свечей (реальная волатильность),
    даже когда включен режим Heikin-Ashi для сигналов.
    """
    # ✅ ATR всегда от обычных свечей
    atr_normal = calculate_atr(df, period)
    n_loss = (sensitivity * atr_normal).values
    
    # Для сигналов используем HA или обычные свечи
    if use_ha:
        df_ut = heikin_ashi(df)
        src = df_ut["close"].values
    else:
        df_ut = df.copy()
        src = df_ut["close"].values
    
    # Расчет трейлинг-стопа
    ts = np.zeros(len(df_ut))
    ts[0] = src[0] - n_loss[0]
    
    for i in range(1, len(df_ut)):
        prev = ts[i-1]
        if src[i] > prev and src[i-1] > prev:
            ts[i] = max(prev, src[i] - n_loss[i])
        elif src[i] < prev and src[i-1] < prev:
            ts[i] = min(prev, src[i] + n_loss[i])
        else:
            ts[i] = src[i] - n_loss[i] if src[i] > prev else src[i] + n_loss[i]
    
    ts_s = pd.Series(ts, index=df.index)
    src_s = df_ut["close"]
    
    ut_buy = (src_s > ts_s) & (src_s.shift(1) <= ts_s.shift(1))
    ut_sell = (src_s < ts_s) & (src_s.shift(1) >= ts_s.shift(1))
    
    return ut_buy, ut_sell

# =====================================================================
# 🔢  K-MEANS MFI
# =====================================================================

def run_kmeans_mfi(mfi: pd.Series, training_size: int = 800) -> Tuple[float, float]:
    """Обучение K-Means для MFI уровней oversold/overbought."""
    vals = mfi.dropna().tail(training_size).values
    
    # Защита от NaN
    if len(vals) < 2 or np.isnan(vals).any():
        return 20.0, 80.0
    
    c1, c2 = float(vals.min()), float(vals.max())
    
    for _ in range(10):
        d1 = np.abs(vals - c1)
        d2 = np.abs(vals - c2)
        
        # Защита от пустых кластеров
        cl1 = vals[d1 <= d2]
        cl2 = vals[d2 < d1]
        
        if len(cl1) == 0 and len(cl2) == 0:
            break
        
        if len(cl1) == 0:
            c1 = float(np.nanmean(vals))
        else:
            c1 = float(np.nanmean(cl1))
        
        if len(cl2) == 0:
            c2 = float(np.nanmean(vals))
        else:
            c2 = float(np.nanmean(cl2))
    
    return min(c1, c2), max(c1, c2)