"""
ai_filter.py — informational Gemini "second opinion" on a freshly opened signal.

The scanner opens a trade by its own rules (signals.py). Once its notifications
are out, bot.py hands the signal to evaluate() in a background task: a compact
market snapshot plus the trade's own levels go to Gemini, and the answer comes
back as a verdict (approve / neutral / reject), a confidence, a news_risk and a
short reason. The result is stored on the history record (record["ai"]), shown
next to the signal, and aggregated by compute_stats() so the verdict can be
checked against real trade outcomes.

INFORMATIONAL ONLY: nothing in the trading path reads the verdict. evaluate()
never raises — a missing key, a timeout, an HTTP error or an unreadable answer
all come back as a dict with a non-"ok" status and no verdict.

Plain REST through aiohttp (already a dependency), API key in a header — the
key never ends up in a URL or a log line.
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp
import numpy as np
import pandas as pd

import config as _cfg

logger = logging.getLogger(__name__)

_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

VERDICTS = ("approve", "neutral", "reject")
NEWS_RISKS = ("low", "medium", "high")
_MAX_REASON_CHARS = 300
_SNAPSHOT_CANDLES = 12   # last closed candles listed verbatim in the prompt
_MAX_ATTEMPTS = 2                      # one retry on a transient failure (timeout / network / 5xx)
_RETRY_PAUSE_S = 2.0
_RETRY_HTTP = (500, 502, 503, 504)


# =====================================================================
# 📸  MARKET SNAPSHOT
# =====================================================================
def _num(x: float) -> float:
    """Compact float for the prompt: 6 significant figures, no trailing noise."""
    try:
        return float(f"{float(x):.6g}")
    except (TypeError, ValueError):
        return float("nan")


def build_snapshot(df: pd.DataFrame, side: str, entry: float) -> Dict[str, Any]:
    """A small, deterministic digest of the CLOSED bars of `df` (the last row is
    the still-forming bar and is dropped). Everything is computed here instead of
    letting the model do arithmetic on raw candles."""
    if df is None or len(df) < 30:
        return {}
    d = df.iloc[:-1].reset_index(drop=True)
    close, high, low, vol = d["close"], d["high"], d["low"], d["volume"]
    last = float(close.iloc[-1])

    def chg(n: int) -> Optional[float]:
        if len(close) <= n or close.iloc[-1 - n] == 0:
            return None
        return round((last / float(close.iloc[-1 - n]) - 1) * 100, 2)

    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr_pct = float(tr.rolling(14).mean().iloc[-1]) / last * 100 if last else None

    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = None
    if float(loss.iloc[-1]) > 0:
        rsi = round(100 - 100 / (1 + float(gain.iloc[-1]) / float(loss.iloc[-1])), 1)
    elif float(gain.iloc[-1]) > 0:
        rsi = 100.0

    base_vol = float(vol.iloc[-21:-1].mean()) if len(vol) > 21 else 0.0
    rel_vol = round(float(vol.iloc[-1]) / base_vol, 2) if base_vol > 0 else None

    win = d.iloc[-50:]
    hi50, lo50 = float(win["high"].max()), float(win["low"].min())

    def dist(level: float) -> float:
        return round((level / entry - 1) * 100, 2) if entry else 0.0

    candles = [
        [_num(r.open), _num(r.high), _num(r.low), _num(r.close), _num(r.volume)]
        for r in d.iloc[-_SNAPSHOT_CANDLES:].itertuples()
    ]
    return {
        "last_close": _num(last),
        "change_pct": {"1_bar": chg(1), "6_bars": chg(6), "24_bars": chg(24)},
        "atr14_pct": round(atr_pct, 2) if atr_pct is not None and np.isfinite(atr_pct) else None,
        "ema20": _num(ema20), "ema50": _num(ema50),
        "price_vs_ema20": "above" if last > ema20 else "below",
        "price_vs_ema50": "above" if last > ema50 else "below",
        "ema20_vs_ema50": "above" if ema20 > ema50 else "below",
        "rsi14": rsi,
        "rel_volume_last_bar": rel_vol,
        "range_50_bars": {
            "high": _num(hi50), "low": _num(lo50),
            "high_vs_entry_pct": dist(hi50), "low_vs_entry_pct": dist(lo50),
        },
        "last_candles_ohlcv_oldest_first": candles,
    }


# =====================================================================
# 📝  PROMPT
# =====================================================================
_SYSTEM_PROMPT = (
    "You are a risk reviewer for a systematic crypto trading bot. The bot has just opened a trade "
    "by its own rules. Your job is a second opinion on THIS trade, not a price prediction: judge whether "
    "the setup looks sound given the data below, and whether there is any news or event risk for the asset "
    "in the next day or two. Be sceptical and concrete. Use only the supplied data, and (if a search tool is "
    "available) recent news — never invent prices, levels or events. If information is missing or mixed, "
    "answer \"neutral\". Reply with ONE JSON object and nothing else."
)

_OUTPUT_SPEC = (
    'Return exactly this JSON (no markdown, no code fence):\n'
    '{"verdict": "approve" | "neutral" | "reject", "confidence": <integer 0-100>, '
    '"news_risk": "low" | "medium" | "high", "reason": "<max 240 characters, plain English>"}\n'
    "verdict: approve = setup and context support the trade; reject = a concrete reason to expect it to fail "
    "(against a clear move, a major event, levels that make the TP unrealistic); neutral = no strong edge either way.\n"
    "news_risk: risk of an adverse scheduled/unscheduled event for this asset or the whole crypto market within ~48h "
    "(regulation, hacks, listings/delistings, unlocks, macro releases, exchange problems). Use \"low\" if you found nothing."
)


def build_prompt(ctx: Dict[str, Any], snapshot: Dict[str, Any]) -> str:
    sig = {k: v for k, v in ctx.items() if v is not None}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"Current time: {now}\n\n"
        f"SIGNAL (opened by the bot, rule-based):\n{json.dumps(sig, ensure_ascii=False, default=str)}\n\n"
        f"MARKET SNAPSHOT (closed bars of the signal timeframe):\n{json.dumps(snapshot, ensure_ascii=False, default=str)}\n\n"
        f"{_OUTPUT_SPEC}"
    )


# =====================================================================
# 🔎  ANSWER PARSING
# =====================================================================
def _extract_json_object(text: str) -> Optional[dict]:
    """First balanced {...} object in `text` that parses as JSON (the model may
    wrap it in prose or a code fence even when told not to)."""
    if not text:
        return None
    depth, start, in_str, esc = 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except ValueError:
                    start = -1
                    continue
                if isinstance(obj, dict):
                    return obj
    return None


def parse_verdict(text: str) -> Optional[Dict[str, Any]]:
    """Validated {verdict, confidence, news_risk, reason} or None if the answer
    does not carry a usable verdict."""
    obj = _extract_json_object(text)
    if obj is None:
        return None
    verdict = str(obj.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        return None
    try:
        confidence = int(round(float(obj.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0
    news = str(obj.get("news_risk", "")).strip().lower()
    reason = re.sub(r"\s+", " ", str(obj.get("reason", "") or "")).strip()
    return {
        "verdict": verdict,
        "confidence": max(0, min(100, confidence)),
        "news_risk": news if news in NEWS_RISKS else "unknown",
        "reason": reason[:_MAX_REASON_CHARS],
    }


# =====================================================================
# 🌐  GEMINI CALL
# =====================================================================
def _failed(status: str, started: float, **extra) -> Dict[str, Any]:
    out = {"status": status, "model": _cfg.AI_MODEL, "latency_s": round(time.monotonic() - started, 1)}
    out.update(extra)
    return out


async def evaluate(ctx: Dict[str, Any], df: Optional[pd.DataFrame]) -> Dict[str, Any]:
    """Asks Gemini for a verdict on one signal. Never raises.

    ctx — the signal's own facts (pair, timeframe, track, side, entry, sl, tp1,
          tp2, rr, leverage, regime, confidence, ...). Whatever is None is left out.
    df  — OHLCV of the signal timeframe (last row = forming bar); optional.

    Always returns a dict with a `status`: "ok" (then verdict/confidence/news_risk/
    reason are present), or "no_key" / "timeout" / "rate_limited" / "http_error" /
    "bad_response" / "error"."""
    started = time.monotonic()
    key = _cfg.GEMINI_API_KEY
    if not key:
        return _failed("no_key", started)

    model = _cfg.AI_MODEL
    grounded = bool(_cfg.AI_GROUNDING)
    try:
        snapshot = build_snapshot(df, str(ctx.get("side", "")), float(ctx.get("entry") or 0.0))
    except Exception as e:
        logger.warning(f"[AI] snapshot failed for {ctx.get('pair')} {ctx.get('timeframe')}: {e}")
        snapshot = {}

    body: Dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": build_prompt(ctx, snapshot)}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096},
    }
    if grounded:
        # Google documents grounding next to JSON mode only for some models, so with
        # search on the JSON shape is requested in the prompt and parsed defensively.
        body["tools"] = [{"google_search": {}}]
    else:
        body["generationConfig"]["responseMimeType"] = "application/json"

    timeout = aiohttp.ClientTimeout(total=max(5, int(_cfg.AI_TIMEOUT_S)))
    raw = ""
    attempt = 0
    # Google overloads (503) and slow answers are usually brief, so a transient failure
    # (timeout, network error, 5xx) gets ONE more try after a short pause. 429 (quota),
    # 4xx and unreadable answers are not retried — repeating them cannot help.
    while True:
        attempt += 1
        retry = attempt < _MAX_ATTEMPTS
        tag = f"{ctx.get('pair')} {ctx.get('timeframe')}"
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    _API_URL.format(model=model), json=body,
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                ) as resp:
                    raw = await resp.text()
                    http = resp.status
        except TimeoutError:
            logger.warning(f"[AI] Gemini timed out after {_cfg.AI_TIMEOUT_S}s ({tag}, attempt {attempt}/{_MAX_ATTEMPTS})")
            if retry:
                await asyncio.sleep(_RETRY_PAUSE_S)
                continue
            return _failed("timeout", started, attempts=attempt)
        except aiohttp.ClientError as e:
            logger.warning(f"[AI] Gemini request failed: {type(e).__name__}: {e} ({tag}, attempt {attempt}/{_MAX_ATTEMPTS})")
            if retry:
                await asyncio.sleep(_RETRY_PAUSE_S)
                continue
            return _failed("error", started, detail=type(e).__name__, attempts=attempt)
        except Exception as e:
            logger.warning(f"[AI] Gemini call failed: {type(e).__name__}: {e}")
            return _failed("error", started, detail=type(e).__name__, attempts=attempt)

        if http == 429:
            logger.warning("[AI] Gemini rate limit / quota reached")
            return _failed("rate_limited", started, detail="HTTP 429", attempts=attempt)
        if http in _RETRY_HTTP and retry:
            logger.warning(f"[AI] Gemini HTTP {http} ({tag}, attempt {attempt}/{_MAX_ATTEMPTS}) — retrying")
            await asyncio.sleep(_RETRY_PAUSE_S)
            continue
        if http != 200:
            # response bodies of Google API errors carry no secrets; keep the head only
            logger.warning(f"[AI] Gemini HTTP {http}: {raw[:300]}")
            return _failed("http_error", started, detail=f"HTTP {http}", attempts=attempt)
        break

    try:
        data = json.loads(raw)
    except Exception as e:
        logger.warning(f"[AI] Gemini answer is not JSON: {type(e).__name__}")
        return _failed("bad_response", started, detail="not JSON", attempts=attempt)

    try:
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
        parsed = parse_verdict(text)
        if parsed is None:
            block = (data.get("promptFeedback") or {}).get("blockReason") or cand.get("finishReason")
            logger.warning(f"[AI] unusable Gemini answer (finish/block={block}): {text[:200]!r}")
            return _failed("bad_response", started, detail=str(block or "no verdict"))
        sources: List[Dict[str, str]] = []
        for ch in ((cand.get("groundingMetadata") or {}).get("groundingChunks") or [])[:5]:
            web = ch.get("web") or {}
            if web.get("uri"):
                sources.append({"title": str(web.get("title", ""))[:120], "uri": str(web["uri"])})
        parsed.update({
            "status": "ok", "model": model, "grounded": grounded,
            "latency_s": round(time.monotonic() - started, 1), "sources": sources,
            "attempts": attempt, "ts": datetime.now(timezone.utc).isoformat(),
        })
        return parsed
    except Exception as e:
        logger.warning(f"[AI] could not read Gemini answer: {type(e).__name__}: {e}")
        return _failed("bad_response", started, detail=type(e).__name__)


# =====================================================================
# 🩺  CONNECTION TEST
# =====================================================================
_TEST_PROMPT = 'Connectivity check. Reply with ONE JSON object and nothing else: {"ok": true}'

# HTTP status -> (our status, hint shown to the operator)
_TEST_HTTP_HINTS = {
    400: ("bad_request", "Google rejected the request — most often an invalid GEMINI_API_KEY or a model code this API does not accept."),
    401: ("auth_error", "The API key was not accepted — check GEMINI_API_KEY in .env."),
    403: ("auth_error", "Access denied — the key is invalid, restricted, or the API is not enabled for its project."),
    404: ("model_not_found", "Unknown model code — check the Model field against Google's model list."),
    429: ("rate_limited", "Rate limit or quota reached — the key works, but Google is throttling it right now."),
}


def _google_error_text(raw: str) -> str:
    """The human-readable `error.message` of a Google API error body (no secrets in
    it), or the head of the raw body when it is not the usual JSON."""
    try:
        err = (json.loads(raw) or {}).get("error") or {}
        msg = str(err.get("message") or "").strip()
        if msg:
            return re.sub(r"\s+", " ", msg)[:240]
    except ValueError:
        pass
    return re.sub(r"\s+", " ", raw or "").strip()[:240]


async def test_connection() -> Dict[str, Any]:
    """One tiny real request to Gemini with the CURRENT settings (model, timeout,
    grounding on/off), so a pass means the next signal's verdict request can work.
    Never raises, costs a few tokens, and sends no market data.

    Returns {ok, status, message, model, grounded, enabled, latency_s, warnings}.
    status: "ok" / "no_key" / "timeout" / "rate_limited" / "auth_error" / "bad_request" /
    "model_not_found" / "http_error" / "bad_response" / "error"."""
    started = time.monotonic()
    model = _cfg.AI_MODEL
    grounded = bool(_cfg.AI_GROUNDING)
    enabled = bool(_cfg.AI_FILTER_ENABLED)

    def result(ok: bool, status: str, message: str, **extra) -> Dict[str, Any]:
        warnings: List[str] = []
        if ok and not enabled:
            warnings.append("AI verdict is switched OFF — signals are not being reviewed until you enable it.")
        out = {
            "ok": ok, "status": status, "message": message, "model": model, "grounded": grounded,
            "enabled": enabled, "latency_s": round(time.monotonic() - started, 1), "warnings": warnings,
        }
        out.update(extra)
        return out

    key = _cfg.GEMINI_API_KEY
    if not key:
        return result(False, "no_key", "GEMINI_API_KEY is not set — add it to .env and restart the container.")

    body: Dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": _TEST_PROMPT}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 1024},
    }
    if grounded:
        body["tools"] = [{"google_search": {}}]   # same request shape as evaluate()
    else:
        body["generationConfig"]["responseMimeType"] = "application/json"

    timeout = aiohttp.ClientTimeout(total=max(5, int(_cfg.AI_TIMEOUT_S)))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                _API_URL.format(model=model), json=body,
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            ) as resp:
                raw = await resp.text()
                http = resp.status
    except TimeoutError:
        return result(False, "timeout", f"No answer within {_cfg.AI_TIMEOUT_S}s — Google is slow or unreachable from this host. "
                                        "Try a longer timeout (grounded requests are slower).")
    except aiohttp.ClientError as e:
        return result(False, "error", f"Network error: {type(e).__name__} — cannot reach generativelanguage.googleapis.com.")
    except Exception as e:
        logger.warning(f"[AI] test request failed: {type(e).__name__}: {e}")
        return result(False, "error", f"Unexpected error: {type(e).__name__}.")

    if http != 200:
        status, hint = _TEST_HTTP_HINTS.get(http, ("http_error", f"Google answered HTTP {http}."))
        detail = _google_error_text(raw)
        logger.warning(f"[AI] test: Gemini HTTP {http}: {detail}")
        return result(False, status, f"{hint} (HTTP {http}: {detail})" if detail else f"{hint} (HTTP {http})", http=http)

    try:
        data = json.loads(raw)
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
    except Exception as e:
        return result(False, "bad_response", f"HTTP 200 but the answer could not be read: {type(e).__name__}.")

    obj = _extract_json_object(text)
    if obj is None:
        block = (data.get("promptFeedback") or {}).get("blockReason") or cand.get("finishReason")
        return result(False, "bad_response",
                      f"The model answered but not with the JSON the verdict parser needs (finish/block: {block or 'unknown'}). "
                      "Verdicts from this model/setting combination would come back empty.")
    return result(True, "ok", "Connected — the model answered and its reply parses as the verdict parser expects.")


# =====================================================================
# 💡  DISPLAY HELPERS
# =====================================================================
VERDICT_EMOJI = {"approve": "🟢", "neutral": "🟡", "reject": "🔴"}
_NEWS_EMOJI = {"low": "🟢", "medium": "🟡", "high": "🔴"}


def format_for_embed(ai: Dict[str, Any]) -> str:
    """One Discord field value for a verdict dict (status "ok" only)."""
    v = ai.get("verdict", "neutral")
    line = f"{VERDICT_EMOJI.get(v, '⚪')} **{v.upper()}** ({ai.get('confidence', 0)}%)"
    news = ai.get("news_risk", "unknown")
    if news in _NEWS_EMOJI:
        line += f" · news risk {_NEWS_EMOJI[news]} {news}"
    reason = ai.get("reason")
    if reason:
        line += f"\n{reason}"
    return line[:1000]


# =====================================================================
# 📊  STATISTICS — does the verdict actually predict anything?
# =====================================================================
def _bucket() -> Dict[str, Any]:
    return {"n": 0, "wins": 0, "sum_pnl": 0.0}


def _finish(b: Dict[str, Any]) -> Dict[str, Any]:
    n = b["n"]
    return {
        "n": n,
        "win_rate": round(b["wins"] / n, 4) if n else None,
        "avg_pnl_pct": round(b["sum_pnl"] / n, 4) if n else None,
        "sum_pnl_pct": round(b["sum_pnl"], 4),
    }


def compute_stats(history: Optional[Dict] = None, origin: str = "live") -> Dict[str, Any]:
    """Win rate / PnL of CLOSED real trades grouped by the AI verdict (and by
    news_risk), plus a "no verdict" group so the comparison has a baseline.

    A trade counts as a win when its stored moved_pct (the PnL the history uses
    everywhere, partial-TP1 aware) is > 0. `origin` is state's usual record-origin
    filter; the default "live" is the only origin that can carry verdicts, since
    backtest records were never reviewed."""
    import state as _state  # lazy: state imports config, keep this module import-light
    if history is None:
        history = _state.load_signals_history()
    origin = _state.normalize_origin(origin)

    by_verdict = {v: _bucket() for v in VERDICTS}
    by_verdict["none"] = _bucket()
    by_news = {n: _bucket() for n in NEWS_RISKS}
    pending = 0

    for _t, _tf, _side, rec in _state._iter_closed_records(history, origin):
        if rec.get("exit_type") == "cancelled":
            continue  # closed by housekeeping (restart / pair removed), not by the market
        ai = rec.get("ai")
        ok = isinstance(ai, dict) and ai.get("status") == "ok" and ai.get("verdict") in VERDICTS
        try:
            pnl = float(rec.get("moved_pct", 0.0))
        except (TypeError, ValueError):
            continue
        groups = [by_verdict[ai["verdict"] if ok else "none"]]
        if ok and ai.get("news_risk") in NEWS_RISKS:
            groups.append(by_news[ai["news_risk"]])
        for g in groups:
            g["n"] += 1
            g["sum_pnl"] += pnl
            g["wins"] += 1 if pnl > 0 else 0

    # verdicts on trades that are still open — not in the stats yet, shown as a counter
    for _t, tfs in history.items():
        for _tf, sides in tfs.items():
            for _side, records in sides.items():
                for rec in records:
                    if rec.get("exit_type") == "open" and isinstance(rec.get("ai"), dict) and rec["ai"].get("status") == "ok":
                        pending += 1

    return {
        "origin": origin,
        "by_verdict": {k: _finish(v) for k, v in by_verdict.items()},
        "by_news_risk": {k: _finish(v) for k, v in by_news.items()},
        "open_with_verdict": pending,
    }
