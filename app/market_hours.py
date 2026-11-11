"""
NYSE session status for the web dashboard's top-bar "NYSE" lamp.

Purely informational: nothing in the signal / entry / filter logic reads it.
Crypto trades 24/7, but a lot of its flow follows US equity hours, so the lamp
just tells you at a glance which part of the New York day it currently is:

    pre     04:00-09:30 ET   pre-market
    open    09:30-16:00 ET   regular session
    after   16:00-20:00 ET   after-hours
    closed  everything else  overnight, weekends, exchange holidays

The schedule comes from pandas_market_calendars (NYSE calendar), so US
daylight-saving shifts, exchange holidays and early closes are handled for us.
On an early-close day (e.g. the day after Thanksgiving, Christmas Eve) the
regular session ends at 13:00 ET and after-hours at 17:00 ET.

get_nyse_status() never raises: if the calendar cannot be built or queried it
returns {"state": "unknown"} and the lamp goes dim.
"""

import logging
import re
from datetime import date, time, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

NYSE_TZ = "America/New_York"
_REGULAR_CLOSE = time(16, 0)
_RULE_SUFFIX = re.compile(r"\s+(Starting at \d{4}|\d{4}\+|\d{4}\s*-\s*\d{4})$")

# How far ahead the schedule is read. The longest NYSE closure is a long
# weekend (~4 days), so 10 days always contains the next state change.
_LOOKAHEAD_DAYS = 10

# (utc timestamp, state that starts at it, session date, session is an early close)
_Boundary = Tuple[pd.Timestamp, str, date, bool]

_calendar_obj = None
# The boundary list only changes with the New York calendar date, so it is
# computed once per ET day instead of on every request.
_cache: Dict[str, object] = {"et_date": None, "bounds": None}


def _calendar():
    global _calendar_obj
    if _calendar_obj is None:
        import pandas_market_calendars as mcal

        _calendar_obj = mcal.get_calendar("NYSE")
    return _calendar_obj


def _boundaries(et_date: date) -> List[_Boundary]:
    """Every state change from the day before `et_date` to `_LOOKAHEAD_DAYS`
    after it, sorted by time. The extra day before covers the after-midnight
    UTC part of yesterday's after-hours session."""
    if _cache["et_date"] == et_date and _cache["bounds"] is not None:
        return _cache["bounds"]  # type: ignore[return-value]

    sched = _calendar().schedule(
        start_date=et_date - timedelta(days=1),
        end_date=et_date + timedelta(days=_LOOKAHEAD_DAYS),
        start="pre",
        end="post",
    )
    bounds: List[_Boundary] = []
    for session, row in sched.iterrows():
        sdate = pd.Timestamp(session).date()
        early = row["market_close"].tz_convert(NYSE_TZ).time() < _REGULAR_CLOSE
        bounds.append((row["pre"], "pre", sdate, early))
        bounds.append((row["market_open"], "open", sdate, early))
        bounds.append((row["market_close"], "after", sdate, early))
        bounds.append((row["post"], "closed", sdate, early))
    bounds.sort(key=lambda b: b[0])

    _cache["et_date"] = et_date
    _cache["bounds"] = bounds
    return bounds


def _holiday_name(d: date) -> Optional[str]:
    """Name of a regular exchange holiday (None for ad-hoc closures)."""
    try:
        names = _calendar().regular_holidays.holidays(d, d, return_name=True)
        if len(names):
            # the calendar tags some rules with their validity period,
            # e.g. "Good Friday 1908+" or "Juneteenth Starting at 2022"
            return _RULE_SUFFIX.sub("", str(names.iloc[0])).strip()
    except Exception:
        logger.debug("Holiday name lookup failed for %s", d, exc_info=True)
    return None


def get_nyse_status(now=None) -> Dict:
    """Current NYSE state plus when it changes next.

    `now` is for tests: anything pd.Timestamp accepts; naive values are read as UTC.

    Returns:
      state            "pre" | "open" | "after" | "closed" | "unknown"
      now              the instant this answer is for (ISO, UTC)
      next_state       the state that follows, or None
      next_change_at   ISO UTC time of that change, or None
      early_close      the current (or, when closed, the upcoming) session ends early
      closed_reason    when closed: "weekend" | "holiday" | None (plain overnight)
      holiday_name     when closed_reason == "holiday" and the name is known
    """
    try:
        ts = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        et_now = ts.tz_convert(NYSE_TZ)
        bounds = _boundaries(et_now.date())

        past = [b for b in bounds if b[0] <= ts]
        future = [b for b in bounds if b[0] > ts]
        state = past[-1][1] if past else "closed"
        nxt = future[0] if future else None

        # An open/pre/after state belongs to the session that just started it;
        # when closed, the interesting session is the next one.
        if state == "closed":
            early = bool(nxt[3]) if nxt else False
        else:
            early = bool(past[-1][3])

        closed_reason = None
        holiday_name = None
        if state == "closed":
            if et_now.weekday() >= 5:
                closed_reason = "weekend"
            elif et_now.date() not in {b[2] for b in bounds}:
                closed_reason = "holiday"
                holiday_name = _holiday_name(et_now.date())

        return {
            "state": state,
            "now": ts.isoformat(),
            "next_state": nxt[1] if nxt else None,
            "next_change_at": nxt[0].isoformat() if nxt else None,
            "early_close": early,
            "closed_reason": closed_reason,
            "holiday_name": holiday_name,
        }
    except Exception:
        logger.warning("NYSE status could not be computed", exc_info=True)
        return {
            "state": "unknown",
            "now": None,
            "next_state": None,
            "next_change_at": None,
            "early_close": False,
            "closed_reason": None,
            "holiday_name": None,
        }
