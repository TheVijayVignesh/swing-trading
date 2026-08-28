"""Canonical metrics: unit math + PARITY tests vs the API layer.

Parity strategy (routes_api computes inline; we must NOT edit it): craft a
journal with one account_snapshot per trading day and fully-journaled closed
trades, then call the REAL FastAPI handlers through httpx (as the existing
tests do) and assert API-computed values == sts.metrics.canonical values
within 1e-6.

Known structural divergence (reported, not edited): /api/lab/compare anchors
return/max-dd on the DATE-COALESCED equity curve (last snapshot per day,
first coalesced point as base) while canonical/summary anchor on
capital_initial over the full-resolution curve. With one snapshot per day and
the first snapshot equal to capital_initial the two coincide, which this
fixture guarantees; any other shape would diverge and is documented in
docs/API_CONTRACT.md.
"""
from __future__ import annotations

import datetime as dt
import math

import httpx
import pytest

from sts.api.app import create_app
from sts.config import SessionConfig, to_yaml
from sts.marketdata.service import MarketDataService
from sts.metrics import canonical
from sts.storage.db import init_db

CAP = 100_000.0
SID = "paritysession1"
D0 = dt.date(2026, 8, 17)  # Monday


def _ts(day_offset: int, h: int = 10) -> str:
    d = D0 + dt.timedelta(days=day_offset)
    return dt.datetime.combine(d, dt.time(h, 0)).isoformat() + "+00:00"


# equities: one snapshot per weekday, first == capital_initial
EQS = [100_000.0, 101_000.0, 99_500.0, 100_800.0, 102_000.0]
INVESTED = [0.0, 30_000.0, 40_000.0, 25_000.0, 35_000.0]


def _trade(conn, *, symbol: str, entry_px: float, exit_px: float, qty: int,
           stop: float, day_open: int, day_close: int, buy_fee: float,
           sell_fee: float):
    conn.execute(
        "INSERT INTO positions(session_id, symbol, qty, avg_entry, stop, status,"
        " opened_at, closed_at, exit_reason) VALUES(?,?,?,?,?,'CLOSED',?,?,?)",
        (SID, symbol, qty, entry_px, stop, _ts(day_open, 9), _ts(day_close, 13),
         "TARGET2"))
    oid_b = conn.execute(
        "INSERT INTO orders(session_id, symbol, side, type, qty, status,"
        " submitted_at, idempotency_key) VALUES(?,?,?,?,?,?,?,?) RETURNING id",
        (SID, symbol, "BUY", "LIMIT", qty, "FILLED", _ts(day_open, 11),
         f"{symbol}-b")).fetchone()[0]
    oid_s = conn.execute(
        "INSERT INTO orders(session_id, symbol, side, type, qty, status,"
        " submitted_at, idempotency_key) VALUES(?,?,?,?,?,?,?,?) RETURNING id",
        (SID, symbol, "SELL", "LIMIT", qty, "FILLED", _ts(day_close, 11),
         f"{symbol}-s")).fetchone()[0]
    conn.execute(
        "INSERT INTO fills(session_id, order_id, ts, px, qty, cost_breakdown_json,"
        " fee, slippage) VALUES(?,?,?,?,?,?,?,0.02)",
        (SID, oid_b, _ts(day_open, 12), entry_px, qty,
         f'{{"total": {buy_fee}}}', buy_fee))
    conn.execute(
        "INSERT INTO fills(session_id, order_id, ts, px, qty, cost_breakdown_json,"
        " fee, slippage) VALUES(?,?,?,?,?,?,?,0.03)",
        (SID, oid_s, _ts(day_close, 12), exit_px, qty,
         f'{{"total": {sell_fee}}}', sell_fee))


@pytest.fixture()
def parity_env(tmp_path):
    conn = init_db(str(tmp_path / "j.db"))
    cfg = SessionConfig(name="parity", capital_initial=CAP)
    conn.execute(
        "INSERT INTO sessions(id, name, status, mode, capital_initial, config_yaml,"
        " strategy_id, ml_model_id, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (SID, "parity", "STOPPED", "paper", CAP, to_yaml(cfg), "pullback-v1",
         "deterministic", _ts(-1)))
    hwm = 0.0
    for i, (eq, inv) in enumerate(zip(EQS, INVESTED)):
        hwm = max(hwm, eq)
        unreal = 0.0
        cash = eq - inv - unreal
        dd = (hwm - eq) / hwm * 100 if hwm else 0.0
        conn.execute(
            "INSERT INTO account_snapshots(session_id, ts, cash, invested,"
            " unrealized, realized, equity, hwm, drawdown)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (SID, _ts(i), cash, inv, unreal, 0.0, eq, hwm, dd))
    # T1 winner: 100 @ 100 -> 100 @ 102.50, stop 97
    _trade(conn, symbol="AAA", entry_px=100.0, exit_px=102.5, qty=100, stop=97.0,
           day_open=0, day_close=2, buy_fee=1.87, sell_fee=23.47)
    # T2 loser: 50 @ 200 -> 50 @ 198, stop 195
    _trade(conn, symbol="BBB", entry_px=200.0, exit_px=198.0, qty=50, stop=195.0,
           day_open=1, day_close=4, buy_fee=1.87, sell_fee=23.47)
    conn.commit()

    class Env:
        pass

    e = Env()
    e.conn = conn
    e.tmp_path = tmp_path
    yield e
    conn.close()


def _client(e) -> httpx.AsyncClient:
    md = MarketDataService(["AAA"], poller=None, clock=lambda: dt.datetime.now(dt.timezone.utc),
                           daily_dir=e.tmp_path, poll_seconds=60)

    class Mgr:  # summary needs lab.sessions.list_sessions + lab.runners
        class _S:
            list_sessions = staticmethod(
                lambda: [dict(e.conn.execute("SELECT * FROM sessions").fetchone())])
            get_session = staticmethod(
                lambda sid: e.conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
        sessions = _S()
        runners = {}
        graphs = {}

    app = create_app(Mgr(), md, e.conn, recover_on_startup=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


# ------------------------------------------------------------------ unit math
async def test_metric_math_units(parity_env):
    e = parity_env
    conn, sid = e.conn, SID

    assert canonical.current_equity(conn, sid) == EQS[-1]
    assert canonical.total_return_pct(conn, sid) == round((EQS[-1] / CAP - 1) * 100, 4)
    # max dd: peak 101000, trough 99500 -> 1.4851..%
    assert canonical.max_drawdown_pct(conn, sid) == pytest.approx(
        (101_000 - 99_500) / 101_000 * 100, abs=1e-4)

    # independent sharpe computation on the known daily returns
    rets = [EQS[i + 1] / EQS[i] - 1 for i in range(4)]
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    assert canonical.sharpe(conn, sid) == pytest.approx(mean / sd * math.sqrt(252), abs=1e-4)
    downside = [r for r in rets if r < 0]
    dd = math.sqrt(sum(r ** 2 for r in downside) / len(downside))
    assert canonical.sortino(conn, sid) == pytest.approx(mean / dd * math.sqrt(252), abs=1e-4)

    days = (dt.date(2026, 8, 21) - D0).days
    expected_cagr = ((EQS[-1] / EQS[0]) ** (365.25 / days) - 1) * 100
    assert canonical.cagr_pct(conn, sid) == pytest.approx(expected_cagr, abs=1e-4)

    # trades: T1 pnl = 2.5*100 - 1.87 - 23.47 ; T2 pnl = -2*50 - 1.87 - 23.47
    t1 = 2.5 * 100 - 1.87 - 23.47
    t2 = -2.0 * 50 - 1.87 - 23.47
    trades = canonical.closed_trades(conn, sid)
    assert [round(t["pnl"], 2) for t in trades] == [round(t1, 2), round(t2, 2)]
    assert canonical.win_rate(conn, sid) == 0.5
    gp, gl = t1, abs(t2)
    assert canonical.profit_factor(conn, sid) == round(gp / gl, 4)
    assert canonical.expectancy_r(conn, sid) == pytest.approx(
        sum(t["r_multiple"] for t in trades) / 2, abs=1e-9)
    assert canonical.avg_win(conn, sid) == round(gp, 2)
    assert canonical.avg_loss(conn, sid) == -round(gl, 2)  # negative-signed per API convention
    assert canonical.avg_hold_days(conn, sid) == 2.5  # T1: day0->day2, T2: day1->day4
    # exposure: mean(invested/equity*100); first snapshot invested=0 contributes 0
    expo = sum(inv / eq for inv, eq in zip(INVESTED, EQS)) / len(EQS) * 100
    assert canonical.exposure_pct_avg(conn, sid) == pytest.approx(expo, abs=1e-4)
    assert canonical.turnover(conn, sid) == round(sum(INVESTED) / CAP, 4)
    assert canonical.cost_drag(conn, sid) == round((1.87 + 23.47) * 2 / CAP * 100, 6)


def test_empty_session_metrics_are_none_safe(tmp_path):
    conn = init_db(str(tmp_path / "empty.db"))
    conn.execute("INSERT INTO sessions(id, name, status, capital_initial)"
                 " VALUES('e','empty','CREATED',50000)")
    conn.commit()
    sid = "e"
    assert canonical.current_equity(conn, sid) == 50000
    assert canonical.win_rate(conn, sid) is None
    assert canonical.profit_factor(conn, sid) is None
    assert canonical.expectancy_r(conn, sid) is None
    assert canonical.sharpe(conn, sid) is None
    assert canonical.cagr_pct(conn, sid) is None
    assert canonical.max_drawdown_pct(conn, sid) == 0.0


# ------------------------------------------------------------- API parity
TOL = 1e-6


async def test_parity_summary(parity_env):
    e = parity_env
    async with _client(e) as client:
        body = (await client.get("/api/lab/summary")).json()
    card = next(s for s in body["sessions"] if s["id"] == SID)
    want = canonical.summary_metrics(e.conn, SID)
    for key, expected in want.items():
        assert card[key] == pytest.approx(expected, abs=TOL), key
    assert card["max_dd_pct"] == pytest.approx(
        canonical.max_drawdown_pct(e.conn, SID), abs=TOL)


async def test_parity_compare(parity_env):
    e = parity_env
    async with _client(e) as client:
        body = (await client.get(f"/api/lab/compare?ids={SID}")).json()
    api_m = body["sessions"][0]["metrics"]
    canon_m = canonical.compare_metrics(e.conn, SID)
    # every key the API currently exposes must match canonical EXACTLY
    for key, api_val in api_m.items():
        assert api_val == pytest.approx(canon_m[key], abs=TOL), (
            f"divergence at {key}: api={api_val} canonical={canon_m[key]}")
    # addendum-v2 fields the API still owes (must come from canonical)
    for key in ("sharpe", "sortino", "cagr_pct"):
        assert key in canon_m


async def test_parity_detail_trades_and_curves(parity_env):
    e = parity_env
    async with _client(e) as client:
        d = (await client.get(f"/api/sessions/{SID}")).json()
    # trades parity (rounded to API precision)
    api_trades = d["trades"]
    canon_trades = canonical.closed_trades(e.conn, SID)
    assert len(api_trades) == len(canon_trades) == 2
    for a, c in zip(api_trades, canon_trades):
        assert a["pnl"] == pytest.approx(round(c["pnl"], 2), abs=TOL)
        assert a["r_multiple"] == pytest.approx(round(c["r_multiple"], 3), abs=TOL)
        assert a["hold_days"] == c["hold_days"]
        assert a["costs"] == pytest.approx(round(c["costs"], 2), abs=TOL)
    # equity curve parity
    curve = canonical.equity_curve(e.conn, SID)
    assert [[ts, round(eq, 2)] for ts, eq in curve] == d["equity_curve"]
