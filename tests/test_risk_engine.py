import math
import random
from dataclasses import dataclass
from datetime import datetime

import pytest

from sts.config import SessionConfig
from sts.contracts import PositionView, PortfolioState, RiskVerdict, Side, TradeIntent
from sts.risk.engine import CHECK_ORDER, evaluate


def make_portfolio(equity=100_000.0, positions=0, total_open_risk=None,
                   gross_exposure=None, hwm=None):
    views = [
        PositionView(symbol=f"P{i}", qty=10, avg_entry=100.0, last_px=105.0,
                     stop_px=95.0, target1_px=None, target2_px=None,
                     trail_px=None, unrealized_pnl=50.0, pnl_pct=0.05,
                     held_days=1, risk_amount=equity * 0.002, t1_done=False)
        for i in range(positions)
    ]
    if total_open_risk is None:
        total_open_risk = sum(p.risk_amount for p in views)
    if gross_exposure is None:
        gross_exposure = sum(p.qty * p.last_px for p in views)
    if hwm is None:
        hwm = equity
    return PortfolioState(
        cash=equity - gross_exposure, invested=gross_exposure, unrealized=0.0,
        realized=0.0, equity=equity, hwm=hwm,
        drawdown_pct=(hwm - equity) / hwm if hwm else 0.0,
        gross_exposure=gross_exposure, total_open_risk=total_open_risk,
        positions=views,
    )


def make_intent(entry=500.0, stop=470.0):
    return TradeIntent(
        session_id="s", ts=datetime(2026, 8, 21, 10, 0), symbol="X",
        side=Side.BUY, order_type="LIMIT", qty=0,
        limit_price=entry, stop_px=stop, correlation_id="c1",
    )


CFG = SessionConfig(name="t", capital_initial=100_000.0)   # standard profile
BASE_VOL = 1_000_000


class TestSizingAndChecks:
    def test_happy_path_approved_with_all_checks_in_order(self):
        v = evaluate(make_intent(), make_portfolio(), CFG, day_pnl=0.0, hwm=100_000.0,
                     avg_daily_volume=BASE_VOL)
        assert v.approved is True
        assert v.rejection_reason == ""
        assert [c.check for c in v.checks] == CHECK_ORDER
        assert all(c.passed for c in v.checks)

    def test_sizing_math(self):
        # equity 100k, risk 1% = 1000; entry 500 stop 470 -> per_share 30 -> qty 33
        v = evaluate(make_intent(500.0, 470.0), make_portfolio(), CFG,
                     0.0, 100_000.0, avg_daily_volume=BASE_VOL)
        sizing = next(c for c in v.checks if c.check == "qty_sizing")
        assert "qty=33" in sizing.observed

    def test_bad_stop_distance_rejected(self):
        v = evaluate(make_intent(500.0, 510.0), make_portfolio(), CFG,
                     0.0, 100_000.0, avg_daily_volume=BASE_VOL)
        assert v.approved is False
        assert v.rejection_reason in ("qty_sizing",)
        qs = next(c for c in v.checks if c.check == "qty_sizing")
        assert qs.passed is False

    def test_min_notional(self):
        # qty=1 (huge per-share risk), notional 500 < 5000
        v = evaluate(make_intent(500.0, 0.0), make_portfolio(), CFG,
                     0.0, 100_000.0, avg_daily_volume=BASE_VOL)
        mn = next(c for c in v.checks if c.check == "min_notional")
        assert mn.passed is False and v.approved is False

    def test_max_positions(self):
        pf = make_portfolio(equity=1_000_000.0, positions=4)
        v = evaluate(make_intent(), pf, SessionConfig(name="t", capital_initial=1_000_000.0),
                     0.0, 1_000_000.0, avg_daily_volume=BASE_VOL)
        mp = next(c for c in v.checks if c.check == "max_positions")
        assert mp.passed is False and v.rejection_reason == "max_positions"

    def test_total_open_risk_cap(self):
        equity = 100_000.0
        pf = make_portfolio(equity=equity, positions=2,
                            total_open_risk=0.015 * equity)
        v = evaluate(make_intent(), pf, CFG, 0.0, equity, avg_daily_volume=BASE_VOL)
        tor = next(c for c in v.checks if c.check == "total_open_risk")
        assert tor.passed is False  # 1.5% + 1% > 2%

    def test_position_cap(self):
        equity = 100_000.0
        # qty 33 * 500 = 16.5k <= 20k ok; force breach with tighter cap via params
        cfg = SessionConfig(name="t", capital_initial=equity, params={"max_position_pct": 0.10})
        v = evaluate(make_intent(), make_portfolio(equity=equity), cfg,
                     0.0, equity, avg_daily_volume=BASE_VOL)
        pc = next(c for c in v.checks if c.check == "position_cap")
        assert pc.passed is False and v.approved is False

    def test_gross_exposure(self):
        equity = 100_000.0
        pf = make_portfolio(equity=equity, gross_exposure=0.75 * equity)
        v = evaluate(make_intent(), pf, CFG, 0.0, equity, avg_daily_volume=BASE_VOL)
        ge = next(c for c in v.checks if c.check == "gross_exposure")
        assert ge.passed is False  # 75k + 16.5k > 80k

    def test_daily_loss_limit(self):
        v = evaluate(make_intent(), make_portfolio(), CFG,
                     day_pnl=-3_500.0, hwm=100_000.0, avg_daily_volume=BASE_VOL)
        dl = next(c for c in v.checks if c.check == "daily_loss_limit")
        assert dl.passed is False and v.approved is False

    def test_drawdown_kill_special_flag(self):
        v = evaluate(make_intent(), make_portfolio(equity=89_000.0), CFG,
                     0.0, hwm=100_000.0, avg_daily_volume=BASE_VOL)
        assert v.approved is False
        assert v.rejection_reason == "DRAWDOWN_KILL"
        dk = next(c for c in v.checks if c.check == "drawdown_kill")
        assert dk.passed is False

    def test_adv_size_and_fail_closed_on_missing_adv(self):
        v_ok = evaluate(make_intent(), make_portfolio(), CFG, 0.0, 100_000.0,
                        avg_daily_volume=BASE_VOL)
        assert v_ok.approved is True
        v_missing = evaluate(make_intent(), make_portfolio(), CFG, 0.0, 100_000.0,
                             avg_daily_volume=None)
        adv = next(c for c in v_missing.checks if c.check == "adv_size")
        assert adv.passed is False and v_missing.approved is False

    def test_ml_cannot_override(self):
        base = make_intent()
        bullish = replace_ml(base, ml_score=0.99)
        bearish = replace_ml(base, ml_score=-1.0)
        none_v = evaluate(base, make_portfolio(), CFG, 0.0, 100_000.0,
                          avg_daily_volume=BASE_VOL)
        bull_v = evaluate(bullish, make_portfolio(), CFG, 0.0, 100_000.0,
                          avg_daily_volume=BASE_VOL)
        bear_v = evaluate(bearish, make_portfolio(), CFG, 0.0, 100_000.0,
                          avg_daily_volume=BASE_VOL)
        assert none_v.approved == bull_v.approved == bear_v.approved
        assert [(c.check, c.observed, c.passed) for c in none_v.checks] == \
               [(c.check, c.observed, c.passed) for c in bull_v.checks] == \
               [(c.check, c.observed, c.passed) for c in bear_v.checks]

    def test_ml_cannot_override_rejection(self):
        pf = make_portfolio(equity=89_000.0)  # drawdown-killed portfolio
        v_low = evaluate(replace_ml(make_intent(), ml_score=0.01), pf, CFG,
                         0.0, 100_000.0, avg_daily_volume=BASE_VOL)
        v_high = evaluate(replace_ml(make_intent(), ml_score=0.9999), pf, CFG,
                          0.0, 100_000.0, avg_daily_volume=BASE_VOL)
        assert v_low.approved is False and v_high.approved is False
        assert v_high.rejection_reason == "DRAWDOWN_KILL"


def replace_ml(intent: TradeIntent, ml_score: float | None) -> TradeIntent:
    import dataclasses
    return dataclasses.replace(intent, ml_score=ml_score)


# ------------------------------------------------------------- property test
EPS = 1e-6


class TestPropertyLoop:
    def test_any_approval_implies_all_numeric_constraints_hold_post_trade(self):
        rng = random.Random(20260824)
        approvals = 0
        for _ in range(500):
            equity = rng.uniform(10_000.0, 500_000.0)
            profile = rng.choice(["small", "standard"])
            cfg = SessionConfig(name="p", capital_initial=equity, risk_profile=profile)
            n_pos = rng.randint(0, 5)
            pos_risk = [rng.uniform(0, 0.006) * equity for _ in range(n_pos)]
            gross = rng.uniform(0, 0.85) * equity
            hwm = equity * rng.uniform(0.85, 1.25)
            day_pnl = rng.uniform(-0.06, 0.03) * equity
            pf = make_portfolio(equity=equity, positions=n_pos,
                                total_open_risk=sum(pos_risk), gross_exposure=gross,
                                hwm=hwm)
            entry = rng.uniform(50.0, 4000.0)
            stop_dist = entry * rng.uniform(-0.02, 0.08)  # sometimes inverted/zero
            intent = make_intent(entry, entry - stop_dist)
            adv = rng.choice([None, 0.0, rng.uniform(500.0, 5_000_000.0)])

            verdict = evaluate(intent, pf, cfg, day_pnl, hwm, avg_daily_volume=adv)

            assert [c.check for c in verdict.checks] == CHECK_ORDER
            assert verdict.approved == all(c.passed for c in verdict.checks)
            if not verdict.approved:
                continue
            approvals += 1

            per_share = entry - (entry - stop_dist)
            risk_amt = cfg.risk_per_trade * equity
            qty = math.floor(risk_amt / per_share)
            notional = qty * entry
            assert qty >= 1
            assert notional >= cfg.min_notional - EPS
            assert len(pf.positions) < cfg.max_positions
            assert sum(pos_risk) + risk_amt <= cfg.max_total_open_risk * equity + EPS
            assert notional <= cfg.max_position_pct * equity + EPS
            assert gross + notional <= cfg.max_gross_exposure * equity + EPS
            assert day_pnl > -cfg.daily_loss_limit * equity
            assert (hwm - equity) / hwm <= cfg.drawdown_kill + EPS
            assert adv is not None and adv > 0
            assert qty <= 0.005 * adv + EPS
        assert approvals > 0  # the loop actually explored approvable states
