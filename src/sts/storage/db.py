"""SQLite WAL runtime journal — schema v2 base DDL + v1–v6 migrations (see
sts.storage.migrations: ARCHIVED status, scan_funnels, trade-level columns,
sessions.archived_at, scan_funnels IST-as-UTC repair, scan_funnels residual
post-v5 repair)."""
from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 6

_DDL = [
    # ---- LIFECYCLE
    """
    CREATE TABLE IF NOT EXISTS sessions(
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        status TEXT CHECK(status IN ('CREATED','RUNNING','PAUSED','STOPPING','STOPPED','ABORTED')),
        terminal_state TEXT,                    -- FLATTENED | HELD | NULL
        mode TEXT,                              -- paper | sandbox | live
        capital_initial REAL,
        config_yaml TEXT,
        config_hash TEXT,                       -- immutable after start
        universe_snapshot_id TEXT,
        strategy_id TEXT,
        strategy_version TEXT,
        param_version TEXT,
        ml_model_id TEXT,                       -- may be NULL => deterministic-only
        costs_version TEXT,
        data_manifest_id TEXT,
        on_stop_policy TEXT,                    -- FLATTEN | HOLD
        created_at TS,
        started_at TS,
        ended_at TS
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT REFERENCES sessions(id),
        ts TS,
        event TEXT,                             -- CREATED/STARTED/PAUSED/RESUMED/
        actor TEXT,                             -- STOP_REQUESTED/STOPPED/FAULTED/RECOVERED
        detail_json TEXT
    )
    """,
    # ---- TRADING (all carry session_id FK, indexed)
    """
    CREATE TABLE IF NOT EXISTS intents(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT REFERENCES sessions(id),
        ts TS,
        symbol TEXT,
        market_state_ref TEXT,
        feature_vector_json TEXT,
        signals_json TEXT,
        ml_score REAL,
        ml_prob REAL,
        risk_checks_json TEXT,
        decision TEXT,
        rejection_reason TEXT,
        portfolio_snapshot_json TEXT,
        versions_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT REFERENCES sessions(id),
        intent_id INTEGER REFERENCES intents(id),
        broker_order_id TEXT,
        replaced_by_id TEXT,
        side TEXT,
        type TEXT,
        qty INTEGER,
        limit_px REAL,
        trigger_px REAL,
        status TEXT,
        filled_qty INTEGER,
        avg_fill_px REAL,
        submitted_at TS,
        updated_at TS,
        idempotency_key TEXT,
        UNIQUE(session_id, idempotency_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fills(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT REFERENCES sessions(id),
        order_id INTEGER REFERENCES orders(id),
        ts TS,
        px REAL,
        qty INTEGER,
        cost_breakdown_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS positions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT REFERENCES sessions(id),
        symbol TEXT,
        qty INTEGER,
        avg_entry REAL,
        stop REAL,
        target2 REAL,
        trail_px REAL,
        opened_at TS,
        closed_at TS,
        status TEXT,
        exit_reason TEXT,
        strategy_version TEXT,
        ml_model_id TEXT,
        param_version TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account_snapshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT REFERENCES sessions(id),
        ts TS,
        cash REAL,
        invested REAL,
        unrealized REAL,
        realized REAL,
        equity REAL,
        hwm REAL,
        drawdown REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics_timeseries(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT REFERENCES sessions(id),
        ts TS,
        metric TEXT,
        value REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS incidents(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT REFERENCES sessions(id),  -- nullable by design
        ts TS,
        severity TEXT,
        kind TEXT,
        detail_json TEXT,
        resolved_at TS
    )
    """,
]

_INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_session_events_session ON session_events(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_intents_session ON intents(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_fills_session ON fills(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_positions_session ON positions(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_account_snapshots_session ON account_snapshots(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_metrics_timeseries_session ON metrics_timeseries(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_incidents_session ON incidents(session_id)",
]


def init_db(path: str) -> sqlite3.Connection:
    """Open (creating if needed) the journal DB with schema v2 applied.

    PRAGMA foreign_keys=ON and WAL mode are set on the returned connection.
    Datetimes must be stored as ISO-8601 UTC strings.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass  # e.g. in-memory DBs keep their own journal mode
    conn.execute("PRAGMA synchronous=NORMAL")
    for ddl in _DDL:
        conn.execute(ddl)
    for ddl in _INDEX_DDL:
        conn.execute(ddl)
    from sts.storage.migrations import migrate
    migrate(conn)
    conn.commit()
    return conn
