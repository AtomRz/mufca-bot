"""
MUFCA v3.1 — Chart Module
Генерация свечных графиков с индикаторами для Discord.
Команда: !chart [PAIR] [TIMEFRAME] [LIMIT]
"""

import io
import logging
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.gridspec as gridspec
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
from typing import Optional, Tuple, List, Dict

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# 🎨  ТЕМА
# ─────────────────────────────────────────────────────────────────────
THEME = {
    "bg":         "#0d1117",
    "bg2":        "#161b22",
    "grid":       "#21262d",
    "text":       "#c9d1d9",
    "text_dim":   "#6e7681",
    "bull":       "#26a641",
    "bear":       "#f85149",
    "bull_body":  "#1a7f37",
    "bear_body":  "#b91c1c",
    "volume":     "#388bfd",
    "frama":      "#f0883e",
    "bb_mid":     "#a5d6ff",
    "bb_band":    "#388bfd",
    "bb_fill":    "#388bfd",
    "support":    "#00bcd4",
    "resist":     "#9e9e9e",
    "pivot":      "#d29922",
    "entry":      "#f0883e",
    "tp":         "#26a641",
    "sl":         "#f85149",
    "signal_long":  "#26a641",
    "signal_short": "#f85149",
    "mfi_line":   "#a371f7",
    "mfi_ob":     "#f85149",
    "mfi_os":     "#26a641",
}

# ─────────────────────────────────────────────────────────────────────
# 📐  ИНДИКАТОРЫ ДЛЯ ГРАФИКА
# ─────────────────────────────────────────────────────────────────────

def calc_bollinger_bands(
    close: pd.Series,
    period: int = 20,
    std_mult: float = 2.0
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands: upper, mid, lower."""
    mid = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return upper, mid, lower


def calc_support_resistance(
    df: pd.DataFrame,
    pivot_window: int = 10,
    round_factor: Optional[float] = None,
    max_levels: int = 4
) -> Dict[str, List[float]]:
    """
    Уровни поддержки/сопротивления:
    - Pivot Points (локальные min/max)
    - Round Numbers (круглые уровни)

    Returns dict: {"support": [...], "resistance": [...], "pivot": [...]}
    """
    close = df["close"]
    high = df["high"]
    low = df["low"]
    last_close = float(close.iloc[-2])  # подтверждённый бар

    supports: List[float] = []
    resistances: List[float] = []
    pivots: List[float] = []

    # 1. Pivot Points (локальные экстремумы)
    w = pivot_window
    for i in range(w, len(df) - w):
        hi_window = high.iloc[i - w:i + w + 1]
        lo_window = low.iloc[i - w:i + w + 1]
        if high.iloc[i] == hi_window.max():
            resistances.append(float(high.iloc[i]))
        if low.iloc[i] == lo_window.min():
            supports.append(float(low.iloc[i]))

    # 2. Round Numbers
    if round_factor is None:
        # Авто-шаг в зависимости от цены
        if last_close > 10000:
            round_factor = 1000.0
        elif last_close > 1000:
            round_factor = 100.0
        elif last_close > 100:
            round_factor = 10.0
        else:
            round_factor = 1.0

    price_range_low  = df["low"].min()
    price_range_high = df["high"].max()
    rn = price_range_low - (price_range_low % round_factor)
    while rn <= price_range_high:
        if rn > 0:
            pivots.append(float(rn))
        rn += round_factor

    # Фильтруем — оставляем только вблизи текущей цены
    def near_price(levels, price, pct=0.05):
        return [l for l in levels if abs(l - price) / price < pct]

    supports    = _cluster_levels(near_price(supports,    last_close, 0.12), max_levels)
    resistances = _cluster_levels(near_price(resistances, last_close, 0.12), max_levels)
    pivots      = _cluster_levels(near_price(pivots,      last_close, 0.12), max_levels)

    return {
        "support":    [l for l in supports    if l < last_close],
        "resistance": [l for l in resistances if l > last_close],
        "pivot":      pivots,
    }


def _cluster_levels(levels: List[float], max_n: int, tol: float = 0.005) -> List[float]:
    """Кластеризует близкие уровни, оставляет до max_n."""
    if not levels:
        return []
    levels = sorted(set(levels))
    clustered: List[float] = []
    used = [False] * len(levels)
    for i, l in enumerate(levels):
        if used[i]:
            continue
        cluster = [l]
        for j in range(i + 1, len(levels)):
            if not used[j] and abs(levels[j] - l) / (l + 1e-8) < tol:
                cluster.append(levels[j])
                used[j] = True
        clustered.append(float(np.mean(cluster)))
    # Возвращаем max_n ближайших к середине
    if len(clustered) <= max_n:
        return clustered
    mid = np.median(clustered)
    clustered.sort(key=lambda x: abs(x - mid))
    return sorted(clustered[:max_n])


# ─────────────────────────────────────────────────────────────────────
# 🏗️  ПОСТРОЕНИЕ ГРАФИКА
# ─────────────────────────────────────────────────────────────────────

def build_chart(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    frama: Optional[pd.Series] = None,
    frama_upper: Optional[pd.Series] = None,
    frama_lower: Optional[pd.Series] = None,
    mfi: Optional[pd.Series] = None,
    mfi_ob: float = 80.0,
    mfi_os: float = 20.0,
    entry_price: Optional[float] = None,
    tp_price: Optional[float] = None,
    sl_price: Optional[float] = None,
    signal_side: Optional[str] = None,  # "long" | "short"
    signal_bar: Optional[int] = None,   # индекс бара сигнала
    limit: int = 50,
) -> io.BytesIO:
    """
    Строит полный свечной график и возвращает BytesIO PNG.

    Панели:
      [0] Свечи + FRAMA + BB + S/R + Entry/TP/SL + сигнальные стрелки
      [1] Объём
      [2] KMeans MFI (если передан)
    """
    df = df.tail(limit).copy().reset_index(drop=True)
    n = len(df)

    has_mfi = mfi is not None and len(mfi) >= limit

    # ── Layout ──────────────────────────────────────────────────────
    T = THEME
    fig = plt.figure(figsize=(14, 9 if has_mfi else 8), facecolor=T["bg"])

    if has_mfi:
        gs = gridspec.GridSpec(
            3, 1, height_ratios=[5, 1.2, 1.2],
            hspace=0.04, left=0.06, right=0.95, top=0.93, bottom=0.07
        )
    else:
        gs = gridspec.GridSpec(
            2, 1, height_ratios=[5, 1.2],
            hspace=0.04, left=0.06, right=0.95, top=0.93, bottom=0.07
        )

    ax_c = fig.add_subplot(gs[0])  # свечи
    ax_v = fig.add_subplot(gs[1], sharex=ax_c)  # объём
    ax_m = fig.add_subplot(gs[2], sharex=ax_c) if has_mfi else None

    for ax in ([ax_c, ax_v] + ([ax_m] if ax_m else [])):
        ax.set_facecolor(T["bg2"])
        ax.tick_params(colors=T["text_dim"], labelsize=8)
        ax.yaxis.tick_right()
        for spine in ax.spines.values():
            spine.set_edgecolor(T["grid"])

    # ── X labels ────────────────────────────────────────────────────
    x = np.arange(n)
    timestamps = pd.to_datetime(df["timestamp"], unit="ms")
    step = max(1, n // 8)
    tick_positions = x[::step]
    tick_labels = [timestamps.iloc[i].strftime("%d/%m %H:%M") for i in tick_positions]
    ax_c.set_xticks(tick_positions)
    ax_c.set_xticklabels([""] * len(tick_positions))  # скрываем на верхней панели

    # ── Bollinger Bands ─────────────────────────────────────────────
    bb_u, bb_m, bb_l = calc_bollinger_bands(df["close"])
    ax_c.fill_between(x, bb_l, bb_u, alpha=0.06, color=T["bb_fill"], zorder=1)
    ax_c.plot(x, bb_u, color=T["bb_band"], linewidth=0.8, alpha=0.7, zorder=2)
    ax_c.plot(x, bb_m, color=T["bb_mid"], linewidth=0.8, alpha=0.6, linestyle="--", zorder=2)
    ax_c.plot(x, bb_l, color=T["bb_band"], linewidth=0.8, alpha=0.7, zorder=2)

    # ── S/R уровни ──────────────────────────────────────────────────
    sr = calc_support_resistance(df)
    x_start = -0.5
    x_end   = n - 0.5

    for lvl in sr["support"]:
        ax_c.hlines(lvl, x_start, x_end, colors=T["support"],
                    linewidth=1.4, linestyles="--", alpha=0.85, zorder=7)
        ax_c.text(1, lvl, f"S {lvl:,.0f}", color=T["support"],
                  fontsize=8, va="bottom", ha="left", fontweight="bold",
                  bbox=dict(facecolor=T["bg2"], edgecolor="none", pad=1, alpha=0.7),
                  zorder=8)

    for lvl in sr["resistance"]:
        ax_c.hlines(lvl, x_start, x_end, colors=T["resist"],
                    linewidth=1.4, linestyles="--", alpha=0.85, zorder=7)
        ax_c.text(1, lvl, f"R {lvl:,.0f}", color=T["resist"],
                  fontsize=8, va="bottom", ha="left", fontweight="bold",
                  bbox=dict(facecolor=T["bg2"], edgecolor="none", pad=1, alpha=0.7),
                  zorder=8)

    for lvl in sr["pivot"]:
        ax_c.hlines(lvl, x_start, x_end, colors=T["pivot"],
                    linewidth=1.0, linestyles=":", alpha=0.7, zorder=7)
        ax_c.text(1, lvl, f"P {lvl:,.0f}", color=T["pivot"],
                  fontsize=7, va="bottom", ha="left",
                  bbox=dict(facecolor=T["bg2"], edgecolor="none", pad=1, alpha=0.7),
                  zorder=8)

    # ── FRAMA ───────────────────────────────────────────────────────
    if frama is not None and len(frama) >= limit:
        fs = frama.tail(limit).values
        ax_c.plot(x, fs, color=T["frama"], linewidth=1.4, zorder=4, label="FRAMA")
        if frama_upper is not None and frama_lower is not None:
            fu = frama_upper.tail(limit).values
            fl = frama_lower.tail(limit).values
            ax_c.fill_between(x, fl, fu, alpha=0.08, color=T["frama"], zorder=1)
            ax_c.plot(x, fu, color=T["frama"], linewidth=0.5, alpha=0.4, zorder=2)
            ax_c.plot(x, fl, color=T["frama"], linewidth=0.5, alpha=0.4, zorder=2)

    # ── Свечи ───────────────────────────────────────────────────────
    bar_w = 0.6
    for i in range(n):
        o = df["open"].iloc[i]
        h = df["high"].iloc[i]
        l = df["low"].iloc[i]
        c = df["close"].iloc[i]
        bull = c >= o
        color      = T["bull"]      if bull else T["bear"]
        body_color = T["bull_body"] if bull else T["bear_body"]

        # Фитиль
        ax_c.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=5)
        # Тело
        body_h = abs(c - o) if abs(c - o) > 0 else (h - l) * 0.01
        rect = Rectangle(
            (i - bar_w / 2, min(o, c)),
            bar_w, body_h,
            facecolor=body_color, edgecolor=color,
            linewidth=0.6, zorder=6
        )
        ax_c.add_patch(rect)

    # ── Сигнал (стрелка) ────────────────────────────────────────────
    if signal_bar is not None and signal_side is not None:
        # signal_bar — абсолютный индекс в оригинальном df до tail()
        # Переводим в индекс после tail()
        offset = len(df) - limit  # сколько срезали tail
        idx = signal_bar - offset if signal_bar is not None else n - 2
        idx = max(0, min(idx, n - 1))

        if signal_side == "long":
            y_arrow = df["low"].iloc[idx] * 0.999
            ax_c.annotate(
                "▲ LONG",
                xy=(idx, y_arrow),
                xytext=(idx, y_arrow * 0.996),
                color=T["signal_long"], fontsize=9, fontweight="bold",
                ha="center", va="top", zorder=9,
                arrowprops=dict(arrowstyle="->", color=T["signal_long"], lw=1.5)
            )
        else:
            y_arrow = df["high"].iloc[idx] * 1.001
            ax_c.annotate(
                "▼ SHORT",
                xy=(idx, y_arrow),
                xytext=(idx, y_arrow * 1.004),
                color=T["signal_short"], fontsize=9, fontweight="bold",
                ha="center", va="bottom", zorder=9,
                arrowprops=dict(arrowstyle="->", color=T["signal_short"], lw=1.5)
            )

    # ── Entry / TP / SL линии ────────────────────────────────────────
    if entry_price:
        ax_c.hlines(entry_price, x_start, x_end, colors=T["entry"],
                    linewidth=1.2, linestyles="-", zorder=8, alpha=0.9)
        ax_c.text(0, entry_price, f"ENTRY {entry_price:,.2f}",
                  color=T["entry"], fontsize=8, va="bottom", fontweight="bold")

    if tp_price:
        ax_c.hlines(tp_price, x_start, x_end, colors=T["tp"],
                    linewidth=1.0, linestyles="-.", zorder=8, alpha=0.9)
        ax_c.text(0, tp_price, f"TP {tp_price:,.2f}",
                  color=T["tp"], fontsize=8, va="bottom", fontweight="bold")

    if sl_price:
        ax_c.hlines(sl_price, x_start, x_end, colors=T["sl"],
                    linewidth=1.0, linestyles="-.", zorder=8, alpha=0.9)
        ax_c.text(0, sl_price, f"SL {sl_price:,.2f}",
                  color=T["sl"], fontsize=8, va="top", fontweight="bold")

    # ── TP/SL зона заливка ──────────────────────────────────────────
    if entry_price and tp_price and sl_price:
        ax_c.fill_between(x, entry_price, tp_price,
                          alpha=0.05, color=T["tp"], zorder=1)
        ax_c.fill_between(x, sl_price, entry_price,
                          alpha=0.05, color=T["sl"], zorder=1)

    # ── Сетка и оформление основной панели ──────────────────────────
    ax_c.grid(True, color=T["grid"], linewidth=0.5, alpha=0.6, zorder=0)
    ax_c.set_xlim(-0.5, n - 0.5)
    ax_c.yaxis.set_label_position("right")

    # Заголовок
    last_close = df["close"].iloc[-1]
    prev_close = df["close"].iloc[-2]
    chg_pct = (last_close - prev_close) / prev_close * 100
    chg_color = T["bull"] if chg_pct >= 0 else T["bear"]
    chg_sign  = "+" if chg_pct >= 0 else ""

    fig.text(
        0.06, 0.955,
        f"{symbol}  ·  {timeframe}",
        color=T["text"], fontsize=13, fontweight="bold", va="top"
    )
    fig.text(
        0.25, 0.955,
        f"${last_close:,.2f}  {chg_sign}{chg_pct:.2f}%",
        color=chg_color, fontsize=12, fontweight="bold", va="top"
    )

    # Легенда
    legend_elements = [
        Line2D([0], [0], color=T["frama"],   linewidth=1.4, label="FRAMA"),
        Line2D([0], [0], color=T["bb_mid"],  linewidth=0.8, linestyle="--", label="BB mid"),
        Line2D([0], [0], color=T["bb_band"], linewidth=0.8, label="BB bands"),
        Line2D([0], [0], color=T["support"], linewidth=0.8, linestyle="--", label="Support"),
        Line2D([0], [0], color=T["resist"],  linewidth=0.8, linestyle="--", label="Resist"),
        Line2D([0], [0], color=T["pivot"],   linewidth=0.6, linestyle=":",  label="Round lvl"),
    ]
    if entry_price:
        legend_elements.append(
            Line2D([0], [0], color=T["entry"], linewidth=1.2, label="Entry")
        )
    ax_c.legend(
        handles=legend_elements,
        loc="upper left", fontsize=7,
        facecolor=T["bg"], edgecolor=T["grid"],
        labelcolor=T["text_dim"], framealpha=0.8
    )

    # ── Панель объёма ────────────────────────────────────────────────
    vol_colors = [
        T["bull"] if df["close"].iloc[i] >= df["open"].iloc[i] else T["bear"]
        for i in range(n)
    ]
    ax_v.bar(x, df["volume"], color=vol_colors, width=0.7, alpha=0.7, zorder=3)
    ax_v.set_ylabel("Vol", color=T["text_dim"], fontsize=7, rotation=0, labelpad=20)
    ax_v.grid(True, color=T["grid"], linewidth=0.4, alpha=0.5, zorder=0)
    ax_v.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda val, _: f"{val/1e3:.0f}K" if val >= 1000 else f"{val:.0f}")
    )

    # ── Панель MFI ───────────────────────────────────────────────────
    if ax_m is not None and mfi is not None:
        mfi_vals = mfi.tail(limit).values
        ax_m.plot(x, mfi_vals, color=T["mfi_line"], linewidth=1.0, zorder=3)
        ax_m.axhline(mfi_ob, color=T["mfi_ob"], linewidth=0.6, linestyle="--", alpha=0.7)
        ax_m.axhline(mfi_os, color=T["mfi_os"], linewidth=0.6, linestyle="--", alpha=0.7)
        ax_m.fill_between(x, mfi_os, mfi_vals,
                          where=(mfi_vals <= mfi_os),
                          alpha=0.2, color=T["mfi_os"], zorder=1)
        ax_m.fill_between(x, mfi_ob, mfi_vals,
                          where=(mfi_vals >= mfi_ob),
                          alpha=0.2, color=T["mfi_ob"], zorder=1)
        ax_m.set_ylim(0, 100)
        ax_m.set_ylabel("MFI", color=T["text_dim"], fontsize=7, rotation=0, labelpad=20)
        ax_m.grid(True, color=T["grid"], linewidth=0.4, alpha=0.5, zorder=0)
        ax_m.set_xticks(tick_positions)
        ax_m.set_xticklabels(tick_labels, rotation=30, ha="right",
                              fontsize=7, color=T["text_dim"])
    else:
        ax_v.set_xticks(tick_positions)
        ax_v.set_xticklabels(tick_labels, rotation=30, ha="right",
                             fontsize=7, color=T["text_dim"])

    plt.setp(ax_c.get_xticklabels(), visible=False)
    plt.setp(ax_v.get_xticklabels(), visible=False if ax_m else True)

    # ── Сохранение ───────────────────────────────────────────────────
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=T["bg"], edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────────────────────────────
# 🤖  ПУБЛИЧНАЯ ФУНКЦИЯ ДЛЯ БОТА
# ─────────────────────────────────────────────────────────────────────

async def generate_chart(
    exchange,
    symbol: str,
    timeframe: str,
    limit: int = 50,
    state_snapshot: Optional[dict] = None,
) -> io.BytesIO:
    """
    Основная точка входа: скачивает OHLCV, считает индикаторы, строит PNG.

    Args:
        exchange:        ccxt.Exchange instance
        symbol:          "BTC/USDT"
        timeframe:       "1h" | "4h"
        limit:           количество свечей (default 50)
        state_snapshot:  dict с полями entry, tp, sl, side, signal_bar (опционально)

    Returns:
        BytesIO PNG buffer
    """
    import asyncio
    from utils import safe_fetch_ohlcv, parse_ohlcv, validate_dataframe
    from indicators import (
        calculate_frama, calculate_mfi, run_kmeans_mfi,
        calculate_atr
    )
    import config

    # Грузим больше данных для прогрева индикаторов
    fetch_limit = max(limit + 250, 300)
    bars = await safe_fetch_ohlcv(exchange, symbol, timeframe, limit=fetch_limit)
    df = parse_ohlcv(bars)

    if not validate_dataframe(df, min_rows=50):
        raise ValueError(f"Недостаточно данных для {symbol} {timeframe}")

    # ── Индикаторы ────────────────────────────────────────────────
    frama_s, frama_u, frama_l, _ = calculate_frama(
        df, length=config.FRAMA_LEN, mult=config.FRAMA_MULT
    )

    mfi_s = calculate_mfi(df, length=config.MFI_LEN)
    mfi_os, mfi_ob = run_kmeans_mfi(mfi_s, training_size=config.MFI_TRAINING)

    # ── Данные активной сделки ────────────────────────────────────
    entry_price  = None
    tp_price     = None
    sl_price     = None
    signal_side  = None
    signal_bar   = None

    if state_snapshot:
        entry_price = state_snapshot.get("entry")
        tp_price    = state_snapshot.get("tp")
        sl_price    = state_snapshot.get("sl")
        signal_side = state_snapshot.get("side")
        signal_bar  = state_snapshot.get("signal_bar")

    return build_chart(
        df=df,
        symbol=symbol,
        timeframe=timeframe,
        frama=frama_s,
        frama_upper=frama_u,
        frama_lower=frama_l,
        mfi=mfi_s,
        mfi_ob=mfi_ob,
        mfi_os=mfi_os,
        entry_price=entry_price,
        tp_price=tp_price,
        sl_price=sl_price,
        signal_side=signal_side,
        signal_bar=signal_bar,
        limit=limit,
    )
