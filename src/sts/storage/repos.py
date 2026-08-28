"""Session-scoped repositories. TradingRepo is BOUND to one session_id at
construction; every read re-asserts row ownership and any cross-session access
raises IsolationError (defense in depth on top of session_id-filtered queries).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from sts.config import SessionConfig, content_hash, to_yaml

# Fixed offset: IST has no DST. Runners keep their internal clock in NAIVE IST
# (see sts.lab.runner clock / contracts.Bar ts); the canonical storage form is
# a tz-aware UTC ISO string (docs/API_CONTRACT.md — Timestamp standard).
_IST = timezone(timedelta(hours=5, minutes=30))


class IsolationError(RuntimeError):
    """Raised when a repository is asked to touch another session's rows."""


class ProtectedDelete(IsolationError):
    """Hard delete refused: session has history or is not in CREATED state."""


class IllegalTransitionError(ValueError):
    """Illegal lifecycle transition requested at the storage layer."""


def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def utc_iso(dt_or_none: datetime | None) -> str | None:
    """Canonical persisted form for absolute instants: tz-aware UTC ISO string.

    Naive datetimes are the runners' internal IST clock convention, so the
    true instant is naive − 5:30 in UTC (attach +05:30, convert to UTC).
    Aware inputs pass through and are converted to UTC.
    """
    if dt_or_none is None:
        return None
    dt = dt_or_none if dt_or_none.tzinfo is not None else dt_or_none.replace(tzinfo=_IST)
    return dt.astimezone(timezone.utc).isoformat()


def _now_iso() -> str:
    return iso_utc(datetime.now(timezone.utc))


def _rowdict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


class _TxnDepths(threading.local):
    """Per-thread {id(conn): open-transaction-depth} (see TradingRepo.transaction)."""

    def __init__(self) -> None:
        self.depths: dict[int, int] = {}


_txn_depths = _TxnDepths()


def _txn_active(conn: sqlite3.Connection) -> bool:
    """True when a transaction() is open on this conn in this thread."""
    return _txn_depths.depths.get(id(conn), 0) > 0


def _finish_commit(conn: sqlite3.Connection, requested: bool) -> None:
    """Commit ONLY when requested AND no transaction() is active on this conn.

    Inside an open transaction every repo write auto-defers its commit
    regardless of the passed `commit` kwarg — the outermost transaction()
    exit issues the single COMMIT (or ROLLBACK). This makes mid-chain writes
    (e.g. the broker sink minting order rows inside OrderManager.place_order)
    safe by construction: existing call sites passing commit=True can no
    longer prematurely commit an intent->order chain.
    """
    if requested and not _txn_active(conn):
        conn.commit()


class SessionRepo:
    """Global lifecycle table — the only repo allowed to see all sessions."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create_session(self, cfg: SessionConfig, now: datetime | None = None) -> str:
        sid = uuid.uuid4().hex
        ts = iso_utc(now) if now else _now_iso()
        self.conn.execute(
            """INSERT INTO sessions(id, name, status, terminal_state, mode, capital_initial,
                   config_yaml, config_hash, strategy_id, ml_model_id, on_stop_policy, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sid, cfg.name, "CREATED", None, cfg.mode, cfg.capital_initial,
                to_yaml(cfg), content_hash(cfg), cfg.strategy_id,
                ("deterministic" if not cfg.ml_enabled else "pinned-at-start"),
                cfg.on_stop_policy, ts,
            ),
        )
        self.record_event(sid, "CREATED", actor="lab", detail={"name": cfg.name}, ts=now)
        _finish_commit(self.conn, True)
        return sid

    def record_event(
        self,
        session_id: str,
        event: str,
        actor: str = "system",
        detail: dict | None = None,
        ts: datetime | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO session_events(session_id, ts, event, actor, detail_json) VALUES(?,?,?,?,?)",
            (session_id, iso_utc(ts) if ts else _now_iso(), event, actor,
             json.dumps(detail or {})),
        )
        _finish_commit(self.conn, True)
        return int(cur.lastrowid)

    def get_status(self, session_id: str) -> str | None:
        row = self.conn.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()
        return None if row is None else str(row["status"])

    def set_status(self, session_id: str, status: str, terminal_state: str | None = None) -> None:
        self.conn.execute(
            "UPDATE sessions SET status=?, terminal_state=COALESCE(?, terminal_state) WHERE id=?",
            (status, terminal_state, session_id),
        )
        _finish_commit(self.conn, True)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return _rowdict(row)

    def list_sessions(self, include_archived: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sessions"
        if not include_archived:
            sql += " WHERE status != 'ARCHIVED'"
        rows = self.conn.execute(sql + " ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------- archive / restore / delete
    def _require(self, session_id: str) -> dict[str, Any]:
        row = self.get_session(session_id)
        if row is None:
            raise KeyError(f"unknown session {session_id}")
        return row

    def archive(self, session_id: str, now: datetime | None = None) -> str:
        """Soft-archive: status='ARCHIVED' + archived_at. Only CREATED/PAUSED/
        STOPPED sessions may be archived (RUNNING must be stopped first)."""
        row = self._require(session_id)
        if row["status"] not in ("CREATED", "PAUSED", "STOPPED"):
            raise IllegalTransitionError(
                f"illegal transition ARCHIVE from {row['status']}")
        ts = iso_utc(now) if now else _now_iso()
        self.conn.execute(
            "UPDATE sessions SET status='ARCHIVED', archived_at=? WHERE id=?",
            (ts, session_id),
        )
        self.record_event(session_id, "ARCHIVED", actor="lab",
                          detail={"from_status": row["status"]}, ts=now)
        return "ARCHIVED"

    def restore(self, session_id: str, now: datetime | None = None) -> str:
        """Un-archive: ARCHIVED -> STOPPED (restart via normal start path)."""
        row = self._require(session_id)
        if row["status"] != "ARCHIVED":
            raise IllegalTransitionError(
                f"illegal transition RESTORE from {row['status']}")
        self.conn.execute(
            "UPDATE sessions SET status='STOPPED', archived_at=NULL WHERE id=?",
            (session_id,),
        )
        self.record_event(session_id, "RESTORED", actor="lab", ts=now)
        return "STOPPED"

    def delete_hard(self, session_id: str) -> None:
        """Hard delete — ONLY for never-started sessions (status CREATED with
        zero intents and zero orders). Anything else raises ProtectedDelete."""
        row = self._require(session_id)
        if row["status"] != "CREATED":
            raise ProtectedDelete(
                f"refusing hard delete of session in status {row['status']}; "
                "only never-started (CREATED) sessions are deletable")
        intents = self.conn.execute(
            "SELECT COUNT(*) n FROM intents WHERE session_id=?", (session_id,)
        ).fetchone()["n"]
        orders = self.conn.execute(
            "SELECT COUNT(*) n FROM orders WHERE session_id=?", (session_id,)
        ).fetchone()["n"]
        if intents or orders:
            raise ProtectedDelete(
                f"refusing hard delete: session has {intents} intents / {orders} orders")
        with self.conn:
            self.conn.execute("DELETE FROM session_events WHERE session_id=?", (session_id,))
            self.conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def funnel_history(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Durable scan-funnel history (scan_funnels table, oldest-last)."""
        rows = self.conn.execute(
            "SELECT * FROM scan_funnels WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            for key in ("top_rejections_json",):
                if d.get(key):
                    d["top_rejections"] = json.loads(d.pop(key))
                else:
                    d.pop(key, None)
            out.append(d)
        return out


class TradingRepo:
    """Every method operates ONLY on the bound session's rows."""

    def __init__(self, conn: sqlite3.Connection, session_id: str) -> None:
        self.conn = conn
        self.session_id = session_id

    # ------------------------------------------------------------ internal guards
    @contextmanager
    def transaction(self):
        """Explicit sqlite transaction: COMMIT on clean exit, ROLLBACK on error
        (including hard crashes simulated with BaseExceptions).

        While a transaction() is open on this connection, EVERY repo write on
        the same conn auto-defers its commit (thread-local depth counter set
        in __enter__-equivalent) — even writes passing commit=True (e.g. the
        broker sink's insert_order inside OrderManager.place_order). The
        whole chain (intent insert -> broker order row -> fills -> position
        mirror) therefore persists atomically or not at all.

        Nested transaction() is reference-counted: inner blocks are no-ops;
        exactly one COMMIT fires at outermost clean exit, one ROLLBACK at
        outermost exception.
        """
        key = id(self.conn)
        outermost = _txn_depths.depths.get(key, 0) == 0
        _txn_depths.depths[key] = _txn_depths.depths.get(key, 0) + 1
        try:
            yield self.conn
            if outermost:
                self.conn.commit()
        except BaseException:
            if outermost:
                self.conn.rollback()
            raise
        finally:
            remaining = _txn_depths.depths.get(key, 0) - 1
            if remaining > 0:
                _txn_depths.depths[key] = remaining
            else:
                _txn_depths.depths.pop(key, None)

    def insert_intent(self, fields: dict[str, Any], *, commit: bool = True) -> int:
        f = dict(fields)
        f["session_id"] = self.session_id
        cols = ",".join(f.keys())
        ph = ",".join("?" for _ in f)
        cur = self.conn.execute(f"INSERT INTO intents({cols}) VALUES({ph})", tuple(f.values()))
        _finish_commit(self.conn, commit)
        return int(cur.lastrowid)

    def record_decision(self, intent_row: dict[str, Any],
                        order_row: dict[str, Any] | None) -> tuple[int, int | None]:
        """Atomic intent->order insert (single BEGIN/COMMIT via conn).

        A crash between the two inserts can no longer orphan an intent without
        its order: both rows commit together or neither persists. The runner's
        live placement path achieves the same atomicity with `transaction()`
        around insert_intent(commit=False) + place_order, since the order row
        is minted by the broker sink callback inside OrderManager.place_order.
        """
        with self.transaction() as conn:
            f = dict(intent_row)
            f["session_id"] = self.session_id
            cols = ",".join(f.keys())
            ph = ",".join("?" for _ in f)
            cur = conn.execute(f"INSERT INTO intents({cols}) VALUES({ph})", tuple(f.values()))
            iid = int(cur.lastrowid)
            oid = None
            if order_row is not None:
                fo = dict(order_row)
                fo["session_id"] = self.session_id
                fo["intent_id"] = iid
                ocols = ",".join(fo.keys())
                oph = ",".join("?" for _ in fo)
                curo = conn.execute(f"INSERT INTO orders({ocols}) VALUES({oph})",
                                    tuple(fo.values()))
                oid = int(curo.lastrowid)
        return iid, oid

    def get_intent(self, intent_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
        if row is None:
            raise KeyError(f"intent {intent_id} not found")
        self._check(row)
        return dict(row)

    def recent_intents(self, n: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM intents WHERE session_id=? ORDER BY ts DESC, id DESC LIMIT ?",
            (self.session_id, n),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_intent_decision(self, intent_id: int, decision: str, rejection_reason: str = "",
                               risk_checks_json: str = "[]", *, commit: bool = True) -> None:
        self.get_intent(intent_id)  # raises IsolationError if not ours
        self.conn.execute(
            "UPDATE intents SET decision=?, rejection_reason=?, risk_checks_json=? WHERE id=?",
            (decision, rejection_reason, risk_checks_json, intent_id),
        )
        _finish_commit(self.conn, commit)

    # ------------------------------------------------------------------- orders
    def insert_order(self, fields: dict[str, Any], *, commit: bool = True) -> int:
        f = dict(fields)
        f["session_id"] = self.session_id
        if "intent_id" in f and f["intent_id"] is not None:
            self.get_intent(int(f["intent_id"]))  # isolation check
        cols = ",".join(f.keys())
        ph = ",".join("?" for _ in f)
        cur = self.conn.execute(f"INSERT INTO orders({cols}) VALUES({ph})", tuple(f.values()))
        _finish_commit(self.conn, commit)
        return int(cur.lastrowid)

    def get_order(self, order_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise KeyError(f"order {order_id} not found")
        self._check(row)
        return dict(row)

    def update_order(self, order_id: int, *, commit: bool = True, **fields: Any) -> None:
        self.get_order(order_id)  # isolation check
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE orders SET {sets}, updated_at=? WHERE id=?",
            (*fields.values(), _now_iso(), order_id),
        )
        _finish_commit(self.conn, commit)

    # -------------------------------------------------------------------- fills
    @staticmethod
    def split_costs(cost_breakdown: dict | None) -> tuple[float, float]:
        """(fee, slippage) from a broker cost breakdown. `slippage` is an
        explicit component when present; every other component (incl. total
        minus slippage) is fee. Unknown/empty breakdowns -> (0.0, 0.0)."""
        b = cost_breakdown or {}
        slip = float(b.get("slippage", 0.0) or 0.0)
        fee = sum(float(v) for k, v in b.items()
                  if k not in ("total", "slippage") and isinstance(v, (int, float)))
        return fee, slip

    def insert_fill(self, order_id: int, ts: datetime, px: float, qty: int,
                    cost_breakdown: dict | None = None,
                    position_id: int | None = None, *,
                    commit: bool = True) -> int:
        self.get_order(order_id)  # isolation check
        fee, slip = self.split_costs(cost_breakdown)
        cur = self.conn.execute(
            "INSERT INTO fills(session_id, order_id, ts, px, qty, cost_breakdown_json,"
            " position_id, fee, slippage) VALUES(?,?,?,?,?,?,?,?,?)",
            (self.session_id, order_id, iso_utc(ts), px, qty,
             json.dumps(cost_breakdown or {}), position_id, fee, slip),
        )
        _finish_commit(self.conn, commit)
        return int(cur.lastrowid)

    def tag_position(self, *, fill_ids: list[int] | None = None,
                     order_ids: list[int] | None = None,
                     position_id: int | None = None,
                     commit: bool = True) -> None:
        """Backfill position_id on fills/orders journaled before the positions
        row existed (fills land mid-bar; upsert_position runs at bar close)."""
        for fid in fill_ids or []:
            self.conn.execute("UPDATE fills SET position_id=? WHERE id=? AND session_id=?",
                              (position_id, fid, self.session_id))
        for oid in order_ids or []:
            self.conn.execute("UPDATE orders SET position_id=? WHERE id=? AND session_id=?",
                              (position_id, oid, self.session_id))
        _finish_commit(self.conn, commit)

    # ---------------------------------------------------------------- positions
    def upsert_position(self, symbol: str, qty: int, avg_entry: float, stop: float,
                        target2: float | None = None, trail_px: float | None = None,
                        opened_at: datetime | None = None,
                        strategy_version: str = "", ml_model_id: str | None = None,
                        param_version: str = "", *, commit: bool = True) -> int:
        row = self.conn.execute(
            "SELECT id FROM positions WHERE session_id=? AND symbol=? AND status='OPEN'",
            (self.session_id, symbol),
        ).fetchone()
        if row is not None:
            self.conn.execute(
                "UPDATE positions SET qty=?, avg_entry=?, stop=?, target2=?, trail_px=? WHERE id=?",
                (qty, avg_entry, stop, target2, trail_px, row["id"]),
            )
            pid = int(row["id"])
        else:
            risk_per_share = abs(avg_entry - stop) if stop is not None else None
            cur = self.conn.execute(
                "INSERT INTO positions(session_id, symbol, qty, avg_entry, stop, target2,"
                " trail_px, opened_at, status, strategy_version, ml_model_id, param_version,"
                " risk_per_share)"
                " VALUES(?,?,?,?,?,?,?,?, 'OPEN', ?, ?, ?, ?)",
                (self.session_id, symbol, qty, avg_entry, stop, target2, trail_px,
                 iso_utc(opened_at) if opened_at else _now_iso(),
                 strategy_version, ml_model_id, param_version, risk_per_share),
            )
            pid = int(cur.lastrowid)
        _finish_commit(self.conn, commit)
        return pid

    def close_position(self, symbol: str, exit_reason: str,
                       closed_at: datetime | None = None, *,
                       exit_avg_px: float | None = None,
                       realized_pnl: float | None = None,
                       r_multiple: float | None = None,
                       total_cost: float | None = None,
                       commit: bool = True) -> None:
        cur = self.conn.execute(
            "UPDATE positions SET status='CLOSED', exit_reason=?, closed_at=?,"
            " exit_avg_px=COALESCE(?, exit_avg_px), realized_pnl=COALESCE(?, realized_pnl),"
            " r_multiple=COALESCE(?, r_multiple), total_cost=COALESCE(?, total_cost)"
            " WHERE session_id=? AND symbol=? AND status='OPEN'",
            (exit_reason, iso_utc(closed_at) if closed_at else _now_iso(),
             exit_avg_px, realized_pnl, r_multiple, total_cost,
             self.session_id, symbol),
        )
        if cur.rowcount == 0:
            raise KeyError(f"no OPEN position {symbol} in session {self.session_id}")
        _finish_commit(self.conn, commit)

    def open_positions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE session_id=? AND status='OPEN' ORDER BY opened_at",
            (self.session_id,),
        ).fetchall()
        return [self._checked_dict(r) for r in rows]

    def trades_closed(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE session_id=? AND status='CLOSED' ORDER BY closed_at",
            (self.session_id,),
        ).fetchall()
        return [self._checked_dict(r) for r in rows]

    # -------------------------------------------------------- account & metrics
    def record_account_snapshot(self, ts: datetime, cash: float, invested: float,
                                unrealized: float, realized: float, equity: float,
                                hwm: float, drawdown: float,
                                *, commit: bool = True) -> int:
        cur = self.conn.execute(
            "INSERT INTO account_snapshots(session_id, ts, cash, invested, unrealized, realized,"
            " equity, hwm, drawdown) VALUES(?,?,?,?,?,?,?,?,?)",
            (self.session_id, iso_utc(ts), cash, invested, unrealized, realized, equity, hwm, drawdown),
        )
        _finish_commit(self.conn, commit)
        return int(cur.lastrowid)

    def equity_curve(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM (SELECT * FROM account_snapshots WHERE session_id=?"
            " ORDER BY ts DESC, id DESC LIMIT ?) ORDER BY ts ASC, id ASC",
            (self.session_id, limit),
        ).fetchall()
        return [self._checked_dict(r) for r in rows]

    def record_metric(self, metric: str, value: float, ts: datetime | None = None,
                      *, commit: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO metrics_timeseries(session_id, ts, metric, value) VALUES(?,?,?,?)",
            (self.session_id, iso_utc(ts) if ts else _now_iso(), metric, float(value)),
        )
        _finish_commit(self.conn, commit)

    def record_incident(self, severity: str, kind: str, detail: dict | None = None,
                        resolved_at: datetime | None = None, ts: datetime | None = None,
                        *, commit: bool = True) -> int:
        cur = self.conn.execute(
            "INSERT INTO incidents(session_id, ts, severity, kind, detail_json, resolved_at)"
            " VALUES(?,?,?,?,?,?)",
            (self.session_id, iso_utc(ts) if ts else _now_iso(), severity, kind,
             json.dumps(detail or {}), iso_utc(resolved_at)),
        )
        _finish_commit(self.conn, commit)
        return int(cur.lastrowid)

    # ------------------------------------------------------------------- funnel
    def record_session_event(self, event: str, actor: str = "system",
                             detail: dict | None = None, ts: datetime | None = None,
                             *, commit: bool = True) -> int:
        cur = self.conn.execute(
            "INSERT INTO session_events(session_id, ts, event, actor, detail_json)"
            " VALUES(?,?,?,?,?)",
            (self.session_id, iso_utc(ts) if ts else _now_iso(), event, actor,
             json.dumps(detail or {})),
        )
        _finish_commit(self.conn, commit)
        return int(cur.lastrowid)

    def record_activity(self, state: str, explanation: str = "",
                        blocker_detail: dict | None = None,
                        ts: datetime | None = None) -> int:
        """Persist the runner activity state (ACTIVITY session_event, latest-wins).

        Read by GET /api/sessions/{id} and /api/lab/summary per CONTRACT
        ADDENDUM v2: {state, explanation, blocker_detail}.
        """
        return self.record_session_event(
            "ACTIVITY", actor="watchdog",
            detail={"activity": {"state": state, "explanation": explanation,
                                 "blocker_detail": blocker_detail or {}}},
            ts=ts)

    def upsert_funnel(self, funnel: Any, ts: datetime | None = None,
                      explanation: str = "") -> None:
        """Latest-wins scan funnel snapshot, journaled as a SCAN_FUNNEL event.

        `funnel` is sts.contracts.ScanFunnel-like (has ts + counters). The v2
        schema has no dedicated funnel table; the decision journal carries it.
        `explanation` (e.g. 'no data' on watchdog liveness floors that report
        scanned=0) is journaled alongside the counters.
        """
        payload = {
            "ts": utc_iso(getattr(funnel, "ts", None)),
            "scanned": getattr(funnel, "scanned", 0),
            "eligible": getattr(funnel, "eligible", 0),
            "setups": getattr(funnel, "setups", 0),
            "ml_passed": getattr(funnel, "ml_passed", 0),
            "portfolio_ok": getattr(funnel, "portfolio_ok", 0),
            "risk_ok": getattr(funnel, "risk_ok", 0),
            "selected": getattr(funnel, "selected", 0),
            **({"explanation": explanation} if explanation else {}),
        }
        self.conn.execute(
            "INSERT INTO session_events(session_id, ts, event, actor, detail_json)"
            " VALUES(?,?,'SCAN_FUNNEL','runner',?)",
            (self.session_id, iso_utc(ts) if ts else _now_iso(), json.dumps(payload)),
        )
        # (typed history lives in scan_funnels — written by record_funnel())
        _finish_commit(self.conn, True)

    def record_funnel(self, funnel: Any, ts: datetime | None = None,
                      explanation: str = "",
                      top_rejections: list[dict] | dict | None = None,
                      *, commit: bool = True) -> int:
        """Durable scan-funnel row in scan_funnels AND the legacy SCAN_FUNNEL
        journal event (latest-wins readers keep working). `top_rejections` is
        a list of {reason, count} (or a stage->reason->count mapping)."""
        self.upsert_funnel(funnel, ts=ts, explanation=explanation)
        payload = json.dumps(top_rejections) if top_rejections else None
        cur = self.conn.execute(
            "INSERT INTO scan_funnels(session_id, ts, scanned, eligible, setups,"
            " ml_passed, portfolio_ok, risk_ok, selected, top_rejections_json, explanation)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (self.session_id, utc_iso(getattr(funnel, "ts", None))
             or utc_iso(ts) or _now_iso(),
             getattr(funnel, "scanned", 0), getattr(funnel, "eligible", 0),
             getattr(funnel, "setups", 0), getattr(funnel, "ml_passed", 0),
             getattr(funnel, "portfolio_ok", 0), getattr(funnel, "risk_ok", 0),
             getattr(funnel, "selected", 0), payload, explanation or None),
        )
        _finish_commit(self.conn, commit)
        return int(cur.lastrowid)

    def funnel_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Scan-funnel rows for THIS session (oldest-last)."""
        rows = self.conn.execute(
            "SELECT * FROM scan_funnels WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (self.session_id, limit),
        ).fetchall()
        self._check(rows[-1]) if rows else None
        out = []
        for r in reversed(rows):
            d = self._checked_dict(r)
            if d.get("top_rejections_json"):
                d["top_rejections"] = json.loads(d.pop("top_rejections_json"))
            else:
                d.pop("top_rejections_json", None)
            out.append(d)
        return out

    def latest_funnel(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT detail_json, ts, session_id FROM session_events WHERE session_id=? AND event='SCAN_FUNNEL'"
            " ORDER BY id DESC LIMIT 1",
            (self.session_id,),
        ).fetchone()
        if row is None:
            return None
        self._check(row)
        out = json.loads(row["detail_json"])
        out["journaled_at"] = row["ts"]
        return out

    # ---------------------------------------------------------------- isolation
    def _check(self, row: sqlite3.Row) -> None:
        if row["session_id"] != self.session_id:
            raise IsolationError(
                f"repo bound to session={self.session_id} touched row of session={row['session_id']}"
            )

    def _checked_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        self._check(row)
        return dict(row)
