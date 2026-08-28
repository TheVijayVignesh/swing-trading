"""Persistence / restart recovery — the acceptance trio.

(a) create -> drive bars -> decision+order+fill persist -> NEW manager+conn on
    the SAME db file -> recover_on_boot() -> decision replayable (JSON intact),
    order exists, broker equity reconstructed == pre-crash journal equity.
(b) archive survives restart (and default listing excludes it).
(c) funnel history survives restart.

The marking-bar trick: after the entry fill we feed one more bar whose OHLC
all equal the fill price, so unrealized P&L is exactly zero at the last
snapshot; recovery marks positions at avg_entry, making "same equity within
1e-6" a deterministic float identity rather than an approximation.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import zoneinfo

import pandas as pd
import pytest

from sts.config import SessionConfig
from sts.contracts import Bar
from sts.data import calendar as cal
from sts.lab.manager import LabManager
from sts.marketdata.service import MarketDataService
from sts.storage.db import init_db
from sts.storage.repos import (
    ProtectedDelete,
    SessionRepo,
    TradingRepo,
)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
CLOCK_NOW = dt.datetime(2026, 8, 25, 10, 0, tzinfo=IST)
DAY1 = dt.date(2026, 8, 21)


def bts(h: int, m: int) -> dt.datetime:
    return dt.datetime.combine(DAY1, dt.time(h, m))


def bar(sym: str, ts: dt.datetime, o, h, l, c, v=130_000) -> Bar:
    return Bar(symbol=sym, ts=ts, open=o, high=h, low=l, close=c, volume=v, timeframe="5m")


def uptrend_frame(end: dt.date, n: int, base: float, drift: float) -> pd.DataFrame:
    dates, d = [], end - dt.timedelta(days=1)
    while len(dates) < n:
        if d.weekday() < 5 and d not in cal.nse_holidays(d.year):
            dates.append(d)
        d -= dt.timedelta(days=1)
    dates = sorted(dates)
    closes = [base + drift * i for i in range(n)]
    for k in (-4, -3, -2):
        closes[k] -= 50.0
    rows = [{"date": d, "open": c, "high": c + 10.0, "low": c - 10.0,
             "close": c, "volume": 200_000.0, "adjclose": c}
            for d, c in zip(dates, closes)]
    return pd.DataFrame(rows)


class FakePoller:
    def __init__(self) -> None:
        self.bars: dict[str, Bar] = {}

    def poll_once(self):
        return len(self.bars), 0

    def get_bars(self):
        return dict(self.bars)

    def feed(self, b: Bar) -> None:
        self.bars[b.symbol] = b


@pytest.fixture()
def lab_env(tmp_path):
    db_path = str(tmp_path / "journal.db")
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    # only AAA has enough history to be eligible -> deterministic single flow
    uptrend_frame(DAY1, 80, 500.0, 0.6).to_parquet(daily_dir / "AAA.parquet", index=False)
    uptrend_frame(DAY1, 40, 300.0, 0.5).to_parquet(daily_dir / "BBB.parquet", index=False)
    uptrend_frame(DAY1, 40, 300.0, 0.5).to_parquet(daily_dir / "CCC.parquet", index=False)
    idx = uptrend_frame(DAY1, 80, 100.0, 0.5)
    idx.to_parquet(daily_dir / "_NSEI.parquet", index=False)

    poller = FakePoller()
    md = MarketDataService(["AAA", "BBB", "CCC"], poller=poller,
                           clock=lambda: CLOCK_NOW, daily_dir=daily_dir, poll_seconds=60)
    universes = {"LAB1": ["AAA", "BBB", "CCC"]}

    class Env:
        pass

    e = Env()
    e.db_path = db_path
    e.md = md
    e.poller = poller
    e.universes = universes
    e.conn = init_db(db_path)
    e.mgr = LabManager(e.conn, md, universe_resolver=lambda n: universes[n])
    yield e
    try:
        e.conn.close()
    except Exception:  # noqa: BLE001 — already closed by the crash path
        pass


async def _drive(e, bars):
    for b in bars:
        e.poller.feed(b)
        e.md.poll_cycle()
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


async def test_full_restart_recovery_decision_order_equity(lab_env):
    e = lab_env
    cfg = SessionConfig(name="phoenix", capital_initial=60_000, mode="paper",
                        universe="LAB1", strategy_id="pullback-v1",
                        risk_profile="standard", ml_enabled=False,
                        params={"rsi_min": 10, "rsi_max": 100})
    sid = e.mgr.create_session(cfg)
    e.mgr.start(sid)

    entry_bars = [
        bar("AAA", bts(9, 35), 550, 552, 548, 551),
        bar("AAA", bts(9, 40), 551, 554, 550, 553),
        bar("AAA", bts(9, 45), 553, 560, 552, 558, v=180_000),  # trigger touched
        bar("AAA", bts(9, 50), 558, 562, 556, 560),             # BUY fills here
    ]
    await _drive(e, entry_bars)

    repo = TradingRepo(e.conn, sid)
    intents = [i for i in repo.recent_intents(50) if i["decision"] == "ENTER"]
    assert intents, "decision must be journaled"
    orders = e.conn.execute(
        "SELECT id, status, symbol, filled_qty FROM orders WHERE session_id=?",
        (sid,)).fetchall()
    assert any(o["status"] == "FILLED" and o["symbol"] == "AAA" for o in orders)
    fills = e.conn.execute(
        "SELECT f.px, f.qty, f.fee, f.slippage, f.position_id FROM fills f"
        " WHERE f.session_id=?", (sid,)).fetchall()
    assert fills and fills[0]["px"] > 0
    assert fills[0]["fee"] > 0, "per-fill fee column must be populated by the sink"
    fill_px = float(fills[0]["px"])

    # ---- marking bar: OHLC == fill px -> unrealized exactly zero at snapshot
    await _drive(e, [bar("AAA", bts(9, 55), fill_px, fill_px, fill_px, fill_px)])
    snap_equity = float(repo.equity_curve()[-1]["equity"])
    pos_row = repo.open_positions()[0]
    assert pos_row["risk_per_share"] == pytest.approx(
        abs(pos_row["avg_entry"] - pos_row["stop"]), abs=1e-9)

    iid = intents[-1]["id"]

    # ---- HARD RESTART: crash runner, drop everything, reopen the same file
    task = e.mgr.tasks.pop(sid)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    e.mgr.runners.pop(sid, None)
    e.mgr.graphs.pop(sid, None)
    e.mgr.sessions.set_status(sid, "RUNNING")     # journal says RUNNING at boot
    e.conn.close()

    conn2 = init_db(e.db_path)                    # migrations re-run: no-op
    mgr2 = LabManager(conn2, e.md, universe_resolver=lambda n: e.universes[n])
    recovered = mgr2.recover_on_boot()
    assert sid in recovered

    # decision fully replayable from persisted JSON
    row = conn2.execute("SELECT * FROM intents WHERE id=?", (iid,)).fetchone()
    assert row["symbol"] == "AAA" and row["decision"] == "ENTER"
    for col in ("feature_vector_json", "signals_json", "risk_checks_json",
                "portfolio_snapshot_json"):
        payload = json.loads(row[col])
        assert isinstance(payload, (dict, list)), col
    assert json.loads(row["feature_vector_json"]).get("action") == "ENTER"

    # order exists post-restart
    order = conn2.execute(
        "SELECT status, filled_qty FROM orders WHERE session_id=? AND symbol='AAA'",
        (sid,)).fetchone()
    assert order is not None and order["status"] == "FILLED"

    # equity reconstructed to the same value within 1e-6
    graph = mgr2.graphs[sid]
    state = graph.broker.get_account_state(sid)
    assert state.equity == pytest.approx(snap_equity, abs=1e-6)
    assert len(state.positions) == 1

    # position row survived with trade-level columns
    conn2.row_factory = conn2.row_factory or None
    prow = conn2.execute("SELECT risk_per_share, status FROM positions"
                         " WHERE session_id=?", (sid,)).fetchone()
    assert prow["status"] == "OPEN" and prow["risk_per_share"] > 0

    task = mgr2.tasks.pop(sid)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    conn2.close()


async def test_archive_and_restore_survive_restart(lab_env):
    e = lab_env
    cfg = SessionConfig(name="archival", capital_initial=25_000, mode="paper",
                        universe="LAB1")
    sid = e.mgr.create_session(cfg)
    sessions = SessionRepo(e.conn)
    assert sessions.archive(sid) == "ARCHIVED"
    with pytest.raises(Exception):          # double archive illegal
        sessions.archive(sid)

    e.conn.close()                          # ---- restart
    conn2 = init_db(e.db_path)
    s2 = SessionRepo(conn2)
    assert sid not in [s["id"] for s in s2.list_sessions()]
    archived = [s for s in s2.list_sessions(include_archived=True) if s["id"] == sid]
    assert archived and archived[0]["status"] == "ARCHIVED"
    assert archived[0]["archived_at"] is not None

    assert s2.restore(sid) == "STOPPED"     # restore -> STOPPED, restart-safe
    conn2.close()
    conn3 = init_db(e.db_path)
    s3 = SessionRepo(conn3)
    row = s3.get_session(sid)
    assert row["status"] == "STOPPED" and row["archived_at"] is None
    conn3.close()


async def test_hard_delete_guardrails(lab_env):
    e = lab_env
    cfg = SessionConfig(name="deleteme", capital_initial=25_000, mode="paper",
                        universe="LAB1")
    fresh = e.mgr.create_session(cfg)       # CREATED + no intents/orders
    dirty = e.mgr.create_session(cfg)
    TradingRepo(e.conn, dirty).insert_intent({"ts": "t", "decision": "ENTER"})
    sessions = SessionRepo(e.conn)
    with pytest.raises(ProtectedDelete):
        sessions.delete_hard(dirty)
    sessions.delete_hard(fresh)
    assert sessions.get_session(fresh) is None


async def test_funnel_history_survives_restart(lab_env):
    from sts.contracts import ScanFunnel

    e = lab_env
    cfg = SessionConfig(name="funnely", capital_initial=25_000, mode="paper",
                        universe="LAB1")
    sid = e.mgr.create_session(cfg)
    repo = TradingRepo(e.conn, sid)
    f1 = ScanFunnel(ts=dt.datetime(2026, 8, 21, 9, 35, tzinfo=dt.timezone.utc),
                    scanned=200, eligible=40, setups=5, ml_passed=4,
                    portfolio_ok=3, risk_ok=2, selected=2)
    f2 = ScanFunnel(ts=dt.datetime(2026, 8, 21, 9, 40, tzinfo=dt.timezone.utc),
                    scanned=200, eligible=38, setups=4, ml_passed=3,
                    portfolio_ok=2, risk_ok=1, selected=1)
    repo.record_funnel(f1, top_rejections=[{"reason": "NO_SETUP", "count": 160}])
    repo.record_funnel(f2, explanation="diagnostic")

    e.conn.close()                          # ---- restart
    conn2 = init_db(e.db_path)
    r2 = TradingRepo(conn2, sid)
    hist = r2.funnel_history(limit=10)
    assert len(hist) == 2
    assert hist[0]["scanned"] == 200 and hist[0]["selected"] == 2
    assert hist[1]["eligible"] == 38 and hist[1]["explanation"] == "diagnostic"
    assert hist[0]["top_rejections"] == [{"reason": "NO_SETUP", "count": 160}]
    # legacy latest-wins event readers still work
    assert r2.latest_funnel()["selected"] == 1
    assert len(SessionRepo(conn2).funnel_history(sid, limit=10)) == 2
    conn2.close()
