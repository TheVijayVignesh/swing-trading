"""Schema migration framework tests: fresh installs, idempotency, legacy-v2
upgrades (rows/indexes preserved), and the v3 column surface."""
from __future__ import annotations

import sqlite3

import pytest

from sts.storage.db import SCHEMA_VERSION, init_db
from sts.storage.migrations import MIGRATIONS, migrate


def _versions(conn: sqlite3.Connection) -> list[int]:
    return [int(r[0]) for r in conn.execute(
        "SELECT version FROM _schema_migrations ORDER BY version")]


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


# ------------------------------------------------------------------ fresh path
def test_fresh_init_applies_all_migrations(tmp_path):
    conn = init_db(str(tmp_path / "j.db"))
    assert _versions(conn) == [1, 2, 3, 4, 5, 6]
    assert SCHEMA_VERSION == len(MIGRATIONS)


def test_archived_status_accepted_after_migration(tmp_path):
    conn = init_db(str(tmp_path / "j.db"))
    conn.execute(
        "INSERT INTO sessions(id, name, status, created_at) VALUES('s1','x','ARCHIVED','t')")
    conn.execute(
        "INSERT INTO sessions(id, name, status, created_at) VALUES('s2','y','RUNNING','t')")
    conn.commit()
    statuses = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM sessions")}
    assert statuses == {"s1": "ARCHIVED", "s2": "RUNNING"}
    # illegal status still rejected by CHECK
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sessions(id, name, status) VALUES('s3','z','BANANA')")


def test_v3_column_surface(tmp_path):
    conn = init_db(str(tmp_path / "j.db"))
    assert "archived_at" in _cols(conn, "sessions")
    assert {"position_id", "symbol"} <= _cols(conn, "orders")
    assert {"position_id", "fee", "slippage"} <= _cols(conn, "fills")
    assert {"exit_avg_px", "realized_pnl", "risk_per_share",
            "r_multiple", "total_cost"} <= _cols(conn, "positions")
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_scan_funnels_session_ts", "idx_orders_position",
            "idx_fills_position", "idx_positions_session_status"} <= idx


def test_fee_slippage_default_zero(tmp_path):
    conn = init_db(str(tmp_path / "j.db"))
    conn.execute("INSERT INTO sessions(id, name, status) VALUES('s','x','CREATED')")
    conn.execute("INSERT INTO orders(session_id, side, type, qty) VALUES('s','BUY','LIMIT',1)")
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    conn.execute("INSERT INTO fills(session_id, order_id, ts, px, qty) VALUES('s',?, 't', 10, 1)", (oid,))
    conn.commit()
    row = conn.execute("SELECT fee, slippage FROM fills").fetchone()
    assert row["fee"] == 0 and row["slippage"] == 0


def test_migrate_idempotent(tmp_path):
    conn = init_db(str(tmp_path / "j.db"))
    assert migrate(conn) == []
    assert _versions(conn) == [1, 2, 3, 4, 5, 6]


# ------------------------------------------------------------- legacy upgrade
LEGACY_V2_SESSIONS = """
CREATE TABLE sessions(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT CHECK(status IN ('CREATED','RUNNING','PAUSED','STOPPING','STOPPED','ABORTED')),
    terminal_state TEXT,
    mode TEXT,
    capital_initial REAL,
    config_yaml TEXT,
    config_hash TEXT,
    universe_snapshot_id TEXT,
    strategy_id TEXT,
    strategy_version TEXT,
    param_version TEXT,
    ml_model_id TEXT,
    costs_version TEXT,
    data_manifest_id TEXT,
    on_stop_policy TEXT,
    created_at TS,
    started_at TS,
    ended_at TS
)
"""


def make_legacy_v2_db(path: str) -> sqlite3.Connection:
    """Hand-build a pre-migration (schema v2) journal with live rows."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(LEGACY_V2_SESSIONS)
    conn.execute("""
        CREATE TABLE session_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT REFERENCES sessions(id),
            ts TS, event TEXT, actor TEXT, detail_json TEXT)""")
    conn.execute("""
        CREATE TABLE intents(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT REFERENCES sessions(id),
            ts TS, symbol TEXT, market_state_ref TEXT, feature_vector_json TEXT,
            signals_json TEXT, ml_score REAL, ml_prob REAL, risk_checks_json TEXT,
            decision TEXT, rejection_reason TEXT, portfolio_snapshot_json TEXT,
            versions_json TEXT)""")
    conn.execute("""
        CREATE TABLE orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT REFERENCES sessions(id),
            intent_id INTEGER REFERENCES intents(id),
            broker_order_id TEXT, replaced_by_id TEXT, side TEXT, type TEXT,
            qty INTEGER, limit_px REAL, trigger_px REAL, status TEXT,
            filled_qty INTEGER, avg_fill_px REAL, submitted_at TS, updated_at TS,
            idempotency_key TEXT, UNIQUE(session_id, idempotency_key))""")
    conn.execute("""
        CREATE TABLE fills(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT REFERENCES sessions(id),
            order_id INTEGER REFERENCES orders(id),
            ts TS, px REAL, qty INTEGER, cost_breakdown_json TEXT)""")
    conn.execute("""
        CREATE TABLE positions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT REFERENCES sessions(id),
            symbol TEXT, qty INTEGER, avg_entry REAL, stop REAL, target2 REAL,
            trail_px REAL, opened_at TS, closed_at TS, status TEXT,
            exit_reason TEXT, strategy_version TEXT, ml_model_id TEXT,
            param_version TEXT)""")
    conn.execute("CREATE INDEX idx_session_events_session ON session_events(session_id)")
    # ---- live rows the upgrade MUST preserve
    conn.execute(
        "INSERT INTO sessions(id, name, status, mode, capital_initial, created_at)"
        " VALUES('legacy1','old-run','STOPPED','paper',50000,'2026-01-05T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO session_events(session_id, ts, event, actor, detail_json)"
        " VALUES('legacy1','2026-01-05T00:00:00+00:00','CREATED','lab','{}')")
    conn.execute(
        "INSERT INTO intents(session_id, ts, symbol, decision, feature_vector_json,"
        " signals_json, risk_checks_json, portfolio_snapshot_json)"
        " VALUES('legacy1','2026-01-05T01:00:00+00:00','RELIANCE','ENTER','{\"k\":1}',"
        "'[]','[]','{}')")
    iid = conn.execute("SELECT id FROM intents").fetchone()[0]
    conn.execute(
        "INSERT INTO orders(session_id, intent_id, side, type, qty, status,"
        " idempotency_key) VALUES('legacy1',?, 'BUY','LIMIT',10,'FILLED','k1')", (iid,))
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    conn.execute(
        "INSERT INTO fills(session_id, order_id, ts, px, qty, cost_breakdown_json)"
        " VALUES('legacy1', ?, '2026-01-05T01:01:00+00:00', 100.0, 10, '{\"total\":1.87}')",
        (oid,))
    conn.execute(
        "INSERT INTO positions(session_id, symbol, qty, avg_entry, stop, status,"
        " opened_at, closed_at) VALUES('legacy1','RELIANCE',10,100.0,97.0,'CLOSED',"
        "'2026-01-05T01:00:00+00:00','2026-01-07T01:00:00+00:00')")
    conn.commit()
    return conn


def test_legacy_v2_upgrade_preserves_everything(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = make_legacy_v2_db(path)
    applied = migrate(conn)
    assert applied == [1, 2, 3, 4, 5, 6]

    # rows preserved verbatim
    sess = dict(conn.execute("SELECT * FROM sessions WHERE id='legacy1'").fetchone())
    assert sess["status"] == "STOPPED" and sess["capital_initial"] == 50000
    assert conn.execute("SELECT COUNT(*) n FROM session_events").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) n FROM fills").fetchone()["n"] == 1
    pos = dict(conn.execute("SELECT * FROM positions").fetchone())
    assert pos["symbol"] == "RELIANCE" and pos["qty"] == 10

    # indexes preserved through the table rebuild
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_session_events_session" in idx

    # child FK references still resolve after rename (insert against new table)
    conn.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO session_events(session_id, ts, event, actor) "
                     "VALUES('nope','t','X','lab')")

    # ARCHIVED now legal; archived_at column present
    conn.execute("UPDATE sessions SET status='ARCHIVED', archived_at='now' WHERE id='legacy1'")
    assert conn.execute("SELECT archived_at FROM sessions").fetchone()["archived_at"] == "now"

    # orders.symbol backfilled from the originating intent
    sym = conn.execute("SELECT symbol FROM orders").fetchone()["symbol"]
    assert sym == "RELIANCE"

    # idempotent re-run
    assert migrate(conn) == []


def test_legacy_upgrade_then_init_db_reopens_cleanly(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = make_legacy_v2_db(path)
    migrate(conn)
    conn.close()
    conn2 = init_db(path)   # full init path on an upgraded file: no-op upgrades
    assert _versions(conn2) == [1, 2, 3, 4, 5, 6]
    assert conn2.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"] == 1
