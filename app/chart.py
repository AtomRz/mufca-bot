"""
MUFCA v4.0 — Chart Module
Generates candlestick charts with indicators for Discord.
Command: !chart [PAIR] [TIMEFRAME] [LIMIT]
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
import config as _cfg
from utils import format_price, round_price

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# 🎨  THEME
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
    "poc":        "#e6c619",
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
# 📐  CHART INDICATORS
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
    max_levels: int = 4
) -> Dict[str, List[float]]:
    """
    Support/resistance levels:
    - Pivot Points (local min/max)
    - Round Numbers (round-number levels)

    Returns dict: {"support": [...], "resistance": [...], "pivot": [...]}
    """
    close = df["close"]
    high = df["high"]
    low = df["low"]
    last_close = float(close.iloc[-2])  # confirmed bar

    supports: List[float] = []
    resistances: List[float] = []
    pivots: List[float] = []

    # 1. Pivot Points (local extremes)
    w = pivot_window
    for i in range(w, len(df) - w):
        hi_window = high.iloc[i - w:i + w + 1]
        lo_window = low.iloc[i - w:i + w + 1]
        if high.iloc[i] == hi_window.max():
            resistances.append(float(high.iloc[i]))
        if low.iloc[i] == lo_window.min():
            supports.append(float(low.iloc[i]))

    # Filter — keep only levels near the current price
    def near_price(levels, price, pct=0.05):
        return [l for l in levels if abs(l - price) / price < pct]

    # 🆕 FIX: direction filtering (resistance above price / support below
    # price) now happens BEFORE selecting the top-N nearest, not after.
    # Before, _cluster_levels could fill all max_levels slots with
    # candidates that would just get filtered out by the final direction
    # check anyway — e.g. after a sharp breakout upward, most historical
    # resistance pivots end up BELOW the current price (already broken
    # through), and if they're closer by distance, they'd crowd out the few
    # remaining levels ABOVE the price, leaving resistance empty even though
    # real resistance levels exist — just a bit farther out in price than
    # the already-broken ones.
    resistances = [l for l in resistances if l > last_close]
    supports = [l for l in supports if l < last_close]

    # 🆕 FIX: when selecting max_levels from clusters, sorting used to be by
    # closeness to the MEDIAN of the candidate set, not to the current
    # price — as a result, levels genuinely close to price (and therefore
    # the most trading-relevant) could get filtered out in favor of ones
    # more "typical of the sample" but far from price. Now sorted by
    # closeness to last_close — the nearest levels are always prioritized.
    supports    = _cluster_levels(near_price(supports,    last_close, 0.12), max_levels, last_close)
    resistances = _cluster_levels(near_price(resistances, last_close, 0.12), max_levels, last_close)

    return {
        "support":    supports,
        "resistance": resistances,
        "pivot":      [],
    }


def _cluster_levels(levels: List[float], max_n: int, ref_price: float, tol: float = 0.005) -> List[float]:
    """Clusters nearby levels, keeps up to max_n CLOSEST to ref_price (the current price)."""
    if not levels:
        return []
    levels = sorted(set(levels))
    clustered: List[float] = []
    used = [False] * len(levels)
    for i, l in enumerate(levels):
        if used[i]:
            continue
        used[i] = True  # mark the pivot element itself as consumed too, not just its cluster-mates
        cluster = [l]
        for j in range(i + 1, len(levels)):
            if not used[j] and abs(levels[j] - l) / (l + 1e-8) < tol:
                cluster.append(levels[j])
                used[j] = True
        clustered.append(float(np.mean(cluster)))
    if len(clustered) <= max_n:
        return sorted(clustered)
    clustered.sort(key=lambda x: abs(x - ref_price))
    return sorted(clustered[:max_n])


def calc_volume_profile(
    df: pd.DataFrame,
    bins: int = 50,
    value_area_pct: float = 0.70,
) -> Dict:
    """
    Volume Profile approximated from OHLCV — there's no tick-level trade
    data available (Gate.io's public API doesn't provide it, and we don't
    want to pull the full trade stream just for this), so this is a
    TPO-style approximation, not a "real" exchange volume profile: each
    bar's volume is distributed evenly across the price bins its
    [low, high] range spans, instead of weighting toward where trades
    actually printed within the bar. This is the standard approach used by
    most retail tools that only have OHLCV, and gives a statistically
    reasonable POC/Value Area — just not pixel-identical to what a
    tick-level profile would show.

    Returns:
        {
            "poc": float | None,   # Point of Control — price bin with the most volume
            "vah": float | None,   # Value Area High
            "val": float | None,   # Value Area Low
            "bins": [{"price": float, "volume": float}, ...],  # for histogram rendering, low to high
        }
    """
    empty = {"poc": None, "vah": None, "val": None, "bins": []}
    if len(df) < 10:
        return empty

    price_min = float(df["low"].min())
    price_max = float(df["high"].max())
    if price_max <= price_min:
        return empty

    bin_width = (price_max - price_min) / bins
    volume_by_bin = np.zeros(bins)

    for row in df.itertuples():
        low, high, vol = float(row.low), float(row.high), float(row.volume)
        if vol <= 0:
            continue
        if high <= low:
            # doji / zero-range bar — dump its volume into a single bin
            idx = min(max(int((low - price_min) / bin_width), 0), bins - 1)
            volume_by_bin[idx] += vol
            continue
        first_bin = min(max(int((low - price_min) / bin_width), 0), bins - 1)
        last_bin = min(max(int((high - price_min) / bin_width), 0), bins - 1)
        n_spanned = last_bin - first_bin + 1
        volume_by_bin[first_bin:last_bin + 1] += vol / n_spanned

    total_volume = float(volume_by_bin.sum())
    if total_volume <= 0:
        return empty

    poc_idx = int(np.argmax(volume_by_bin))
    poc_price = price_min + (poc_idx + 0.5) * bin_width

    # Expand the Value Area outward from the POC bin — at each step, add
    # whichever neighbor (above or below) has more volume — until we've
    # covered value_area_pct of the total.
    target_volume = total_volume * value_area_pct
    covered_volume = float(volume_by_bin[poc_idx])
    lo_idx = hi_idx = poc_idx
    while covered_volume < target_volume and (lo_idx > 0 or hi_idx < bins - 1):
        vol_below = float(volume_by_bin[lo_idx - 1]) if lo_idx > 0 else -1.0
        vol_above = float(volume_by_bin[hi_idx + 1]) if hi_idx < bins - 1 else -1.0
        if vol_above >= vol_below:
            hi_idx += 1
            covered_volume += float(volume_by_bin[hi_idx])
        else:
            lo_idx -= 1
            covered_volume += float(volume_by_bin[lo_idx])

    val_price = price_min + lo_idx * bin_width
    vah_price = price_min + (hi_idx + 1) * bin_width

    bins_out = [
        {"price": round_price(price_min + (i + 0.5) * bin_width), "volume": round(float(volume_by_bin[i]), 4)}
        for i in range(bins)
    ]

    return {
        "poc": round_price(poc_price),
        "vah": round_price(vah_price),
        "val": round_price(val_price),
        "bins": bins_out,
    }


# ─────────────────────────────────────────────────────────────────────
# 🏗️  CHART CONSTRUCTION
# ─────────────────────────────────────────────────────────────────────

def build_chart(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    df_full: Optional[pd.DataFrame] = None,
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
    signal_bar_offset: int = -2,        # signal bar offset FROM THE END of the displayed df
    limit: int = 50,
    volume_profile: Optional[Dict] = None,  # output of calc_volume_profile(), or None if disabled
) -> io.BytesIO:
    """
    Builds the full candlestick chart and returns a BytesIO PNG.

    Panels:
      [0] Candles + FRAMA + BB + S/R + Entry/TP/SL + signal arrows
      [1] Volume
      [2] KMeans MFI (if provided)

    🆕 FIX: signal_bar used to be an "absolute index into the original df
    before tail()", which the calling code (bot.py) computed against ITS OWN
    df (limit=100), while generate_chart internally does its OWN independent
    fetch (limit≈300+). The indices from the two different series didn't
    line up, and the arrow almost always ended up flying to the start of the
    chart (clamped to 0). Now signal_bar_offset is an offset FROM THE END of
    the already-displayed (tail-ed) df, independent of how many bars were
    originally fetched or how. Per the bot's rules a signal always forms on
    the last confirmed closed bar (iloc[-2]), so the default is -2.
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

    ax_c = fig.add_subplot(gs[0])  # candles
    ax_v = fig.add_subplot(gs[1], sharex=ax_c)  # volume
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
    ax_c.set_xticklabels([""] * len(tick_positions))  # hide on the top panel

    # ── Bollinger Bands — computed from the full df, take tail(limit) ──
    _bb_src = df_full["close"] if df_full is not None and len(df_full) > len(df) else df["close"]
    _bb_u_full, _bb_m_full, _bb_l_full = calc_bollinger_bands(_bb_src, period=_cfg.BB_PERIOD, std_mult=_cfg.BB_STDDEV)
    bb_u = _bb_u_full.tail(limit).values
    bb_m = _bb_m_full.tail(limit).values
    bb_l = _bb_l_full.tail(limit).values
    ax_c.fill_between(x, bb_l, bb_u, alpha=0.06, color=T["bb_fill"], zorder=1)
    ax_c.plot(x, bb_u, color=T["bb_band"], linewidth=0.8, alpha=0.7, zorder=2)
    ax_c.plot(x, bb_m, color=T["bb_mid"], linewidth=0.8, alpha=0.6, linestyle="--", zorder=2)
    ax_c.plot(x, bb_l, color=T["bb_band"], linewidth=0.8, alpha=0.7, zorder=2)

    # ── S/R levels — computed from the full df (200+ bars) ─────────────
    _sr_df = df_full if df_full is not None and len(df_full) > len(df) else df
    sr = calc_support_resistance(_sr_df, pivot_window=_cfg.SR_PIVOT_WINDOW, max_levels=_cfg.SR_MAX_LEVELS)
    x_start = -0.5
    x_end   = n - 0.5

    for lvl in sr["support"]:
        ax_c.hlines(lvl, x_start, x_end, colors=T["support"],
                    linewidth=1.4, linestyles="--", alpha=0.85, zorder=7)
        # 🆕 FIX: was f"S {lvl:,.0f}" — ZERO decimal places. For a sub-$1 pair
        # (DOGE, SHIB, etc.) every S/R label on the chart would render as "S 0",
        # indistinguishable from every other level. format_price() scales
        # precision to the price's magnitude instead.
        ax_c.text(n - 0.5, lvl, f"S {format_price(lvl)}", color=T["support"],
                  fontsize=8, va="bottom", ha="right", fontweight="bold",
                  bbox=dict(facecolor=T["bg2"], edgecolor="none", pad=1, alpha=0.7),
                  zorder=8)

    for lvl in sr["resistance"]:
        ax_c.hlines(lvl, x_start, x_end, colors=T["resist"],
                    linewidth=1.4, linestyles="--", alpha=0.85, zorder=7)
        ax_c.text(n - 0.5, lvl, f"R {format_price(lvl)}", color=T["resist"],
                  fontsize=8, va="bottom", ha="right", fontweight="bold",
                  bbox=dict(facecolor=T["bg2"], edgecolor="none", pad=1, alpha=0.7),
                  zorder=8)

    # ── Volume Profile: Value Area band + POC line ───────────────────
    # Deliberately a distinct gold color (T["poc"]), not a variant of the
    # pivot S/R colors — POC/Value Area are a different kind of level
    # (where volume concentrated) from pivot S/R (local price extremes),
    # and should read as visually separate on the chart, not as "more S/R".
    if volume_profile and volume_profile.get("poc") is not None:
        vah = volume_profile.get("vah")
        val = volume_profile.get("val")
        poc = volume_profile["poc"]
        if vah is not None and val is not None:
            ax_c.fill_between(x, val, vah, alpha=0.06, color=T["poc"], zorder=1)
        ax_c.hlines(poc, x_start, x_end, colors=T["poc"], linewidth=2.0, zorder=7, alpha=0.95)
        ax_c.text(n - 0.5, poc, f"POC {format_price(poc)}", color=T["poc"],
                  fontsize=8, va="bottom", ha="right", fontweight="bold",
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

    # ── Candles ───────────────────────────────────────────────────────
    bar_w = 0.6
    for i in range(n):
        o = df["open"].iloc[i]
        h = df["high"].iloc[i]
        l = df["low"].iloc[i]
        c = df["close"].iloc[i]
        bull = c >= o
        color      = T["bull"]      if bull else T["bear"]
        body_color = T["bull_body"] if bull else T["bear_body"]

        # Wick
        ax_c.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=5)
        # Body
        body_h = abs(c - o) if abs(c - o) > 0 else (h - l) * 0.01
        rect = Rectangle(
            (i - bar_w / 2, min(o, c)),
            bar_w, body_h,
            facecolor=body_color, edgecolor=color,
            linewidth=0.6, zorder=6
        )
        ax_c.add_patch(rect)

    # ── Signal (arrow) ────────────────────────────────────────────
    if signal_side is not None:
        # signal_bar_offset — offset from the end of the displayed df (e.g.
        # -2 = the last confirmed closed bar). Independent of what fetch
        # request the df was built from.
        offset = signal_bar_offset if signal_bar_offset is not None else -2
        idx = n + offset if offset < 0 else offset
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

    # ── Entry / TP / SL lines ────────────────────────────────────────
    # 🆕 FIX: these three labels used to be f"...{price:,.2f}" — a fixed 2
    # decimal places, same class of bug as the S/R labels above: on a
    # sub-$1 pair, Entry/TP/SL could render as visually identical or
    # collapse toward the same rounded value.
    if entry_price:
        ax_c.hlines(entry_price, x_start, x_end, colors=T["entry"],
                    linewidth=1.2, linestyles="-", zorder=8, alpha=0.9)
        ax_c.text(0, entry_price, f"ENTRY {format_price(entry_price)}",
                  color=T["entry"], fontsize=8, va="bottom", fontweight="bold")

    if tp_price:
        ax_c.hlines(tp_price, x_start, x_end, colors=T["tp"],
                    linewidth=1.0, linestyles="-.", zorder=8, alpha=0.9)
        ax_c.text(0, tp_price, f"TP {format_price(tp_price)}",
                  color=T["tp"], fontsize=8, va="bottom", fontweight="bold")

    if sl_price:
        ax_c.hlines(sl_price, x_start, x_end, colors=T["sl"],
                    linewidth=1.0, linestyles="-.", zorder=8, alpha=0.9)
        ax_c.text(0, sl_price, f"SL {format_price(sl_price)}",
                  color=T["sl"], fontsize=8, va="top", fontweight="bold")

    # ── TP/SL zone fill ──────────────────────────────────────────
    if entry_price and tp_price and sl_price:
        ax_c.fill_between(x, entry_price, tp_price,
                          alpha=0.05, color=T["tp"], zorder=1)
        ax_c.fill_between(x, sl_price, entry_price,
                          alpha=0.05, color=T["sl"], zorder=1)

    # ── Grid and styling for the main panel ──────────────────────
    ax_c.grid(True, color=T["grid"], linewidth=0.5, alpha=0.6, zorder=0)
    ax_c.set_xlim(-0.5, n - 0.5)
    ax_c.yaxis.set_label_position("right")

    # Title
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
    # 🆕 FIX: was f"${last_close:,.2f}  ..." — same fixed-2-decimal issue.
    fig.text(
        0.25, 0.955,
        f"${format_price(last_close)}  {chg_sign}{chg_pct:.2f}%",
        color=chg_color, fontsize=12, fontweight="bold", va="top"
    )

    # Legend
    legend_elements = [
        Line2D([0], [0], color=T["frama"],   linewidth=1.4, label="FRAMA"),
        Line2D([0], [0], color=T["bb_mid"],  linewidth=0.8, linestyle="--", label="BB mid"),
        Line2D([0], [0], color=T["bb_band"], linewidth=0.8, label="BB bands"),
        Line2D([0], [0], color=T["support"], linewidth=0.8, linestyle="--", label="Support"),
        Line2D([0], [0], color=T["resist"],  linewidth=0.8, linestyle="--", label="Resist"),
    ]
    if entry_price:
        legend_elements.append(
            Line2D([0], [0], color=T["entry"], linewidth=1.2, label="Entry")
        )
    if volume_profile and volume_profile.get("poc") is not None:
        legend_elements.append(
            Line2D([0], [0], color=T["poc"], linewidth=1.6, label="POC / Value Area")
        )
    ax_c.legend(
        handles=legend_elements,
        loc="upper left", fontsize=7,
        facecolor=T["bg"], edgecolor=T["grid"],
        labelcolor=T["text_dim"], framealpha=0.8
    )

    # ── Volume Profile histogram (narrow inset, right edge of the candle panel) ──
    # A separate axes sharing ax_c's y-limits (price axis), so each bar lands
    # at the correct price level regardless of the candle panel's own
    # autoscale. Positioned just inside ax_c's own right edge rather than as
    # a separate gridspec column — keeps the existing layout/margins
    # untouched when volume_profile is None (the common case if the feature
    # is disabled).
    if volume_profile and volume_profile.get("bins"):
        vp_bins = volume_profile["bins"]
        volumes = [b["volume"] for b in vp_bins]
        max_vol = max(volumes) if volumes else 0
        if max_vol > 0:
            bbox = ax_c.get_position()
            profile_width = bbox.width * 0.16  # ~16% of the candle panel's width
            ax_vp = fig.add_axes([bbox.x1 - profile_width, bbox.y0, profile_width, bbox.height])
            ax_vp.set_ylim(ax_c.get_ylim())
            ax_vp.axis("off")
            ax_vp.patch.set_alpha(0)

            vah = volume_profile.get("vah")
            val = volume_profile.get("val")
            for b in vp_bins:
                price = b["price"]
                vol = b["volume"]
                # Skip near-empty bins — at ~0 width they still leave a
                # visible sliver right at the profile's left edge (x=0);
                # stacked together, many of them read as a stray vertical line.
                if vol / max_vol < 0.06:
                    continue
                y0, y1 = ax_c.get_ylim()
                if price < y0 or price > y1:
                    continue  # outside the visible price range — skip
                in_value_area = val is not None and vah is not None and val <= price <= vah
                width = (vol / max_vol) * 0.94  # fraction of the inset axes' own width
                bar_color = T["poc"] if in_value_area else T["text_dim"]
                bar_alpha = 0.85 if in_value_area else 0.35
                bin_height = (df_full["high"].max() - df_full["low"].min()) / len(vp_bins) if df_full is not None else (y1 - y0) / len(vp_bins)
                ax_vp.barh(
                    price, width, height=bin_height * 0.9,
                    left=0, color=bar_color, alpha=bar_alpha, zorder=3, edgecolor="none",
                )
            ax_vp.set_xlim(0, 1)

    # ── Volume panel ────────────────────────────────────────────────
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

    # ── MFI panel ───────────────────────────────────────────────────
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

    # ── Save ───────────────────────────────────────────────────────
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=T["bg"], edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────────────────────────────
# 🤖  PUBLIC FUNCTION FOR THE BOT
# ─────────────────────────────────────────────────────────────────────

async def generate_chart(
    exchange,
    symbol: str,
    timeframe: str,
    limit: int = 50,
    state_snapshot: Optional[dict] = None,
) -> io.BytesIO:
    """
    Main entry point: downloads OHLCV, computes indicators, builds the PNG.

    Args:
        exchange:        ccxt.Exchange instance
        symbol:          "BTC/USDT"
        timeframe:       "1h" | "4h"
        limit:           number of candles (default 50)
        state_snapshot:  dict with entry, tp, sl, side, signal_bar_offset
                          fields (optional; offset from the end of df,
                          default -2 — the last closed bar)

    Returns:
        BytesIO PNG buffer
    """
    import asyncio
    from utils import safe_fetch_ohlcv, parse_ohlcv, validate_dataframe
    from indicators import (
        calculate_frama, calculate_mfi, run_kmeans_mfi,
        calculate_atr
    )

    # Fetch more data to warm up the indicators
    fetch_limit = max(limit + 250, 300)
    bars = await safe_fetch_ohlcv(exchange, symbol, timeframe, limit=fetch_limit)
    df = parse_ohlcv(bars)

    if not validate_dataframe(df, min_rows=50):
        raise ValueError(f"Not enough data for {symbol} {timeframe}")

    # ── Indicators (module-level _cfg — sees live edits from the web UI) ──
    frama_s, frama_u, frama_l, _ = calculate_frama(
        df, length=_cfg.FRAMA_LEN, mult=_cfg.FRAMA_MULT
    )

    mfi_s = calculate_mfi(df, length=_cfg.MFI_LEN)
    mfi_os, mfi_ob = run_kmeans_mfi(mfi_s, training_size=_cfg.MFI_TRAINING)

    # 🆕 Volume Profile (POC / Value Area) — see calc_volume_profile() for
    # the TPO-style approximation used since only OHLCV is available.
    # Computed over its own lookback window (config.VP_LOOKBACK, independent
    # of `limit` — the number of candles actually shown on the chart), so
    # the profile reflects where volume concentrated over a meaningfully
    # long history, not just whatever's currently on screen.
    volume_profile = None
    if _cfg.VP_ENABLED:
        vp_window = df.tail(min(_cfg.VP_LOOKBACK, len(df)))
        volume_profile = calc_volume_profile(vp_window, bins=_cfg.VP_BINS, value_area_pct=_cfg.VP_VALUE_AREA_PCT)
        if not _cfg.VP_SHOW_HISTOGRAM:
            volume_profile["bins"] = []  # keep POC/VA lines, drop the histogram bars

    # ── Active trade data ────────────────────────────────────
    entry_price       = None
    tp_price          = None
    sl_price          = None
    signal_side       = None
    signal_bar_offset = -2

    if state_snapshot:
        entry_price = state_snapshot.get("entry")
        tp_price    = state_snapshot.get("tp")
        sl_price    = state_snapshot.get("sl")
        signal_side = state_snapshot.get("side")

        entry_time_ms = state_snapshot.get("entry_time_ms")
        if entry_time_ms is not None:
            # 🆕 FIX: the trade may have been opened many bars ago, on a
            # different df. We look up the real bar by timestamp in OUR OWN
            # just-fetched df, instead of trusting a positional index from
            # someone else's df.
            try:
                ts_arr = df["timestamp"].values
                closest_i = int(np.argmin(np.abs(ts_arr - float(entry_time_ms))))
                # If the real entry is earlier than the chart's visible
                # window (limit bars), argmin will still find the "closest"
                # one — usually the chart's first bar — drawing the marker
                # in a definitely wrong spot. Check a tolerance (1.5 bar
                # intervals); if it doesn't fit, use the default offset (-2,
                # the last closed bar) instead of a false location.
                bar_interval_ms = float(np.median(np.diff(ts_arr))) if len(ts_arr) > 1 else 0
                actual_diff = abs(ts_arr[closest_i] - float(entry_time_ms))
                if bar_interval_ms > 0 and actual_diff > bar_interval_ms * 1.5:
                    signal_bar_offset = -2
                else:
                    signal_bar_offset = closest_i - len(df)  # offset from the end, always <= -1
            except Exception as e:
                logger.warning(f"[CHART] Failed to resolve entry_time_ms to bar index: {e}")
                signal_bar_offset = -2
        else:
            signal_bar_offset = state_snapshot.get("signal_bar_offset", -2)

    # 🆕 FIX: build_chart() is a heavy synchronous matplotlib call (CPU-bound
    # rendering + PNG encode, easily 100-300ms). Calling it directly here would
    # block the whole asyncio event loop — including the scanner loop and every
    # other coroutine — for that whole duration on every single signal chart.
    # Offload to a worker thread so the event loop stays responsive.
    return await asyncio.to_thread(
        build_chart,
        df=df,
        symbol=symbol,
        timeframe=timeframe,
        df_full=df,
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
        signal_bar_offset=signal_bar_offset,
        limit=limit,
        volume_profile=volume_profile,
    )
