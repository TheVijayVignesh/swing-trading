import json
import sqlite3
from datetime import datetime, timezone

import pytest

from sts.config import SessionConfig
from sts.storage.db import init_db
from sts.storage.repos import IsolationError, SessionRepo, TradingRepo


@pytest.fixture()
def conn(tmp_path):
    c = init_db(str(tmp_path / "journal.db"))
    yield c
    c.close()


@pytest.fixture()
def cfg():
    return SessionConfig(name="alpha", capital_initial=25000.0)


def ts():
    return datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ db schema
class TestSchema:
    def test_pragmas(self, tmp_path):
        c = init_db(str(tmp_path / "w.db"))
        try:
            assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            c.close()

    def test_all_tables_exist(self, conn):
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        expected = {"sessions", "session_events", "intents", "orders", "fills",
                    "positions", "account_snapshots", "metrics_timeseries", "incidents"}
        assert expected <= tables

    def test_indexes_on_session_id(self, conn):
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        for t in ("session_events", "intents", "orders", "fills",
                  "positions", "account_snapshots", "metrics_timeseries", "incidents"):
            assert f"idx_{t}_session" in idx

    def test_orders_idempotency_unique_per_session(self, conn):
        srepo = SessionRepo(conn)
        sid_a = srepo.create_session(cfg := SessionConfig(name="a", capital_initial=100.0))
        sid_b = srepo.create_session(SessionConfig(name="b", capital_initial=100.0))
        repo_a = TradingRepo(conn, sid_a)
        repo_b = TradingRepo(conn, sid_b)
        repo_a.insert_order({"intent_id": None, "side": "BUY", "type": "LIMIT",
                             "qty": 1, "limit_px": 10.0, "idempotency_key": "K1"})
        # same key within session A -> IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            repo_a.insert_order({"intent_id": None, "side": "BUY", "type": "LIMIT",
                                 "qty": 1, "limit_px": 10.0, "idempotency_key": "K1"})
        # same key in session B -> fine (uniqueness is per-session)
        repo_b.insert_order({"intent_id": None, "side": "BUY", "type": "LIMIT",
                             "qty": 1, "limit_px": 10.0, "idempotency_key": "K1"})


class TestSessionRepo:
    def test_create_and_lifecycle(self, conn, cfg):
        repo = SessionRepo(conn)
        sid = repo.create_session(cfg, now=ts())
        assert len(sid) == 32
        sess = repo.get_session(sid)
        assert sess["status"] == "CREATED"
        assert sess["config_hash"] and len(sess["config_hash"]) == 64
        assert "name: alpha" in sess["config_yaml"]

        repo.set_status(sid, "RUNNING")
        assert repo.get_status(sid) == "RUNNING"
        repo.set_status(sid, "STOPPED", terminal_state="FLATTENED")
        assert repo.get_status(sid) == "STOPPED"
        assert repo.get_session(sid)["terminal_state"] == "FLATTENED"

        names = [s["id"] for s in repo.list_sessions()]
        assert sid in names

    def test_events_journaled(self, conn, cfg):
        repo = SessionRepo(conn)
        sid = repo.create_session(cfg, now=ts())
        repo.record_event(sid, "STARTED", actor="lab", detail={"ok": True}, ts=ts())
        rows = conn.execute(
            "SELECT event, actor, detail_json FROM session_events WHERE session_id=? ORDER BY id",
            (sid,),
        ).fetchall()
        assert [r["event"] for r in rows] == ["CREATED", "STARTED"]
        assert json.loads(rows[1]["detail_json"]) == {"ok": True}


class TestTradingRepoBasics:
    def _setup(self, conn, cfg):
        sid = SessionRepo(conn).create_session(cfg, now=ts())
        return sid, TradingRepo(conn, sid)

    def test_intent_roundtrip_and_funnel(self, conn, cfg):
        sid, repo = self._setup(conn, cfg)
        iid = repo.insert_intent({"ts": iso(ts()), "symbol": "TCS", "decision": "ENTER"})
        got = repo.get_intent(iid)
        assert got["symbol"] == "TCS" and got["session_id"] == sid
        assert len(repo.recent_intents(5)) == 1

        from sts.contracts import ScanFunnel
        repo.upsert_funnel(ScanFunnel(ts=ts(), scanned=200, eligible=40,
                                      setups=8, ml_passed=5, portfolio_ok=4,
                                      risk_ok=3, selected=2))
        snap = repo.latest_funnel()
        assert snap["scanned"] == 200 and snap["selected"] == 2

    def test_positions_and_equity_curve(self, conn, cfg):
        sid, repo = self._setup(conn, cfg)
        pid = repo.upsert_position("INFY", qty=10, avg_entry=1500.0, stop=1450.0,
                                   target2=1650.0, opened_at=ts())
        assert repo.open_positions()[0]["symbol"] == "INFY"
        repo.upsert_position("INFY", qty=10, avg_entry=1500.0, stop=1460.0)  # update-in-place
        assert len(repo.open_positions()) == 1
        assert repo.open_positions()[0]["stop"] == 1460.0

        for i in range(3):
            repo.record_account_snapshot(
                datetime(2026, 8, 20 + i, 15, 30, tzinfo=timezone.utc),
                cash=1000.0 + i, invested=14000.0, unrealized=50.0 * i,
                realized=0.0, equity=15000.0 + 50.0 * i, hwm=15000.0 + 50.0 * i,
                drawdown=0.0)
        curve = repo.equity_curve(limit=2)
        assert len(curve) == 2
        assert curve[0]["ts"] < curve[-1]["ts"]

        repo.close_position("INFY", exit_reason="TARGET1", closed_at=ts())
        assert repo.open_positions() == []
        closed = repo.trades_closed()
        assert closed[0]["exit_reason"] == "TARGET1"

        repo.record_metric("scan_ms", 123.4)
        repo.record_incident("WARN", "FEED_STALE", detail={"secs": 90})
        assert conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE session_id=?", (sid,)
        ).fetchone()[0] == 1


def iso(dt):
    return dt.isoformat()


# ------------------------------------------------------------------- isolation
class TestIsolation:
    def test_repo_cannot_read_other_sessions_rows(self, conn):
        srepo = SessionRepo(conn)
        sid_a = srepo.create_session(SessionConfig(name="A", capital_initial=100.0), now=ts())
        sid_b = srepo.create_session(SessionConfig(name="B", capital_initial=200.0), now=ts())
        repo_a, repo_b = TradingRepo(conn, sid_a), TradingRepo(conn, sid_b)

        intent_a = repo_a.insert_intent({"ts": iso(ts()), "symbol": "AAA", "decision": "ENTER"})
        intent_b = repo_b.insert_intent({"ts": iso(ts()), "symbol": "BBB", "decision": "ENTER"})

        # reads of the other session's ids must raise
        with pytest.raises(IsolationError):
            repo_a.get_intent(intent_b)
        with pytest.raises(IsolationError):
            repo_b.get_intent(intent_a)

        # queries never leak across sessions
        assert [i["symbol"] for i in repo_a.recent_intents()] == ["AAA"]
        assert [i["symbol"] for i in repo_b.recent_intents()] == ["BBB"]

    def test_repo_cannot_write_via_foreign_ids(self, conn):
        srepo = SessionRepo(conn)
        sid_a = srepo.create_session(SessionConfig(name="A", capital_initial=100.0), now=ts())
        sid_b = srepo.create_session(SessionConfig(name="B", capital_initial=200.0), now=ts())
        repo_a, repo_b = TradingRepo(conn, sid_a), TradingRepo(conn, sid_b)

        intent_b = repo_b.insert_intent({"ts": iso(ts()), "symbol": "BBB", "decision": "ENTER"})
        order_b = repo_b.insert_order({"intent_id": intent_b, "side": "SELL", "type": "LIMIT",
                                       "qty": 1, "limit_px": 9.0, "idempotency_key": "OB1"})
        pos_b = repo_b.upsert_position("BBB", qty=5, avg_entry=10.0, stop=9.0, opened_at=ts())

        # A cannot attach an order to B's intent
        with pytest.raises(IsolationError):
            repo_a.insert_order({"intent_id": intent_b, "side": "BUY", "type": "LIMIT",
                                 "qty": 1, "limit_px": 9.0, "idempotency_key": "hijack"})
        # A cannot update B's order or close B's position by symbol/id
        with pytest.raises(IsolationError):
            repo_a.update_order(order_b, status="CANCELLED")
        with pytest.raises(KeyError):  # B owns the OPEN BBB position; A has none
            repo_a.close_position("BBB", exit_reason="STOP")
        assert repo_b.get_order(order_b)["status"] != "CANCELLED"
        assert len(repo_b.open_positions()) == 1
        assert pos_b > 0

        # A cannot fill into B's order
        with pytest.raises(IsolationError):
            repo_a.insert_fill(order_b, ts=ts(), px=9.0, qty=1)

    def test_mutations_are_scoped(self, conn):
        srepo = SessionRepo(conn)
        sid_a = srepo.create_session(SessionConfig(name="A", capital_initial=100.0), now=ts())
        sid_b = srepo.create_session(SessionConfig(name="B", capital_initial=200.0), now=ts())
        repo_a, repo_b = TradingRepo(conn, sid_a), TradingRepo(conn, sid_b)
        repo_a.insert_intent({"ts": iso(ts()), "symbol": "X", "decision": "ENTER"})
        repo_b.insert_intent({"ts": iso(ts()), "symbol": "Y", "decision": "ENTER"})
        repo_a.update_intent_decision(
            repo_a.recent_intents(1)[0]["id"], "REJECT", rejection_reason="TEST")
        assert repo_b.recent_intents(1)[0]["decision"] == "ENTER"
