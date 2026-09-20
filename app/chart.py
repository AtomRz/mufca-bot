"""
MUFCA Chart Module

Renders candles, indicators, legacy support/resistance, volume profile,
and Demand/Supply zones from the shared market structure engine.

All source comments and strings are ASCII-only.
"""

import asyncio
import io
import logging
from typing import Optional, Tuple, Dict, List

import matplotlib
matplotlib.use("Agg")
import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle

import config as _cfg
from utils import format_price
from market_structure import (
    calc_support_resistance,
    calc_volume_profile,
    detect_demand_supply_zones,
    zone_absolute_index,
)

logger = logging.getLogger(__name__)


THEME = {
    "bg": "#0d1117",
    "bg2": "#161b22",
    "grid": "#21262d",
    "text": "#c9d1d9",
    "text_dim": "#6e7681",
    "bull": "#26a641",
    "bear": "#f85149",
    "bull_body": "#1a7f37",
    "bear_body": "#b91c1c",
    "volume": "#388bfd",
    "frama": "#f0883e",
    "bb_mid": "#a5d6ff",
    "bb_band": "#388bfd",
    "bb_fill": "#388bfd",
    "support": "#00bcd4",
    "resist": "#9e9e9e",
    "poc": "#e6c619",
    "entry": "#f0883e",
    "tp": "#26a641",
    "sl": "#f85149",
    "signal_long": "#26a641",
    "signal_short": "#f85149",
    "mfi_line": "#a371f7",
    "mfi_ob": "#f85149",
    "mfi_os": "#26a641",
    "demand": "#00d4a8",
    "supply": "#ff4d8d",
}

_VP_STRIP_FRAC = 0.16


def calc_bollinger_bands(
    close: pd.Series,
    period: int = 20,
    std_mult: float = 2.0,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    mid = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return upper, mid, lower


def _cfg_value(name: str, default):
    return getattr(_cfg, name, default)


def _zone_alpha(score: float, state: str) -> float:
    base = 0.07 + min(max(float(score), 0.0), 100.0) / 100.0 * 0.10
    if state == "fresh":
        return min(0.20, base + 0.03)
    if state == "tested":
        return min(0.17, base + 0.01)
    return min(0.14, base)


def _zone_label(zone: Dict, side: str) -> str:
    state = str(zone.get("state", "unknown")).upper()
    score = float(zone.get("score", 0.0))
    touches = int(zone.get("touches", 0))
    return f"{side.upper()} {score:.0f} {state} T{touches}"


def _prepare_zone_positions(
    zones: Dict[str, List[Dict]],
    full_len: int,
    display_start: int,
    lookback: int,
) -> Dict[str, List[Dict]]:
    result = {"demand": [], "supply": []}
    for side in ("demand", "supply"):
        for source in zones.get(side, []):
            zone = dict(source)
            absolute_index = zone_absolute_index(full_len, lookback, zone.get("created_bar", 0))
            zone["chart_index"] = int(absolute_index - display_start)
            result[side].append(zone)
    return result


def _draw_zones(
    ax,
    zones: Dict[str, List[Dict]],
    n: int,
    x_start: float,
    x_end: float,
    theme: Dict,
):
    for side, color_key in (("demand", "demand"), ("supply", "supply")):
        color = theme[color_key]
        for zone in zones.get(side, []):
            low = float(zone.get("low", 0.0))
            high = float(zone.get("high", 0.0))
            if not np.isfinite(low) or not np.isfinite(high) or high <= low:
                continue

            start = float(zone.get("chart_index", 0))
            start = max(x_start, min(start, x_end))
            state = str(zone.get("state", "unknown"))
            score = float(zone.get("score", 0.0))
            alpha = _zone_alpha(score, state)

            rect = Rectangle(
                (start, low),
                max(0.75, x_end - start),
                high - low,
                facecolor=color,
                edgecolor=color,
                linewidth=0.9,
                linestyle="-" if state == "fresh" else "--",
                alpha=alpha,
                zorder=1.8,
            )
            ax.add_patch(rect)

            label_x = (start + x_end) / 2.0
            label_y = (low + high) / 2.0
            label = _zone_label(zone, side)
            ax.text(
                label_x,
                label_y,
                label,
                color=color,
                fontsize=6.5,
                va="center",
                ha="center",
                fontweight="bold",
                bbox=dict(facecolor=theme["bg2"], edgecolor=color, linewidth=0.5, pad=1.5, alpha=0.78),
                zorder=9,
                clip_on=True,
            )


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
    signal_side: Optional[str] = None,
    signal_bar_offset: int = -2,
    limit: int = 50,
    volume_profile: Optional[Dict] = None,
    demand_supply_zones: Optional[Dict[str, List[Dict]]] = None,
) -> io.BytesIO:
    """Build the chart PNG and return it as a BytesIO object."""
    original_display_len = len(df)
    display_start = max(0, original_display_len - limit)
    df = df.tail(limit).copy().reset_index(drop=True)
    n = len(df)
    has_mfi = mfi is not None and len(mfi) >= limit

    T = THEME
    fig = plt.figure(figsize=(14, 9 if has_mfi else 8), facecolor=T["bg"])
    if has_mfi:
        gs = gridspec.GridSpec(
            3, 1, height_ratios=[5, 1.2, 1.2],
            hspace=0.04, left=0.06, right=0.95, top=0.93, bottom=0.07,
        )
    else:
        gs = gridspec.GridSpec(
            2, 1, height_ratios=[5, 1.2],
            hspace=0.04, left=0.06, right=0.95, top=0.93, bottom=0.07,
        )

    ax_c = fig.add_subplot(gs[0])
    ax_v = fig.add_subplot(gs[1], sharex=ax_c)
    ax_m = fig.add_subplot(gs[2], sharex=ax_c) if has_mfi else None
    for ax in ([ax_c, ax_v] + ([ax_m] if ax_m else [])):
        ax.set_facecolor(T["bg2"])
        ax.tick_params(colors=T["text_dim"], labelsize=8)
        ax.yaxis.tick_right()
        for spine in ax.spines.values():
            spine.set_edgecolor(T["grid"])

    x = np.arange(n)
    timestamps = pd.to_datetime(df["timestamp"], unit="ms")
    step = max(1, n // 8)
    tick_positions = x[::step]
    tick_labels = [timestamps.iloc[i].strftime("%d/%m %H:%M") for i in tick_positions]
    ax_c.set_xticks(tick_positions)
    ax_c.set_xticklabels([""] * len(tick_positions))

    full = df_full if df_full is not None else df
    _bb_u_full, _bb_m_full, _bb_l_full = calc_bollinger_bands(
        full["close"],
        period=_cfg_value("BB_PERIOD", 20),
        std_mult=_cfg_value("BB_STDDEV", 2.0),
    )
    bb_u = _bb_u_full.tail(limit).values
    bb_m = _bb_m_full.tail(limit).values
    bb_l = _bb_l_full.tail(limit).values
    ax_c.fill_between(x, bb_l, bb_u, alpha=0.06, color=T["bb_fill"], zorder=1)
    ax_c.plot(x, bb_u, color=T["bb_band"], linewidth=0.8, alpha=0.7, zorder=2)
    ax_c.plot(x, bb_m, color=T["bb_mid"], linewidth=0.8, alpha=0.6, linestyle="--", zorder=2)
    ax_c.plot(x, bb_l, color=T["bb_band"], linewidth=0.8, alpha=0.7, zorder=2)

    sr = calc_support_resistance(
        full,
        pivot_window=_cfg_value("SR_PIVOT_WINDOW", 10),
        max_levels=_cfg_value("SR_MAX_LEVELS", 4),
    )
    x_start = -0.5
    x_end = n - 0.5

    for lvl in sr.get("support", []):
        ax_c.hlines(lvl, x_start, x_end, colors=T["support"], linewidth=1.4, linestyles="--", alpha=0.85, zorder=7)
        ax_c.text(
            n - 0.5, lvl, f"S {format_price(lvl)}", color=T["support"],
            fontsize=8, va="bottom", ha="right", fontweight="bold",
            bbox=dict(facecolor=T["bg2"], edgecolor="none", pad=1, alpha=0.7), zorder=8,
        )
    for lvl in sr.get("resistance", []):
        ax_c.hlines(lvl, x_start, x_end, colors=T["resist"], linewidth=1.4, linestyles="--", alpha=0.85, zorder=7)
        ax_c.text(
            n - 0.5, lvl, f"R {format_price(lvl)}", color=T["resist"],
            fontsize=8, va="bottom", ha="right", fontweight="bold",
            bbox=dict(facecolor=T["bg2"], edgecolor="none", pad=1, alpha=0.7), zorder=8,
        )

    if demand_supply_zones:
        _draw_zones(ax_c, demand_supply_zones, n, x_start, x_end, T)

    vp_x_end = x_end - (x_end - x_start) * _VP_STRIP_FRAC
    if volume_profile and volume_profile.get("poc") is not None:
        vah = volume_profile.get("vah")
        val = volume_profile.get("val")
        poc = volume_profile["poc"]
        if vah is not None and val is not None:
            ax_c.fill_between([x_start, vp_x_end], val, vah, alpha=0.06, color=T["poc"], zorder=1)
        ax_c.hlines(poc, x_start, vp_x_end, colors=T["poc"], linewidth=2.0, zorder=7, alpha=0.95)
        ax_c.text(
            n - 0.5, poc, f"POC {format_price(poc)}", color=T["poc"],
            fontsize=8, va="bottom", ha="right", fontweight="bold",
            bbox=dict(facecolor=T["bg2"], edgecolor="none", pad=1, alpha=0.7), zorder=8,
        )

    if frama is not None and len(frama) >= limit:
        fs = frama.tail(limit).values
        ax_c.plot(x, fs, color=T["frama"], linewidth=1.4, zorder=4, label="FRAMA")
        if frama_upper is not None and frama_lower is not None:
            fu = frama_upper.tail(limit).values
            fl = frama_lower.tail(limit).values
            ax_c.fill_between(x, fl, fu, alpha=0.08, color=T["frama"], zorder=1)
            ax_c.plot(x, fu, color=T["frama"], linewidth=0.5, alpha=0.4, zorder=2)
            ax_c.plot(x, fl, color=T["frama"], linewidth=0.5, alpha=0.4, zorder=2)

    bar_w = 0.6
    for i in range(n):
        o = float(df["open"].iloc[i])
        h = float(df["high"].iloc[i])
        l = float(df["low"].iloc[i])
        c = float(df["close"].iloc[i])
        bull = c >= o
        color = T["bull"] if bull else T["bear"]
        body_color = T["bull_body"] if bull else T["bear_body"]
        ax_c.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=5)
        body_h = abs(c - o) if abs(c - o) > 0 else max((h - l) * 0.01, 1e-12)
        rect = Rectangle(
            (i - bar_w / 2, min(o, c)), bar_w, body_h,
            facecolor=body_color, edgecolor=color, linewidth=0.6, zorder=6,
        )
        ax_c.add_patch(rect)

    if signal_side is not None and n:
        offset = signal_bar_offset if signal_bar_offset is not None else -2
        idx = n + offset if offset < 0 else offset
        idx = max(0, min(idx, n - 1))
        if signal_side == "long":
            y_arrow = float(df["low"].iloc[idx]) * 0.999
            ax_c.annotate(
                "LONG", xy=(idx, y_arrow), xytext=(idx, y_arrow * 0.996),
                color=T["signal_long"], fontsize=9, fontweight="bold",
                ha="center", va="top", zorder=9,
                arrowprops=dict(arrowstyle="->", color=T["signal_long"], lw=1.5),
            )
        else:
            y_arrow = float(df["high"].iloc[idx]) * 1.001
            ax_c.annotate(
                "SHORT", xy=(idx, y_arrow), xytext=(idx, y_arrow * 1.004),
                color=T["signal_short"], fontsize=9, fontweight="bold",
                ha="center", va="bottom", zorder=9,
                arrowprops=dict(arrowstyle="->", color=T["signal_short"], lw=1.5),
            )

    if entry_price:
        ax_c.hlines(entry_price, x_start, x_end, colors=T["entry"], linewidth=1.2, zorder=8, alpha=0.9)
        ax_c.text(0, entry_price, f"ENTRY {format_price(entry_price)}", color=T["entry"], fontsize=8, va="bottom", fontweight="bold")
    if tp_price:
        ax_c.hlines(tp_price, x_start, x_end, colors=T["tp"], linewidth=1.0, linestyles="-.", zorder=8, alpha=0.9)
        ax_c.text(0, tp_price, f"TP {format_price(tp_price)}", color=T["tp"], fontsize=8, va="bottom", fontweight="bold")
    if sl_price:
        ax_c.hlines(sl_price, x_start, x_end, colors=T["sl"], linewidth=1.0, linestyles="-.", zorder=8, alpha=0.9)
        ax_c.text(0, sl_price, f"SL {format_price(sl_price)}", color=T["sl"], fontsize=8, va="top", fontweight="bold")
    if entry_price and tp_price and sl_price:
        ax_c.fill_between(x, entry_price, tp_price, alpha=0.05, color=T["tp"], zorder=1)
        ax_c.fill_between(x, sl_price, entry_price, alpha=0.05, color=T["sl"], zorder=1)

    ax_c.grid(True, color=T["grid"], linewidth=0.5, alpha=0.6, zorder=0)
    ax_c.set_xlim(x_start, x_end)
    ax_c.yaxis.set_label_position("right")

    last_close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-2]) if n > 1 else last_close
    chg_pct = (last_close - prev_close) / prev_close * 100 if prev_close else 0.0
    chg_color = T["bull"] if chg_pct >= 0 else T["bear"]
    chg_sign = "+" if chg_pct >= 0 else ""
    fig.text(0.06, 0.955, f"{symbol}  -  {timeframe}", color=T["text"], fontsize=13, fontweight="bold", va="top")
    fig.text(0.25, 0.955, f"${format_price(last_close)}  {chg_sign}{chg_pct:.2f}%", color=chg_color, fontsize=12, fontweight="bold", va="top")

    legend_elements = [
        Line2D([0], [0], color=T["frama"], linewidth=1.4, label="FRAMA"),
        Line2D([0], [0], color=T["bb_mid"], linewidth=0.8, linestyle="--", label="BB mid"),
        Line2D([0], [0], color=T["bb_band"], linewidth=0.8, label="BB bands"),
        Line2D([0], [0], color=T["support"], linewidth=0.8, linestyle="--", label="Support"),
        Line2D([0], [0], color=T["resist"], linewidth=0.8, linestyle="--", label="Resist"),
        Line2D([0], [0], color=T["demand"], linewidth=5, alpha=0.55, label="Demand zone"),
        Line2D([0], [0], color=T["supply"], linewidth=5, alpha=0.55, label="Supply zone"),
    ]
    if entry_price:
        legend_elements.append(Line2D([0], [0], color=T["entry"], linewidth=1.2, label="Entry"))
    if volume_profile and volume_profile.get("poc") is not None:
        legend_elements.append(Line2D([0], [0], color=T["poc"], linewidth=1.6, label="POC / Value Area"))
    ax_c.legend(
        handles=legend_elements,
        loc="upper left", fontsize=7,
        facecolor=T["bg"], edgecolor=T["grid"],
        labelcolor=T["text_dim"], framealpha=0.8,
        ncol=2,
    )

    if volume_profile and volume_profile.get("bins"):
        vp_bins = volume_profile["bins"]
        volumes = [b["volume"] for b in vp_bins]
        max_vol = max(volumes) if volumes else 0
        if max_vol > 0:
            bbox = ax_c.get_position()
            profile_width = bbox.width * _VP_STRIP_FRAC
            ax_vp = fig.add_axes([bbox.x1 - profile_width, bbox.y0, profile_width, bbox.height])
            ax_vp.set_ylim(ax_c.get_ylim())
            ax_vp.axis("off")
            ax_vp.patch.set_alpha(0)
            vah = volume_profile.get("vah")
            val = volume_profile.get("val")
            y0, y1 = ax_c.get_ylim()
            bin_height = (y1 - y0) / max(1, len(vp_bins))
            for b in vp_bins:
                price = float(b["price"])
                vol = float(b["volume"])
                if vol / max_vol < 0.06 or price < y0 or price > y1:
                    continue
                in_value_area = val is not None and vah is not None and val <= price <= vah
                width = (vol / max_vol) * 0.94
                bar_color = T["poc"] if in_value_area else T["text_dim"]
                bar_alpha = 0.55 if in_value_area else 0.18
                ax_vp.barh(price, width, height=bin_height * 0.6, left=1 - width, color=bar_color, alpha=bar_alpha, zorder=3, edgecolor="none")
            ax_vp.set_xlim(0, 1)

    vol_colors = [T["bull"] if df["close"].iloc[i] >= df["open"].iloc[i] else T["bear"] for i in range(n)]
    ax_v.bar(x, df["volume"], color=vol_colors, width=0.7, alpha=0.7, zorder=3)
    ax_v.set_ylabel("Vol", color=T["text_dim"], fontsize=7, rotation=0, labelpad=20)
    ax_v.grid(True, color=T["grid"], linewidth=0.4, alpha=0.5, zorder=0)
    ax_v.yaxis.set_major_formatter(plt.FuncFormatter(lambda val, _: f"{val / 1e3:.0f}K" if val >= 1000 else f"{val:.0f}"))

    if ax_m is not None and mfi is not None:
        mfi_vals = mfi.tail(limit).values
        ax_m.plot(x, mfi_vals, color=T["mfi_line"], linewidth=1.0, zorder=3)
        ax_m.axhline(mfi_ob, color=T["mfi_ob"], linewidth=0.6, linestyle="--", alpha=0.7)
        ax_m.axhline(mfi_os, color=T["mfi_os"], linewidth=0.6, linestyle="--", alpha=0.7)
        ax_m.fill_between(x, mfi_os, mfi_vals, where=(mfi_vals <= mfi_os), alpha=0.2, color=T["mfi_os"], zorder=1)
        ax_m.fill_between(x, mfi_ob, mfi_vals, where=(mfi_vals >= mfi_ob), alpha=0.2, color=T["mfi_ob"], zorder=1)
        ax_m.set_ylim(0, 100)
        ax_m.set_ylabel("MFI", color=T["text_dim"], fontsize=7, rotation=0, labelpad=20)
        ax_m.grid(True, color=T["grid"], linewidth=0.4, alpha=0.5, zorder=0)
        ax_m.set_xticks(tick_positions)
        ax_m.set_xticklabels(tick_labels, rotation=30, ha="right", fontsize=7, color=T["text_dim"])
    else:
        ax_v.set_xticks(tick_positions)
        ax_v.set_xticklabels(tick_labels, rotation=30, ha="right", fontsize=7, color=T["text_dim"])

    plt.setp(ax_c.get_xticklabels(), visible=False)
    plt.setp(ax_v.get_xticklabels(), visible=False if ax_m else True)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=T["bg"], edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf


async def generate_chart(
    exchange,
    symbol: str,
    timeframe: str,
    limit: int = 50,
    state_snapshot: Optional[dict] = None,
) -> io.BytesIO:
    """Fetch market data, compute indicators and render the chart."""
    from utils import safe_fetch_ohlcv, parse_ohlcv, validate_dataframe
    from indicators import calculate_frama, calculate_mfi, run_kmeans_mfi

    fetch_limit = max(limit + 250, 300)
    bars = await safe_fetch_ohlcv(exchange, symbol, timeframe, limit=fetch_limit)
    df = parse_ohlcv(bars)
    if not validate_dataframe(df, min_rows=50):
        raise ValueError(f"Not enough data for {symbol} {timeframe}")

    frama_s, frama_u, frama_l, _ = calculate_frama(
        df, length=_cfg_value("FRAMA_LEN", 16), mult=_cfg_value("FRAMA_MULT", 1.6)
    )
    mfi_s = calculate_mfi(df, length=_cfg_value("MFI_LEN", 7))
    mfi_os, mfi_ob = run_kmeans_mfi(mfi_s, training_size=_cfg_value("MFI_TRAINING", 800))

    volume_profile = None
    if _cfg_value("VP_ENABLED", False):
        confirmed_df = df.iloc[:-1]
        vp_window = confirmed_df.tail(min(_cfg_value("VP_LOOKBACK", 200), len(confirmed_df)))
        volume_profile = calc_volume_profile(
            vp_window,
            bins=_cfg_value("VP_BINS", 50),
            value_area_pct=_cfg_value("VP_VALUE_AREA_PCT", 0.70),
        )
        if not _cfg_value("VP_SHOW_HISTOGRAM", True):
            volume_profile["bins"] = []

    zone_lookback = int(_cfg_value("ZONE_LOOKBACK", 300))
    zone_data = detect_demand_supply_zones(
        df,
        atr_period=int(_cfg_value("ZONE_ATR_PERIOD", 14)),
        lookback=zone_lookback,
        base_bars=int(_cfg_value("ZONE_BASE_BARS", 4)),
        impulse_bars=int(_cfg_value("ZONE_IMPULSE_BARS", 3)),
        max_zones=int(_cfg_value("ZONE_MAX_ZONES", 5)),
        max_base_atr=float(_cfg_value("ZONE_MAX_BASE_ATR", 1.6)),
        min_displacement_atr=float(_cfg_value("ZONE_MIN_DISPLACEMENT_ATR", 1.1)),
        min_volume_ratio=float(_cfg_value("ZONE_MIN_VOLUME_RATIO", 1.15)),
    )

    display_start = max(0, len(df) - limit)
    positioned_zones = _prepare_zone_positions(zone_data, len(df), display_start, zone_lookback)

    entry_price = None
    tp_price = None
    sl_price = None
    signal_side = None
    signal_bar_offset = -2

    if state_snapshot:
        entry_price = state_snapshot.get("entry")
        tp_price = state_snapshot.get("tp")
        sl_price = state_snapshot.get("sl")
        signal_side = state_snapshot.get("side")
        entry_time_ms = state_snapshot.get("entry_time_ms")
        if entry_time_ms is not None:
            try:
                ts_arr = df["timestamp"].values.astype(float)
                closest_i = int(np.argmin(np.abs(ts_arr - float(entry_time_ms))))
                bar_interval_ms = float(np.median(np.diff(ts_arr))) if len(ts_arr) > 1 else 0.0
                actual_diff = abs(ts_arr[closest_i] - float(entry_time_ms))
                if bar_interval_ms > 0 and actual_diff <= bar_interval_ms * 1.5:
                    signal_bar_offset = closest_i - len(df)
            except Exception as exc:
                logger.warning("Failed to resolve entry_time_ms: %s", exc)
        else:
            signal_bar_offset = state_snapshot.get("signal_bar_offset", -2)

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
        demand_supply_zones=positioned_zones,
    )
