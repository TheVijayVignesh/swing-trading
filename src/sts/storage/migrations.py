"""Idempotent schema upgrades tracked in _schema_migrations(version, applied_at).

init_db() applies the v2 base DDL then runs migrate(); every migration is
tracked so re-opening an upgraded DB is a no-op. Schema v3 additions:

  1. sessions.status CHECK rebuilt to include 'ARCHIVED' (table rebuild that
     preserves all rows and indexes).
  2. scan_funnels table (+ session/ts index): durable funnel history beyond
     the latest-wins SCAN_FUNNEL journal event.
  3. Trade-level columns: orders.symbol / orders.position_id,
     fills.position_id / fills.fee / fills.slippage,
     positions.exit_avg_px / realized_pnl / risk_per_share / r_multiple /
     total_cost (+ indexes). orders.symbol fixes the routes_api._trades join
     (o.symbol) which previously referenced a non-existent column.
  4. sessions.archived_at TS (nullable).
  5. scan_funnels IST-as-UTC repair (see MULTI_SESSION_DIAGNOSTIC_2026-08-26
     finding #4): rows written from a naive-IST funnel.ts stamped as-if-UTC
     are future-dated by exactly +05:30. Confirmed rows (a same-session
     legacy SCAN_FUNNEL journal event exists exactly 05:30 earlier, i.e. an
     identical microsecond fraction) are shifted back to the true UTC
      instant; every change is recorded in scan_funnels_tz_audit for
      reversibility. Future-dated rows without confirming evidence are left
      as-is and audited with method='UNCORRECTED'.

Schema v6 addition:

   6. Residual pass of the same repair for rows written AFTER v5 applied
      (the pre-fix server process kept running and wrote further watchdog
      heartbeats). Identical evidence rule via the shared _repair_funnel_tz
      helper; audit-row existence is the already-handled guard, so rows
      audited by earlier passes are never re-touched and a re-run is a no-op.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA_VERSION = 6

_IST_OFFSET = timedelta(hours=5, minutes=30)

SCAN_FUNNELS_TZ_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS scan_funnels_tz_audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_funnel_id INTEGER,
    old_ts TEXT,
    new_ts TEXT,
    method TEXT,
    recorded_at TEXT
)
"""

SESSIONS_DDL_V3 = """
CREATE TABLE sessions_new(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT CHECK(status IN ('CREATED','RUNNING','PAUSED','STOPPING','STOPPED','ABORTED','ARCHIVED')),
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

SCAN_FUNNELS_DDL = """
CREATE TABLE IF NOT EXISTS scan_funnels(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    ts TS,
    scanned INTEGER,
    eligible INTEGER,
    setups INTEGER,
    ml_passed INTEGER,
    portfolio_ok INTEGER,
    risk_ok INTEGER,
    selected INTEGER,
    top_rejections_json TEXT,
    explanation TEXT
)
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, col_ddl: str) -> None:
    col = col_ddl.split()[0]
    if col not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_ddl}")


def _m1_sessions_archived(conn: sqlite3.Connection) -> None:
    """Rebuild sessions with ARCHIVED in the status CHECK, preserving rows."""
    cols = _columns(conn, "sessions")
    if "archived_at" in cols:
        # already rebuilt by a newer path; nothing to do
        return
    if conn.in_transaction:
        conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(
            SESSIONS_DDL_V3
            + ";INSERT INTO sessions_new SELECT * FROM sessions ORDER BY rowid;"
            + "DROP TABLE sessions;"
            + "ALTER TABLE sessions_new RENAME TO sessions;"
        )
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _m2_scan_funnels(conn: sqlite3.Connection) -> None:
    conn.execute(SCAN_FUNNELS_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scan_funnels_session_ts"
        " ON scan_funnels(session_id, ts)"
    )


def _m3_trade_columns(conn: sqlite3.Connection) -> None:
    _add_column(conn, "orders", "position_id INTEGER")
    _add_column(conn, "orders", "symbol TEXT")
    _add_column(conn, "fills", "position_id INTEGER")
    _add_column(conn, "fills", "fee REAL DEFAULT 0")
    _add_column(conn, "fills", "slippage REAL DEFAULT 0")
    _add_column(conn, "positions", "exit_avg_px REAL")
    _add_column(conn, "positions", "realized_pnl REAL")
    _add_column(conn, "positions", "risk_per_share REAL")
    _add_column(conn, "positions", "r_multiple REAL")
    _add_column(conn, "positions", "total_cost REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_position ON orders(position_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(session_id, symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_position ON fills(position_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_positions_session_status"
        " ON positions(session_id, status)"
    )
    # Backfill orders.symbol from the originating intent where possible so
    # pre-migration rows are queryable by symbol too.
    conn.execute(
        "UPDATE orders SET symbol=(SELECT i.symbol FROM intents i WHERE i.id=orders.intent_id)"
        " WHERE symbol IS NULL AND intent_id IS NOT NULL"
    )


def _m4_sessions_archived_at(conn: sqlite3.Connection) -> None:
    _add_column(conn, "sessions", "archived_at TS")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _repair_funnel_tz(
    conn: sqlite3.Connection,
    method_tag: str = "IST_AS_UTC_CORRECTED",
) -> list[tuple[int, str, str]]:
    """Shared IST-as-UTC repair core behind migrations v5 and v6.

    Considers ONLY scan_funnels rows whose id has no row yet in
    scan_funnels_tz_audit: the audit schema carries no run/version column,
    so audit-row existence is the already-handled marker (this is what keeps
    v6 from re-touching the rows v5 audited, and what makes every pass
    idempotent by construction). A row is corrected ONLY when a same-session
    legacy SCAN_FUNNEL journal event exists whose aware UTC ts differs from
    the funnel ts by exactly +05:30 — an identical microsecond fraction
    proves the naive-IST-as-UTC write (the journal writer used the aware
    path, so its UTC instant is trusted). Only scan_funnels.ts is corrected;
    journal detail_json payloads are left exactly as written (v5 behavior).
    Future-dated rows without confirming evidence are audited UNCORRECTED
    and left untouched. Returns the fixes applied as (id, old_ts, new_ts).
    """
    conn.execute(SCAN_FUNNELS_TZ_AUDIT_DDL)
    audited = {int(r[0]) for r in conn.execute(
        "SELECT scan_funnel_id FROM scan_funnels_tz_audit"
        " WHERE scan_funnel_id IS NOT NULL")}

    events: dict[str, list[datetime]] = {}
    for sid, ts in conn.execute(
            "SELECT session_id, ts FROM session_events WHERE event='SCAN_FUNNEL'"):
        t = _parse_iso(ts)
        if t is not None:
            events.setdefault(str(sid), []).append(t)

    now_utc = datetime.now(timezone.utc)
    fixes: list[tuple[int, str, str]] = []
    uncorrected: list[tuple[int | None, str, str]] = []
    for fid, sid, ts in conn.execute("SELECT id, session_id, ts FROM scan_funnels").fetchall():
        t = _parse_iso(ts)
        if fid in audited or t is None:
            continue
        match = next((e for e in events.get(str(sid), [])
                      if e.tzinfo is not None and t.tzinfo is not None
                      and (t - e) == _IST_OFFSET), None)
        if match is not None:
            new_ts = (t - _IST_OFFSET).astimezone(timezone.utc).isoformat()
            conn.execute("UPDATE scan_funnels SET ts=? WHERE id=?", (new_ts, fid))
            fixes.append((int(fid), ts, new_ts))
        elif t.tzinfo is not None and t.astimezone(timezone.utc) > now_utc:
            # future-dated => provably wrong, but no trusted evidence to correct
            uncorrected.append((int(fid), ts, ts))
    for fid, old, new, method in (
            [(f[0], f[1], f[2], method_tag) for f in fixes]
            + [(u[0], u[1], u[2], "UNCORRECTED") for u in uncorrected]):
        conn.execute(
            "INSERT INTO scan_funnels_tz_audit(scan_funnel_id, old_ts, new_ts,"
            " method, recorded_at) VALUES(?,?,?,?,?)",
            (fid, old, new, method, _now_iso()),
        )
    conn.commit()
    return fixes


def _m5_scan_funnels_tz(conn: sqlite3.Connection) -> None:
    """Repair pre-v5 scan_funnels rows written naive-IST-as-UTC (+05:30 skew).

    Delegates to the shared _repair_funnel_tz core: conservative,
    microsecond-exact journal-event confirmation; ids already present in
    scan_funnels_tz_audit are skipped; future-dated rows without confirming
    evidence are audited UNCORRECTED and left as-is.
    """
    _repair_funnel_tz(conn, "IST_AS_UTC_CORRECTED")


def _m6_scan_funnels_residual_tz(conn: sqlite3.Connection) -> None:
    """Residual repair for rows written AFTER v5 applied (2026-08-26
    07:22:03Z) by the still-running pre-fix server process — watchdog
    scanned=0 heartbeats carrying naive-IST wall time labeled +00:00.

    Identical deterministic evidence rule and audit guard as v5 via
    _repair_funnel_tz: only rows absent from scan_funnels_tz_audit are
    considered (earlier passes' audited ids are never re-touched), so a
    re-run finds zero matches and is a no-op.
    """
    _repair_funnel_tz(conn, "IST_AS_UTC_CORRECTED")


# (version, description, apply fn) — append-only; never reorder or renumber.
MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "sessions status CHECK includes ARCHIVED (table rebuild)", _m1_sessions_archived),
    (2, "scan_funnels table + index", _m2_scan_funnels),
    (3, "trade-level columns on orders/fills/positions + indexes", _m3_trade_columns),
    (4, "sessions.archived_at", _m4_sessions_archived_at),
    (5, "scan_funnels IST-as-UTC repair + scan_funnels_tz_audit", _m5_scan_funnels_tz),
    (6, "scan_funnels residual IST-as-UTC repair (post-v5 stale-process rows)",
     _m6_scan_funnels_residual_tz),
]


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations idempotently; returns versions applied now."""
    if conn.in_transaction:
        conn.commit()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _schema_migrations("
        " version INTEGER PRIMARY KEY, applied_at TS)"
    )
    applied = {int(r[0]) for r in conn.execute("SELECT version FROM _schema_migrations")}
    done: list[int] = []
    for version, _desc, fn in MIGRATIONS:
        if version in applied:
            continue
        fn(conn)
        conn.execute(
            "INSERT INTO _schema_migrations(version, applied_at) VALUES(?,?)",
            (version, _now_iso()),
        )
        conn.commit()
        done.append(version)
    return done
