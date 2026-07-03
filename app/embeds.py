"""
embeds.py — Discord embed builders.

🆕 Вынесено из bot.py (было ~1600 строк, всё в одном файле — Discord-команды,
scanner, embed-билдинг, state). Чистый перенос кода, логика не менялась.

Ничего не импортирует из bot.py — независимый модуль, поэтому его безопасно
импортировать и из bot.py (scanner), и из discord_commands.py (команды) без
риска циклического импорта.
"""

import logging

import discord

import config as _cfg
from volume_indicators import volume_flow_signal_v3, volume_score_for_side

logger = logging.getLogger(__name__)


def _flow_label(flow: str) -> str:
    """Переводит internal flow в читаемый лейбл для Discord."""
    return {"inflow": "BUY PRESSURE", "outflow": "SELL PRESSURE"}.get(flow, "NEUTRAL")


def build_embed(ticker, tf, signal_type, price, regime, leverage, confidence,
                sl, tp, tp1, risk, stats, tp_desc: str = "", df=None) -> discord.Embed:
    is_long = "BUY" in signal_type or "LONG" in signal_type
    is_a_track = "Andean" in signal_type or "A " in signal_type
    is_u_track = "UT Bot" in signal_type or "U " in signal_type
    coin_emoji = "🟡" if "BTC" in ticker else "🔷" if "ETH" in ticker else "🟣"
    track_emoji = "🔵" if is_a_track else "🟢" if is_u_track else "⚪"
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
    embed.add_field(name="💵 Entry", value=f"${round(price, 2):,.2f}", inline=True)
    embed.add_field(name="🛑 Stop Loss", value=f"${round(sl, 2):,.2f}", inline=True)
    embed.add_field(name="🎯 TP1 (50%)", value=f"${round(tp1, 2):,.2f} (+{tp1_pct:.2f}%)", inline=True)
    embed.add_field(name="🏁 TP2 (100%)", value=f"${round(tp, 2):,.2f} (+{tp2_pct:.2f}%)", inline=True)
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
            # 🆕 FIX BUG-LO005: Убрано дублирующее присваивание is_long
            # Переменная is_long уже определена в начале функции
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
