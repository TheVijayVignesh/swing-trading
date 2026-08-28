import numpy as np
import pandas as pd
import pytest

from sts.features import indicators as ta


def series(vals):
    return pd.Series(vals, dtype=float)


# ---------------------------------------------------------------- fixtures
CLASSIC_RSI_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
    45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
]

# hand-built OHLC table: TRs are 1.0, 1.5, 1.5 -> ATR(3) at idx2 = 4/3
ATR_HIGH = [11.0, 12.0, 13.0]
ATR_LOW = [10.0, 11.0, 12.0]
ATR_CLOSE = [10.5, 11.5, 12.5]


class TestSMA:
    def test_trivial(self):
        s = series([1, 2, 3, 4, 5])
        out = ta.sma(s, 3)
        assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])
        assert out.iloc[2] == pytest.approx(2.0)
        assert out.iloc[3] == pytest.approx(3.0)
        assert out.iloc[4] == pytest.approx(4.0)

    def test_window_one_equals_input(self):
        s = series([7.5, -2.0])
        assert ta.sma(s, 1).tolist() == pytest.approx([7.5, -2.0])

    @pytest.mark.parametrize("bad_n", [0, -1, 6])
    def test_invalid_window_raises(self, bad_n):
        with pytest.raises(ValueError):
            ta.sma(series([1, 2, 3, 4, 5]), bad_n)


class TestEMA:
    def test_hand_computed_seed_and_alpha_half(self):
        # n=3 -> alpha=0.5; seed=mean(1,2,3)=2; then 2+.5*(4-2)=3; 3+.5*(5-3)=4
        out = ta.ema(series([1, 2, 3, 4, 5]), 3)
        assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])
        assert out.iloc[2] == pytest.approx(2.0)
        assert out.iloc[3] == pytest.approx(3.0)
        assert out.iloc[4] == pytest.approx(4.0)

    def test_invalid_window_raises(self):
        with pytest.raises(ValueError):
            ta.ema(series([1, 2]), 3)


class TestRSI:
    def test_classic_wilder_example(self):
        out = ta.rsi(series(CLASSIC_RSI_CLOSES), 14)
        assert np.all(np.isnan(out.iloc[:14]))
        assert out.iloc[14] == pytest.approx(70.46, abs=0.1)

    def test_all_gives_rsi_100(self):
        out = ta.rsi(series(np.arange(1.0, 21.0)), 14)
        assert out.iloc[-1] == pytest.approx(100.0)

    def test_all_losses_rsi_0(self):
        out = ta.rsi(series(np.arange(20.0, 1.0, -1.0)), 14)
        assert out.iloc[-1] == pytest.approx(0.0)

    def test_too_short_raises(self):
        with pytest.raises(ValueError):
            ta.rsi(series(CLASSIC_RSI_CLOSES), 20)


class TestATR:
    def test_hand_built_table(self):
        out = ta.atr(series(ATR_HIGH), series(ATR_LOW), series(ATR_CLOSE), 3)
        assert np.all(np.isnan(out.iloc[:2]))
        assert out.iloc[2] == pytest.approx((1.0 + 1.5 + 1.5) / 3.0, abs=1e-9)

    def test_wilder_recursion_continues(self):
        h = ATR_HIGH + [13.0]
        l = ATR_LOW + [12.0]
        c = ATR_CLOSE + [12.9]
        out = ta.atr(series(h), series(l), series(c), 3)
        prev = (1.0 + 1.5 + 1.5) / 3.0
        tr_last = max(1.0, abs(13.0 - 12.5), abs(12.0 - 12.5))  # = 1.0
        assert out.iloc[3] == pytest.approx((prev * 2 + tr_last) / 3, abs=1e-9)

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            ta.atr(series([1, 2]), series([1, 2]), series([1, 2, 3]), 2)

    def test_too_short_raises(self):
        with pytest.raises(ValueError):
            ta.atr(series(ATR_HIGH), series(ATR_LOW), series(ATR_CLOSE), 4)


class TestRocAndDonchian:
    def test_roc(self):
        out = ta.roc(series([100.0, 110.0, 99.0]), 1)
        assert np.isnan(out.iloc[0])
        assert out.iloc[1] == pytest.approx(10.0)
        assert out.iloc[2] == pytest.approx(-10.0)

    def test_donchian_high(self):
        out = ta.donchian_high(series([5.0, 7.0, 6.0, 8.0]), 3)
        assert out.iloc[2] == pytest.approx(7.0)
        assert out.iloc[3] == pytest.approx(8.0)
        assert np.isnan(out.iloc[1])

    def test_donchian_invalid_window(self):
        with pytest.raises(ValueError):
            ta.donchian_high(series([1.0, 2.0]), 3)


class TestRealizedVol:
    def test_constant_returns_zero_vol(self):
        out = ta.realized_vol_pct(series([0.01] * 30), 20)
        assert out.iloc[-1] == pytest.approx(0.0, abs=1e-12)

    def test_two_point_sample_std(self):
        # window n=2 of returns [a,b]: sample std = |b-a|/sqrt(2); a=.02 b=-.02
        expected = (0.04 / np.sqrt(2.0)) * np.sqrt(252.0) * 100.0
        out = ta.realized_vol_pct(series([0.02, -0.02]), 2)
        assert out.iloc[1] == pytest.approx(expected, rel=1e-9)

    def test_invalid_window(self):
        with pytest.raises(ValueError):
            ta.realized_vol_pct(series([0.01, 0.02]), 5)


class TestSlope:
    def test_perfect_linear(self):
        out = ta.slope(series([0.0, 1.0, 2.0, 3.0]), 3)
        assert out.iloc[2] == pytest.approx(1.0)
        assert out.iloc[3] == pytest.approx(1.0)

    def test_steeper_line(self):
        out = ta.slope(series([0.0, 2.0, 4.0]), 3)
        assert out.iloc[2] == pytest.approx(2.0)

    def test_declining(self):
        out = ta.slope(series([9.0, 6.0, 3.0]), 3)
        assert out.iloc[2] == pytest.approx(-3.0)

    def test_needs_two_points(self):
        with pytest.raises(ValueError):
            ta.slope(series([1.0]), 1)
        with pytest.raises(ValueError):
            ta.slope(series([1.0, 2.0, 3.0]), 4)
