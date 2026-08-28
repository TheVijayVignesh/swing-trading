"""H1 / pullback_v1 STRATEGY-PRESERVATION regression pins.

Background: docs/MULTI_SESSION_DIAGNOSTIC_2026-08-26.md measured real rejects
where the volume rule blocks morning breakouts because `today_vol` is the
CUMULATIVE PARTIAL-DAY 5m volume compared against 1.5x SMA20(full prior-day
volume): KOTAKBANK cumulative 7.68M vs required 14.65M (ratio ~0.52x);
SBICARD cumulative 706K vs required 2.53M (ratio ~0.28x), around 09:30 IST.

These tests make accidental drift LOUD:
  1. A hash TRIPWIRE over the three H1 strategy files. This tripwire is
     INTENTIONAL. pullback_v1.py is frozen bit-for-bit; changing any pinned
     file requires explicit human sign-off, and only humans may update the
     pinned hashes below (see docs/MULTI_SESSION_DIAGNOSTIC_2026-08-26.md,
     "Recommended Fix" section -- volume-rule reformulation is a research
     decision, not an infrastructure change).
  2. Behavioral pins proving the cumulative-volume gate (and nothing else)
     rejects a fully-qualified morning breakout, and that once cumulative
     volume crosses the SAME threshold late-day, the candidate IS produced.

Note on diagnostics surface: when the volume rule fails, detect_candidates()
`continue`s before appending a CandidateSignal, so the per-symbol failing
RuleResult list is discarded and NOT reachable from evaluate(). We therefore
mirror the negative-fixture technique of tests/test_strategy_v1.py
(test_volume_failure): assert zero candidates from evaluate(), then pin the
predicate arithmetic directly against the same module constants.

All datetimes are fixed naive IST values inside the 09:30-14:30 trading
window; no network, no wall clock.
"""
import hashlib
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from sts.features import indicators as ta
from sts.strategy.pullback_v1 import DEFAULT_PARAMS, StrategyContext, evaluate

# Reuse the proven fixture-building technique from the golden suite verbatim.
from test_strategy_v1 import apply_pullback_dip, build_daily, golden_closes, golden_index


# ------------------------------------------------------------------ tripwire
# INTENTIONAL HASH TRIPWIRE -- do NOT update these hashes without explicit
# human sign-off. See module docstring.
H1_PINNED_HASHES: dict[str, str] = {
    "src/sts/strategy/pullback_v1.py":
        "2e68e8a0f6df946eaa25357e345e72014d14388af72b740ae52d77af40b51b34",
    "src/sts/strategy/random_k.py":
        "6b05d4ffe8e392118835415205ca1df87eaa0d3014d55859f1689133fcfb6772",
    "src/sts/strategy/registry.py":
        "88eff8020c07f2b11478ab20afe61692dd0d660c3c18cb2b2dad6659c4748cff",
}


def test_h1_strategy_files_hash_tripwire():
    repo_root = Path(__file__).resolve().parents[1]
    mismatches = []
    for rel, expected in H1_PINNED_HASHES.items():
        actual = hashlib.sha256((repo_root / rel).read_bytes()).hexdigest()
        if actual != expected:
            mismatches.append(f"  {rel}\n    expected sha256={expected}\n    actual   sha256={actual}")
    assert not mismatches, (
        "H1 strategy file changed — this requires explicit human sign-off "
        "(volume-rule reformulation is a research decision). "
        "See docs/MULTI_SESSION_DIAGNOSTIC_2026-08-26.md §Recommended Fix.\n"
        + "\n".join(mismatches)
    )


# ------------------------------------------------------- fixture engineering
# Diagnostic-mirroring numbers (docs/MULTI_SESSION_DIAGNOSTIC_2026-08-26.md):
KOTAK_SMA20_VOL = 9_770_000.0          # SMA20(volume) of prior full sessions
KOTAK_MORNING_CUM_VOL = 7_680_000.0    # cumulative 5m volume by ~09:35  (~0.52x of required)
SBICARD_SMA20_VOL = 1_687_000.0        # -> required threshold 2,530,500 (~2.53M)
SBICARD_MORNING_CUM_VOL = 706_000.0    # cumulative 5m volume by ~09:35  (~0.28x of required)
KOTAK_LATE_DAY_CUM_VOL = 15_000_000.0  # >= 1.5 * 9.77M = 14,655,000
SBICARD_LATE_DAY_CUM_VOL = 3_000_000.0  # >= 1.5 * 1.687M = 2,530,500

MORNING_NOW = datetime(2026, 8, 26, 9, 35)    # inside 09:30-14:30 window
LATE_DAY_NOW = datetime(2026, 8, 26, 14, 25)  # also inside window
PREV_DAY = date(2026, 8, 25)


def diag_daily(sma20_vol: float):
    """Daily frame using the golden-suite price shape (trend/pullback/momentum
    all PASS by construction) with flat volume so SMA20(volume)==sma20_vol."""
    return apply_pullback_dip(
        build_daily(golden_closes(80), volumes=[float(sma20_vol)] * 80))


def diag_intraday(prev_high: float, cum_vol: float, bar_start: str) -> pd.DataFrame:
    """Single 09:30-09:35-style 5m bar whose high is ABOVE the prior day's
    high (breakout TRUE) carrying the given cumulative volume."""
    o = prev_high - 0.50
    return pd.DataFrame({
        "ts": pd.date_range(bar_start, periods=1, freq="5min"),
        "o": [o], "h": [prev_high + 0.50],
        "l": [o - 0.05], "c": [prev_high + 0.25],
        "v": [float(cum_vol)],
    })


SYM_SMA20_VOL = {"KOTAKBANK": KOTAK_SMA20_VOL, "SBICARD": SBICARD_SMA20_VOL}


def universe_ctx(cum_vols: dict[str, float], now: datetime) -> StrategyContext:
    bar_start = "2026-08-26 09:30" if now == MORNING_NOW else "2026-08-26 14:20"
    daily = {sym: diag_daily(SYM_SMA20_VOL[sym]) for sym in cum_vols}
    intraday = {
        sym: diag_intraday(float(df["high"].iloc[-1]), cum_vols[sym], bar_start)
        for sym, df in daily.items()
    }
    return StrategyContext(
        daily=daily, intraday=intraday, index_daily=golden_index(),
        vix_now=15.0, now=now, eligible=list(cum_vols),
        prev_day=PREV_DAY, rng_seed=None, params={},
    )


MORNING_TOTALS = {"KOTAKBANK": KOTAK_MORNING_CUM_VOL,
                  "SBICARD": SBICARD_MORNING_CUM_VOL}
LATE_DAY_TOTALS = {"KOTAKBANK": KOTAK_LATE_DAY_CUM_VOL,
                   "SBICARD": SBICARD_LATE_DAY_CUM_VOL}


class TestParamsPinned:
    def test_default_params_volume_values_are_frozen(self):
        assert DEFAULT_PARAMS["vol_multiple"] == 1.5
        assert DEFAULT_PARAMS["vol_sma_n"] == 20


class TestMorningBreakoutRejectedByVolume:
    def test_kotakbank_like_breakout_blocked_only_by_volume(self):
        # Everything except volume passes (golden price shape + true breakout);
        # evaluate() therefore mirrors the negative-fixture technique from
        # tests/test_strategy_v1.py::test_volume_failure.
        out = evaluate(universe_ctx({"KOTAKBANK": KOTAK_MORNING_CUM_VOL}, MORNING_NOW))
        assert out == []

    def test_sbicard_like_breakout_blocked_only_by_volume(self):
        out = evaluate(universe_ctx({"SBICARD": SBICARD_MORNING_CUM_VOL}, MORNING_NOW))
        assert out == []

    def test_full_universe_zero_candidates(self):
        out = evaluate(universe_ctx(MORNING_TOTALS, MORNING_NOW))
        assert out == []
        assert [c.symbol for c in out] == []

    def test_volume_predicate_matches_diagnostic_numbers(self):
        # Direct unit-pin of the predicate block (pullback_v1.py:174-176):
        # today_vol = intraday["v"].sum(); vol_ok = today_vol >= 1.5*SMA20(volume).
        for sym, sma20v, cum in (("KOTAKBANK", KOTAK_SMA20_VOL, KOTAK_MORNING_CUM_VOL),
                                 ("SBICARD", SBICARD_SMA20_VOL, SBICARD_MORNING_CUM_VOL)):
            df = diag_daily(sma20v)
            computed_sma20v = float(ta.sma(df["volume"], DEFAULT_PARAMS["vol_sma_n"]).iloc[-1])
            assert computed_sma20v == pytest.approx(sma20v)
            required = DEFAULT_PARAMS["vol_multiple"] * computed_sma20v
            assert cum < required, f"{sym}: {cum=} unexpectedly >= {required=}"
            ratio = cum / required
            assert 0.0 < ratio < 1.0
            if sym == "KOTAKBANK":
                assert ratio == pytest.approx(0.52, abs=0.01)   # diagnostic: ~0.52x
            else:
                assert ratio == pytest.approx(0.28, abs=0.01)   # diagnostic: ~0.28x
            assert required == pytest.approx(
                14_655_000.0 if sym == "KOTAKBANK" else 2_530_500.0)


class TestLateDayContrast:
    def test_same_setup_passes_once_cumulative_volume_crosses_threshold(self):
        # ONLY the volume input changes vs the morning pin; proves the volume
        # rule (not trend/pullback/rsi/regime/trigger) is what gates entry.
        out = evaluate(universe_ctx(LATE_DAY_TOTALS, LATE_DAY_NOW))
        assert sorted(c.symbol for c in out) == ["KOTAKBANK", "SBICARD"]
        by_sym = {c.symbol: c for c in out}
        for sym, df in (("KOTAKBANK", diag_daily(KOTAK_SMA20_VOL)),
                        ("SBICARD", diag_daily(SBICARD_SMA20_VOL))):
            cand = by_sym[sym]
            expected_trigger = float(df["high"].iloc[-1])   # prior-day high
            assert cand.entry_trigger_price == pytest.approx(expected_trigger)
            assert cand.ts == LATE_DAY_NOW
            rule_ids = [r.rule_id for r in cand.rules]
            for rid in ("regime_index", "regime_vix", "trend", "pullback",
                        "momentum", "volume", "trigger"):
                assert rid in rule_ids, f"{sym}: missing {rid}"
            assert all(r.passed for r in cand.rules)

    def test_volume_threshold_is_inclusive_ge(self):
        # Pins the exact boundary semantics of pullback_v1.py:176 (>=, not >).
        exactly_required = DEFAULT_PARAMS["vol_multiple"] * KOTAK_SMA20_VOL
        assert evaluate(universe_ctx({"KOTAKBANK": exactly_required}, LATE_DAY_NOW))
        assert evaluate(universe_ctx(
            {"KOTAKBANK": exactly_required - 1.0}, LATE_DAY_NOW)) == []
