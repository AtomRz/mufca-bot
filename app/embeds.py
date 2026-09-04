"""
embeds.py — Discord embed builders.

🆕 Split out of bot.py (which was ~1600 lines, everything in one file —
Discord commands, scanner, embed building, state). A clean code move, logic
unchanged.

Doesn't import anything from bot.py — an independent module, so it's safe to
import from both bot.py (scanner) and discord_commands.py (commands) without
risk of a circular import.
"""

import logging

import discord

import config as _cfg
from volume_indicators import volume_flow_signal_v3, volume_score_for_side
from utils import format_price

logger = logging.getLogger(__name__)


def _flow_label(flow: str) -> str:
    """Translates the internal flow value into a readable Discord label."""
    return {"inflow": "BUY PRESSURE", "outflow": "SELL PRESSURE"}.get(flow, "NEUTRAL")


def build_embed(ticker, tf, signal_type, price, regime, leverage, confidence,
                sl, tp, tp1, risk, stats, tp_desc: str = "", df=None) -> discord.Embed:
    is_long = "BUY" in signal_type or "LONG" in signal_type
    # 🆕 signal_type is always "{TRACK_LABEL} BUY/SELL (...)" (see
    # signals.py's signal_label) — read the track off its actual leading
    # letter instead of ad-hoc substring checks like "Andean" in
    # signal_type, which silently fell through to a generic "⚪" for any
    # track that isn't A or U (as B now is) rather than genuinely detecting it.
    track_letter = signal_type.strip()[:1]
    _TRACK_EMOJI = {"A": "🔵", "U": "🟢", "B": "🟠"}
    _COIN_EMOJI = {
        "BTC": "🟡", "ETH": "🔷", "SOL": "🟢", "XRP": "⚪",
        "DOGE": "🐕", "BNB": "🟨", "ADA": "🔵", "AVAX": "🔺",
    }
    coin_emoji = next((e for sym, e in _COIN_EMOJI.items() if sym in ticker), "🟣")
    track_emoji = _TRACK_EMOJI.get(track_letter, "⚪")
    conf_color = "🟢" if confidence >= 80 else "🟡" if confidence >= 60 else "🔴"
    mode_label = "Spot" if _cfg.MARKET_MODE == "spot" else "Futures"
    ha_label = "HA" if _cfg.UT_HEIKIN_ASHI else "Normal"
    rr = round(abs(tp - price) / max(risk, 1e-8), 2)
    tp1_pct = abs(tp1 - price) / price * 100
    tp2_pct = abs(tp - price) / price * 100

    tp_source = (
        f"📚 Adaptive (last {stats['count']} signals, {_cfg.TP_PERCENTILE*100:.0f}th %ile)"
        if stats["count"] >= 5 else "📐 Fixed R:R = 2.0"
    )

    embed = discord.Embed(
        title=f"🚨 MUFCA v4.0 {coin_emoji} {'📈 LONG' if is_long else '📉 SHORT'}",
        color=discord.Color.green() if is_long else discord.Color.red(),
    )
    embed.add_field(name="📈 Pair", value=f"**{ticker}**", inline=True)
    embed.add_field(name="⏱ TF", value=tf.upper(), inline=True)
    embed.add_field(name=f"{track_emoji} Track", value=signal_type.strip(), inline=True)
    embed.add_field(name="🧬 HTF Bias", value=f"✅ {_cfg.HTF_BIAS.upper()} FRAMA confirmed", inline=True)
    embed.add_field(name="💵 Entry", value=f"${format_price(price)}", inline=True)
    embed.add_field(name="🛑 Stop Loss", value=f"${format_price(sl)}", inline=True)
    embed.add_field(name="🎯 TP1 (50%)", value=f"${format_price(tp1)} (+{tp1_pct:.2f}%)", inline=True)
    embed.add_field(name="🏁 TP2 (100%)", value=f"${format_price(tp)} (+{tp2_pct:.2f}%)", inline=True)
    embed.add_field(name="📊 Risk/Reward", value=f"1:{rr}", inline=True)
    embed.add_field(name="⚙️ Regime", value=regime, inline=True)
    embed.add_field(name="⚠️ Leverage", value=f"x{leverage}", inline=True)
    embed.add_field(name=f"{conf_color} AI Conf", value=f"{confidence}%", inline=True)
    embed.add_field(name="🕯️ UT Bot", value=f"Heikin Ashi: {'✅' if _cfg.UT_HEIKIN_ASHI else '❌'}", inline=True)
    # 🆕 Volume info (display only, not a filter)
    if df is not None:
        try:
            vol_info = volume_flow_signal_v3(df)
            vol_flow = vol_info["flow"]
            vol_emoji = "🟢" if vol_flow == "inflow" else "🔴" if vol_flow == "outflow" else "⚪"
            rel_vol = vol_info["rel_vol"]
            # 🆕 FIX BUG-LO005: removed a duplicate is_long assignment here.
            # The is_long variable is already defined at the top of the function.
            dir_score = volume_score_for_side(vol_info, "long" if is_long else "short")
            lev_adj = "+" if dir_score > 0.3 else "-" if dir_score < -0.3 else "="
            vol_text = f"{_flow_label(vol_flow)} RV:{rel_vol:.1f}x [{lev_adj}lev]"
            embed.add_field(name=f"{vol_emoji} Volume", value=vol_text, inline=True)
        except Exception as e:
            logger.debug(f"Volume info error in build_embed: {e}")

    embed.add_field(name="📚 TP Source", value=tp_source, inline=False)
    if stats["count"] >= 5:
        embed.add_field(name="📈 Signal Stats",
                        value=f"Avg MFE: {stats['avg_mfe']:.2f}% | Best: {stats['best']:.2f}% | Signals: {stats['count']}",
                        inline=False)
    if tp_desc:
        embed.add_field(name="🧠 TP Logic", value=tp_desc, inline=False)
    embed.set_footer(text=f"MUFCA [AtomDC] v4.0 • Gate.io {mode_label} • HTF:{_cfg.HTF_BIAS.upper()} • UT:{ha_label}")
    return embed
