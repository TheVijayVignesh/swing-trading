"""Lifecycle fixes (audit v2): resume-after-reboot zombie, flatten-timeout
honesty, micro sizing-tier defaulting + envelope math verification.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import zoneinfo

import pandas as pd
import pytest

from sts.config import (MICRO_TIER_CAPITAL_THRESHOLD, RISK_PROFILES, SessionConfig)
from sts.contracts import PortfolioState, TradeIntent, Side, OrderType
from sts.data import calendar as cal
from sts.lab import manager as manager_module
from sts.lab.manager import LabManager, LifecycleError
from sts.marketdata.service import MarketDataService
from sts.risk.engine import evaluate as risk_evaluate
from sts.storage.db import init_db
from sts.storage.repos import TradingRepo

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
CLOCK_NOW = dt.datetime(2026, 8, 25, 10, 0, tzinfo=IST)   # Tuesday, OPEN
DAY1 = dt.date(2026, 8, 21)


def bts(day: dt.date, h: int, m: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(h, m))


def bar(sym: str, ts: dt.datetime, o, h, l, c, v=130_000):
    from sts.contracts import Bar
    return Bar(symbol=sym, ts=ts, open=o, high=h, low=l, close=c, volume=v,
               timeframe="5m")


def weekday_dates(end: dt.date, n: int) -> list[dt.date]:
    out: list[dt.date] = []
    d = end - dt.timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5 and d not in cal.nse_holidays(d.year):
            out.append(d)
        d -= dt.timedelta(days=1)
    return sorted(out)


def uptrend_frame(end: dt.date, n: int, base: float, drift: float) -> pd.DataFrame:
    dates = weekday_dates(end, n)
    closes = [base + drift * i for i in range(n)]
    for k in (-4, -3, -2):
        closes[k] -= 50.0
    return pd.DataFrame([{"date": d, "open": c, "high": c + 10.0, "low": c - 10.0,
                          "close": c, "volume": 200_000.0, "adjclose": c}
                         for d, c in zip(dates, closes)])


class FakePoller:
    def __init__(self) -> None:
        self.bars: dict = {}

    def poll_once(self):
        return len(self.bars), 0

    def get_bars(self):
        return dict(self.bars)

    def feed(self, b) -> None:
        self.bars[b.symbol] = b


@pytest.fixture()
def env(tmp_path):
    conn = init_db(str(tmp_path / "journal.db"))
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    uptrend_frame(DAY1, 40, 300.0, 0.5).to_parquet(daily_dir / "CCC.parquet", index=False)
    for sym in ("AAA", "BBB"):
        uptrend_frame(DAY1, 80, 500.0, 0.6).to_parquet(daily_dir / f"{sym}.parquet",
                                                       index=False)
    idx = uptrend_frame(DAY1, 80, 100.0, 0.5)
    idx.to_parquet(daily_dir / "_NSEI.parquet", index=False)

    poller = FakePoller()
    md = MarketDataService(["AAA", "BBB"], poller=poller, clock=lambda: CLOCK_NOW,
                           daily_dir=daily_dir)
    mgr = LabManager(conn, md, universe_resolver=lambda name: {"LAB1": ["AAA", "BBB", "CCC"]}[name])

    class Env:
        pass
    e = Env()
    e.conn = conn
    e.md = md
    e.poller = poller
    e.mgr = mgr
    yield e
    conn.close()


def make_cfg(e, name: str, **kw) -> SessionConfig:
    # NOTE: risk_profile intentionally omitted from defaults so an explicit
    # choice stays detectable via model_fields_set (micro-tier logic).
    base = dict(name=name, capital_initial=60_000.0, mode="paper", universe="LAB1",
                strategy_id="pullback-v1",
                params={"rsi_min": 10, "rsi_max": 100})
    base.update(kw)
    return SessionConfig(**base)


async def drive(e, bars, settle: float = 0.06):
    for b in bars:
        e.poller.feed(b)
        e.md.poll_cycle()
        await asyncio.sleep(0)
    await asyncio.sleep(settle)


async def kill_runner(e, sid: str) -> None:
    task = e.mgr.tasks.pop(sid)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    e.mgr.runners.pop(sid, None)


# ------------------------------------------------- resume-after-reboot zombie
async def test_resume_respawns_zombie_running_session_and_consumes_bar(env):
    e = env
    sid = e.mgr.create_session(make_cfg(e, "zombie"))
    e.mgr.start(sid)
    await asyncio.sleep(0.02)

    # ---- REBOOT: drop all in-memory runner state; journal still says RUNNING
    await kill_runner(e, sid)
    e.mgr.graphs.pop(sid, None)

    # recovery path rebuilds the graph and spawns a runner...
    assert sid in e.mgr.recover_on_boot()
    assert e.mgr._task_alive(sid)

    # ...but simulate a crash BETWEEN recovery and spawn: RUNNING, no task.
    await kill_runner(e, sid)
    assert not e.mgr._task_alive(sid)

    res = e.mgr.resume(sid)          # previously a silent zombie; now respawns
    assert res == "RUNNING"
    assert e.mgr._task_alive(sid), "resume must spawn a live runner task"

    # let the respawned runner subscribe before feeding
    await asyncio.sleep(0.05)

    # the respawned runner actually consumes a bar
    t = bts(DAY1, 9, 35)
    await drive(e, [bar("AAA", t, 550, 552, 548, 551)])
    snaps = e.conn.execute(
        "SELECT COUNT(*) n FROM account_snapshots WHERE session_id=?", (sid,)
    ).fetchone()["n"]
    assert snaps >= 1, "respawned runner must process bars (snapshot journaled)"

    await e.mgr.stop(sid, policy_override="HOLD")


async def test_resume_paused_session_without_task_also_spawns(env):
    e = env
    sid = e.mgr.create_session(make_cfg(e, "paused-zombie"))
    e.mgr.start(sid)
    await asyncio.sleep(0.02)
    e.mgr.pause(sid)
    await kill_runner(e, sid)        # reboot wiped the paused runner task

    assert e.mgr.resume(sid) == "RUNNING"
    assert e.mgr._task_alive(sid)
    await e.mgr.stop(sid, policy_override="HOLD")


async def test_resume_while_actually_running_still_illegal(env):
    e = env
    sid = e.mgr.create_session(make_cfg(e, "live-running"))
    e.mgr.start(sid)
    await asyncio.sleep(0.02)
    with pytest.raises(LifecycleError):
        e.mgr.resume(sid)
    await e.mgr.stop(sid, policy_override="HOLD")


# ------------------------------------------------- flatten timeout honesty
async def test_flatten_timeout_positions_remain_aborts_not_flattened(env, monkeypatch):
    e = env
    monkeypatch.setattr(manager_module, "FLATTEN_TIMEOUT_S", 0.05)
    sid = e.mgr.create_session(make_cfg(e, "flatten-stuck"))
    e.mgr.start(sid)
    await asyncio.sleep(0.02)

    t = lambda h, m: bts(DAY1, h, m)  # noqa: E731
    aaa = [bar("AAA", t(9, 35), 550, 552, 548, 551),
           bar("AAA", t(9, 40), 551, 554, 550, 553),
           bar("AAA", t(9, 45), 553, 560, 552, 558, v=180_000),
           bar("AAA", t(9, 50), 558, 562, 556, 560)]
    bbb = [bar("BBB", t(9, 35), 550, 552, 548, 551),
           bar("BBB", t(9, 40), 551, 554, 550, 553),
           bar("BBB", t(9, 45), 553, 560, 552, 558, v=180_000),
           bar("BBB", t(9, 50), 558, 562, 556, 560)]
    await drive(e, aaa[:4] + bbb[:4])
    repo = TradingRepo(e.conn, sid)
    assert len(repo.open_positions()) == 1

    stop_task = asyncio.create_task(e.mgr.stop(sid))
    # NO further bars fed: flatten sells never fill -> timeout fires
    res = await asyncio.wait_for(stop_task, timeout=5)
    assert res == "ABORTED"

    row = e.conn.execute("SELECT status, terminal_state FROM sessions WHERE id=?",
                         (sid,)).fetchone()
    assert row["status"] == "ABORTED", "timeout with positions must NOT claim STOPPED"
    assert row["terminal_state"] == "HELD"
    inc = e.conn.execute(
        "SELECT detail_json FROM incidents WHERE session_id=? AND kind='FLATTEN_TIMEOUT_POSITIONS_HELD'",
        (sid,)).fetchall()
    assert inc and json.loads(inc[0]["detail_json"])["open_positions"]
    assert len(repo.open_positions()) == 1   # honestly still held


# ------------------------------------------------- micro tier + envelope math
async def test_micro_profile_auto_selected_below_threshold(env):
    e = env
    assert MICRO_TIER_CAPITAL_THRESHOLD == 30_000.0

    small = make_cfg(e, "tiny", capital_initial=25_000.0)
    assert "risk_profile" not in small.model_fields_set      # user did not choose
    sid_small = e.mgr.create_session(small)
    row = e.mgr.sessions.get_session(sid_small)
    assert "risk_profile: micro" in row["config_yaml"]

    explicit = make_cfg(e, "explicit-small", capital_initial=25_000.0,
                        risk_profile="small")
    sid_explicit = e.mgr.create_session(explicit)
    row = e.mgr.sessions.get_session(sid_explicit)
    assert "risk_profile: small" in row["config_yaml"], \
        "explicit user choice must never be overridden"

    big = make_cfg(e, "big", capital_initial=60_000.0)
    sid_big = e.mgr.create_session(big)
    row = e.mgr.sessions.get_session(sid_big)
    assert "risk_profile: standard" in row["config_yaml"]

    clone_id = e.mgr.clone(sid_big, new_name="clone-tiny",
                           overrides={"capital_initial": 20_000.0})
    row = e.mgr.sessions.get_session(clone_id)
    assert "risk_profile: micro" in row["config_yaml"], \
        "clone without an explicit profile follows the micro default"


def test_micro_envelope_math_real_numbers():
    """Documented envelope: f >= risk/(1.5*cap); f <= risk*E/(1.5*min_notional).

    micro @ E=25k: f in [2.22%, 11.11%]. RELIANCE (f=1.5%) is HONESTLY vetoed;
    a 5%-ATR name fits comfortably."""
    micro = SessionConfig(name="env", capital_initial=25_000.0, mode="paper",
                          risk_profile="micro")
    assert micro.risk_per_trade == 0.02
    assert micro.max_position_pct == 0.60
    assert micro.min_notional == 3000.0
    assert RISK_PROFILES["micro"]["risk_per_trade"] == 0.02

    equity = 25_000.0
    pf = PortfolioState(cash=equity, invested=0.0, unrealized=0.0, realized=0.0,
                        equity=equity, hwm=equity, drawdown_pct=0.0,
                        gross_exposure=0.0, total_open_risk=0.0)
    adv_ok = 1_000_000.0

    # RELIANCE ₹1310, ATR 1.5% => stop distance 1.5 * 19.65 = ₹29.5
    entry, stop = 1310.0, 1310.0 - 29.5
    intent = TradeIntent(session_id="s", ts=dt.datetime(2026, 8, 25, 10, 0),
                         symbol="RELIANCE", side=Side.BUY, order_type=OrderType.LIMIT,
                         qty=int(500 / 29.5), limit_price=entry, stop_px=stop,
                         correlation_id="env-reliance")
    v = risk_evaluate(intent, pf, micro, day_pnl=0.0, hwm=equity,
                      avg_daily_volume=adv_ok)
    assert not v.approved
    assert v.rejection_reason == "position_cap"
    checks = {c.check: c for c in v.checks}
    assert float(checks["position_cap"].observed.split(" vs")[0]) == pytest.approx(
        16 * 1310.0)                       # 20,960 > 15,000 cap -> honest veto

    # volatile name: price 500, atr_frac 5% => stop distance 37.5
    entry2, stop2 = 500.0, 500.0 - 37.5
    qty2 = int(500 / 37.5)                 # floor(500/37.5)=13
    intent2 = TradeIntent(session_id="s", ts=dt.datetime(2026, 8, 25, 10, 0),
                          symbol="HIGHATR", side=Side.BUY, order_type=OrderType.LIMIT,
                          qty=qty2, limit_price=entry2, stop_px=stop2,
                          correlation_id="env-highatr")
    v2 = risk_evaluate(intent2, pf, micro, day_pnl=0.0, hwm=equity,
                       avg_daily_volume=adv_ok)
    sizing_fail = [c.check for c in v2.checks
                   if c.check in ("min_notional", "position_cap") and not c.passed]
    assert sizing_fail == [], "f=5% must fit inside the micro envelope"
    assert 13 * 500.0 <= 0.60 * equity     # 6500 <= 15000 cap
    assert 13 * 500.0 >= 3000.0            # >= min notional

    # envelope bounds exactly as documented in config.py
    lo = 0.02 / (1.5 * 0.60)
    hi = 0.02 * 25_000.0 / (1.5 * 3000.0)
    assert lo == pytest.approx(0.0222, abs=1e-4)     # ~2.22%
    assert hi == pytest.approx(0.1111, abs=1e-4)     # ~11.11%
