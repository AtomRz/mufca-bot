"""
Tests for the history-based depth optimizer in app/pullback.py.

pullback.py is pure (no exchange / state / config imports), so these run
without the bot. Run with `python tests/test_pullback_optimizer.py` or pytest.
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import pullback as pb  # noqa: E402

TP1 = 2.0        # % distance of TP1 from the signal price
SL = 1.0         # % distance of the stop
N_BARS = 3       # wait window
HOLD = 20        # hold bars
SPACING = 30     # bars between synthetic signals
E = 100.0        # signal price


def _simulate(fav, adv, cl, depth, keep=False, n_bars=3, long=True, tp1=0.02, sl=0.01):
    pnl, filled = pb._simulate_depths(np.array(fav, float), np.array(adv, float), np.array(cl, float),
                                      n_bars, tp1, sl, np.array([0.0, depth]), long, keep)
    return pnl, filled


def test_winner_with_pullback_fills_and_beats_market():
    fav = [0.005, 0.01, 0.025, 0.03, 0.03, 0.03]
    adv = [-0.004, -0.002, 0.0, 0.0, 0.0, 0.0]
    pnl, filled = _simulate(fav, adv, [0.0] * 6, 0.003)
    assert filled.tolist() == [True, True]
    assert abs(pnl[0] - 2.0) < 1e-9                      # market: TP1 at +2%
    assert abs(pnl[1] - (0.02 + 0.003) / 0.997 * 100) < 1e-9
    assert pnl[1] > pnl[0]


def test_limit_deeper_than_the_dip_expires():
    fav = [0.005, 0.01, 0.025, 0.03, 0.03, 0.03]
    adv = [-0.004, -0.002, 0.0, 0.0, 0.0, 0.0]
    pnl, filled = _simulate(fav, adv, [0.0] * 6, 0.005)
    assert filled.tolist() == [True, False]
    assert pnl[1] == 0.0


def test_move_that_leaves_without_a_fill_is_missed():
    # TP1 is reached on bar 1, the same bar that touches the limit -> missed
    fav = [0.03, 0.03, 0.03, 0.03, 0.03, 0.03]
    adv = [-0.002, 0.0, 0.0, 0.0, 0.0, 0.0]
    pnl, filled = _simulate(fav, adv, [0.0] * 6, 0.002)
    assert filled.tolist() == [True, False]
    assert pnl[1] == 0.0
    assert abs(pnl[0] - 2.0) < 1e-9


def test_loser_fixed_stop_vs_keep_distance():
    fav = [0.0] * 6
    adv = [-0.004, -0.012, -0.02, -0.02, -0.02, -0.02]
    fixed, _ = _simulate(fav, adv, [-0.02] * 6, 0.003, keep=False)
    keep, _ = _simulate(fav, adv, [-0.02] * 6, 0.003, keep=True)
    assert abs(fixed[0] - (-1.0)) < 1e-9                  # market: stopped at -1%
    assert abs(fixed[1] - (-0.01 + 0.003) / 0.997 * 100) < 1e-9
    assert abs(keep[1] - (-0.01 / 0.997 * 100)) < 1e-9    # same risk as at the signal price


def test_fill_bar_that_also_hits_the_stop_is_an_immediate_loss():
    fav = [0.0] * 6
    adv = [-0.02, -0.02, -0.02, -0.02, -0.02, -0.02]
    pnl, filled = _simulate(fav, adv, [-0.02] * 6, 0.003)
    assert filled.tolist() == [True, True]
    assert pnl[1] < 0


def test_stop_wins_a_same_bar_tie_with_tp1():
    fav = [0.03, 0.03, 0.03, 0.03, 0.03, 0.03]
    adv = [-0.02, 0.0, 0.0, 0.0, 0.0, 0.0]
    pnl, _ = _simulate(fav, adv, [0.0] * 6, 0.003)
    assert abs(pnl[0] - (-1.0)) < 1e-9


def test_timeout_is_marked_to_the_last_close():
    fav = [0.001] * 6
    adv = [-0.001] * 6
    cl = [0.0, 0.0, 0.0, 0.0, 0.0, 0.004]
    pnl, _ = _simulate(fav, adv, cl, 0.0005)
    assert abs(pnl[0] - 0.4) < 1e-9


def test_short_is_the_mirror_of_long():
    long_h = np.array([100.5, 102.5, 103.0, 103.0])
    long_l = np.array([99.6, 99.9, 100.0, 100.0])
    long_c = np.array([100.0, 102.0, 103.0, 103.0])
    # the same move seen from the short side: prices mirrored around 100
    short_h, short_l, short_c = 200.0 - long_l, 200.0 - long_h, 200.0 - long_c
    pad = np.full(1, 100.0)
    f_l, a_l, c_l = pb._signed_paths(
        "long", 100.0, np.concatenate((pad, long_h)), np.concatenate((pad, long_l)), np.concatenate((pad, long_c)), 0, 4)
    f_s, a_s, c_s = pb._signed_paths(
        "short", 100.0, np.concatenate((pad, short_h)), np.concatenate((pad, short_l)), np.concatenate((pad, short_c)), 0, 4)
    assert np.allclose(f_l, f_s) and np.allclose(a_l, a_s) and np.allclose(c_l, c_s)


def test_incomplete_or_nan_window_is_skipped():
    highs = np.array([100.0, 101.0, np.nan, 101.0])
    lows = np.array([100.0, 99.0, 99.0, 99.0])
    closes = np.array([100.0, 100.0, 100.0, 100.0])
    assert pb._signed_paths("long", 100.0, highs, lows, closes, 0, 3) is None
    assert pb._signed_paths("long", 100.0, highs, lows, closes, 2, 3) is None  # window runs off the end


# ---------------------------------------------------------------------
# end to end on a synthetic history
# ---------------------------------------------------------------------

def _build(scenarios):
    """df with one signal per scenario, SPACING bars apart. A scenario is
    (dip_pct, outcome): the first bar after the signal dips dip_pct below the
    signal price, then the trade either runs to TP1 ('win') or to the stop
    ('loss')."""
    n = 10 + SPACING * len(scenarios) + 40
    high = np.full(n, E * 1.0005)
    low = np.full(n, E * 0.9995)
    close = np.full(n, E)
    records = []
    for k, (dip, outcome) in enumerate(scenarios):
        j = 10 + SPACING * k
        records.append(j)
        low[j + 1] = E * (1.0 - dip / 100.0)
        if outcome == "win":
            high[j + 2] = E * (1.0 + (TP1 + 0.1) / 100.0)
        else:
            low[j + 2] = E * (1.0 - (SL + 0.5) / 100.0)
            close[j + 2:j + 8] = E * 0.98
    ts = (np.arange(n) * 3_600_000 + 1_700_000_000_000).astype("int64")
    df = pd.DataFrame({"timestamp": ts, "open": close, "high": high, "low": low, "close": close})
    recs = {"long": [{"entry": E, "timestamp": int(ts[j]), "track": "a"} for j in records], "short": []}
    return df, recs


def _depth(df, recs, **kw):
    args = dict(track="a", n_bars=N_BARS, last_idx=len(df) - 2, lookback_signals=60, min_samples=12,
                mult=1.0, sl_dist_pct=SL, max_sl_fraction=0.5, min_depth_pct=0.05,
                pool_fallback=False, depth_mode="optimize", tp1_dist_pct=TP1, hold_bars=HOLD,
                keep_distance=False)
    args.update(kw)
    return pb.compute_pullback_depth(df, recs, **args)


def test_winners_that_always_dip_give_a_depth_inside_the_dip():
    df, recs = _build([(0.30, "win")] * 15)
    info = _depth(df, recs)
    assert info is not None and info["mode"] == "optimize"
    assert 0.05 <= info["depth_pct"] <= 0.30
    assert info["exp_pnl_pct"] > info["market_pnl_pct"]
    assert info["est_fill_rate"] == 1.0
    assert info["n"] == 15


def test_winners_that_never_dip_mean_market_even_though_losers_dip():
    # the case the plain average gets wrong: losers dip hard, winners do not
    scen = [(0.0, "win")] * 9 + [(0.45, "loss")] * 6
    df, recs = _build(scen)
    assert _depth(df, recs, keep_distance=True) is None
    mean_info = _depth(df, recs, depth_mode="mean")
    assert mean_info is not None and mean_info["mode"] == "mean" and mean_info["depth_pct"] > 0


def test_depth_respects_the_stop_cap():
    df, recs = _build([(0.45, "win")] * 15)
    info = _depth(df, recs, max_sl_fraction=0.3)
    assert info is not None and info["depth_pct"] <= 0.3 * SL + 1e-12


def test_too_few_samples_enters_at_market():
    df, recs = _build([(0.30, "win")] * 8)
    assert _depth(df, recs) is None


def test_no_look_ahead_unclosed_horizon_is_excluded():
    df, recs = _build([(0.30, "win")] * 15)
    # pretend "now" is just after the 6th signal: later signals do not exist yet
    last_idx = 10 + SPACING * 5 + N_BARS + HOLD + 1
    info = _depth(df, recs, last_idx=last_idx, min_samples=5)
    assert info is not None and info["n"] <= 6
    assert _depth(df, recs, last_idx=last_idx, min_samples=7) is None


def test_missing_inputs_fall_back_to_the_average():
    df, recs = _build([(0.30, "win")] * 15)
    for kw in ({"tp1_dist_pct": None}, {"hold_bars": 0}, {"tp1_dist_pct": float("nan")}):
        info = _depth(df, recs, **kw)
        assert info is not None and info["mode"] == "mean"


def test_pooling_borrows_other_tracks():
    df, recs = _build([(0.30, "win")] * 15)
    for r in recs["long"]:
        r["track"] = "u"
    assert _depth(df, recs, pool_fallback=False) is None
    pooled = _depth(df, recs, pool_fallback=True)
    assert pooled is not None and pooled["pooled"] is True


def test_default_mode_is_unchanged():
    df, recs = _build([(0.30, "win")] * 15)
    info = pb.compute_pullback_depth(df, recs, "a", N_BARS, len(df) - 2, 60, 12, 1.0, SL, 0.5, 0.05)
    assert info["mode"] == "mean" and abs(info["depth_pct"] - 0.30) < 1e-6


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok    {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
