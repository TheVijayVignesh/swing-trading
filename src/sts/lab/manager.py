"""LabManager — singleton owning session tasks, lifecycle transitions,
boot recovery (ARCHITECTURE_V1.2 §1/§3/§5).

Concurrency model: one asyncio task per RUNNING session, all on the uvicorn
event loop; SQLite WAL serializes writes. Hard cap of 10 concurrent sessions
(the Postgres migration trigger documented in §3).
"""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import json
from pathlib import Path

from sts.config import (MICRO_TIER_CAPITAL_THRESHOLD, SessionConfig, content_hash,
                        default_risk_profile_for, from_yaml)
from sts.contracts import TerminalState
from sts.lab.factory import SessionGraph, build_session_graph, find_costs_yaml
from sts.lab.runner import STRATEGY_VERSION, SessionRunner
from sts.brokers.costs import load_cost_schedule
from sts.observability.alerts import alert
from sts.observability.logs import get_logger
from sts.storage.repos import SessionRepo, TradingRepo

log = get_logger("sts.lab")

MAX_SESSIONS = 10
FLATTEN_TIMEOUT_S = 30.0        # grace for the runner to confirm flat
REF_DIR_CANDIDATES = (Path("data/ref"), Path(__file__).resolve().parents[3] / "data" / "ref")


class LifecycleError(ValueError):
    """Illegal lifecycle transition -> HTTP 400."""


class LabFullError(RuntimeError):
    """More than MAX_SESSIONS concurrent sessions requested -> HTTP 409."""


def _bundled_universe(name: str) -> list[str]:
    fname = {"NIFTY50": "nifty50_membership.csv", "NIFTY200": "nifty200_membership.csv"}.get(name)
    if fname is None:
        raise KeyError(f"unknown universe {name!r}; known: NIFTY50, NIFTY200")
    for d in REF_DIR_CANDIDATES:
        p = d / fname
        if p.exists():
            out = []
            for row in csv.reader(line for line in p.read_text().splitlines() if not line.startswith("#")):
                if row and row[0].strip() != "symbol":
                    sym = row[0].strip()
                    out.append(sym.split(".")[0])
            return out
    raise FileNotFoundError(f"bundled membership CSV missing for {name}")


class LabManager:
    def __init__(self, conn, marketdata, *, cost_path: str | Path | None = None,
                 universe_resolver=None) -> None:
        self.conn = conn
        self.marketdata = marketdata
        self.sessions = SessionRepo(conn)
        self.cost_path = find_costs_yaml(cost_path)
        self._universe_resolver = universe_resolver or _bundled_universe
        self.graphs: dict[str, SessionGraph] = {}
        self.runners: dict[str, SessionRunner] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------ creation
    def resolve_universe(self, name: str) -> list[str]:
        return list(self._universe_resolver(name))

    @staticmethod
    def _apply_micro_default(cfg: SessionConfig) -> SessionConfig:
        """Sizing-envelope fix (audit v2): capital < ₹30k defaults to the
        `micro` risk profile UNLESS the user explicitly set risk_profile
        (detected via pydantic model_fields_set, so clone() overrides count
        as explicit)."""
        explicit = "risk_profile" in cfg.model_fields_set
        suggested = default_risk_profile_for(cfg.capital_initial, explicit)
        if suggested and cfg.risk_profile != suggested:
            data = cfg.model_dump()
            data["risk_profile"] = suggested
            return SessionConfig(**data)
        return cfg

    def create_session(self, cfg: SessionConfig) -> str:
        if cfg.mode == "live":
            raise LifecycleError("LIVE_INTERLOCKED")
        cfg = self._apply_micro_default(cfg)
        symbols = self.resolve_universe(cfg.universe)
        if len(symbols) < 3:
            raise LifecycleError("UNIVERSE_TOO_SMALL")
        sid = self.sessions.create_session(cfg)
        costs_version = load_cost_schedule(self.cost_path).version
        self.conn.execute(
            "UPDATE sessions SET strategy_version=?, param_version=?, costs_version=?,"
            " universe_snapshot_id=? WHERE id=?",
            (STRATEGY_VERSION, content_hash(cfg)[:12], costs_version,
             self.marketdata.universe_snapshot_id(symbols), sid),
        )
        self.conn.commit()
        log.info("session created", extra={"session": sid, "session_name": cfg.name})
        return sid

    def clone(self, source_id: str, new_name: str | None = None,
              overrides: dict | None = None) -> str:
        src = self.sessions.get_session(source_id)
        if src is None:
            raise KeyError(f"unknown session {source_id}")
        cfg = from_yaml(src["config_yaml"])
        data = cfg.model_dump()
        if new_name:
            data["name"] = new_name
        ov = overrides or {}
        for key in ("capital_initial", "ml_enabled", "risk_profile", "on_stop_policy"):
            if key in ov and ov[key] is not None:
                data[key] = ov[key]
        if "risk_profile" not in ov:
            # model_dump marks every field explicit; drop it so create_session's
            # micro-tier default (capital < threshold, profile not user-chosen)
            # can still apply to the clone.
            data.pop("risk_profile", None)
        clone_cfg = SessionConfig(**data)
        # never touches the source beyond reading its frozen config_yaml
        return self.create_session(clone_cfg)

    # ------------------------------------------------------------ lifecycle
    def _cfg_of(self, row: dict) -> SessionConfig:
        return from_yaml(row["config_yaml"])

    def start(self, session_id: str) -> str:
        row = self.sessions.get_session(session_id)
        if row is None:
            raise KeyError(f"unknown session {session_id}")
        status = row["status"]
        if status not in ("CREATED", "PAUSED"):
            raise LifecycleError(f"illegal transition START from {status}")
        running = sum(1 for s in self.sessions.list_sessions() if s["status"] == "RUNNING")
        if running >= MAX_SESSIONS:
            raise LabFullError(f"concurrent session cap ({MAX_SESSIONS}) reached")

        self._spawn_runner(session_id, row)
        now = dt.datetime.now(dt.timezone.utc)
        self.sessions.set_status(session_id, "RUNNING")
        if status == "CREATED":
            self.conn.execute("UPDATE sessions SET started_at=? WHERE id=?",
                              (now.isoformat(), session_id))
        self.conn.commit()
        self.sessions.record_event(session_id, "STARTED", actor="lab",
                                   detail={"resumed_from": status})
        return "RUNNING"

    def _symbols_for(self, row: dict) -> list[str]:
        cfg = self._cfg_of(row)
        try:
            return self.resolve_universe(cfg.universe)
        except Exception:  # noqa: BLE001 — fall back to service universe
            return list(self.marketdata.symbols)

    def _spawn_runner(self, session_id: str, row: dict) -> SessionRunner:
        """Build (or reuse) the session graph and start a fresh runner task.

        Shared by start(), resume() (zombie fix: a PAUSED/RUNNING session that
        survived a reboot has no live runner task — resume must spawn one, not
        just flip the pause flag on a ghost object) and recover_on_boot()."""
        graph = self.graphs.get(session_id)
        if graph is None:
            graph = build_session_graph(self._cfg_of(row), self.conn, session_id,
                                        cost_path=self.cost_path)
            self.graphs[session_id] = graph
        runner = SessionRunner(graph, self.marketdata, self._symbols_for(row))
        self.runners[session_id] = runner
        self.tasks[session_id] = asyncio.get_running_loop().create_task(
            runner.run(), name=f"session-{session_id}")
        return runner

    def _task_alive(self, session_id: str) -> bool:
        task = self.tasks.get(session_id)
        return task is not None and not task.done()

    def pause(self, session_id: str) -> str:
        row = self.sessions.get_session(session_id)
        if row is None:
            raise KeyError(f"unknown session {session_id}")
        if row["status"] != "RUNNING":
            raise LifecycleError(f"illegal transition PAUSE from {row['status']}")
        runner = self.runners.get(session_id)
        if runner:
            runner.pause_flag = True
        self.sessions.set_status(session_id, "PAUSED")
        self.sessions.record_event(session_id, "PAUSED", actor="lab")
        return "PAUSED"

    def resume(self, session_id: str) -> str:
        row = self.sessions.get_session(session_id)
        if row is None:
            raise KeyError(f"unknown session {session_id}")
        status = row["status"]
        if status == "PAUSED":
            if self._task_alive(session_id):
                runner = self.runners.get(session_id)
                if runner:
                    runner.pause_flag = False
            else:
                # Post-reboot zombie: no live runner task exists for this
                # session — spawn one instead of flipping a ghost flag.
                runner = self._spawn_runner(session_id, row)
                runner.pause_flag = False
        elif status == "RUNNING":
            if self._task_alive(session_id):
                raise LifecycleError("illegal transition RESUME from RUNNING")
            # Session shows RUNNING after boot recovery but has NO runner task
            # (crash between recovery and spawn, or external kill): re-spawn
            # so it actually consumes bars again.
            self._spawn_runner(session_id, row)
            self.sessions.record_event(session_id, "RESUMED", actor="lab",
                                       detail={"resumed_from": "RUNNING", "respawned": True})
            return "RUNNING"
        else:
            raise LifecycleError(f"illegal transition RESUME from {status}")
        self.sessions.set_status(session_id, "RUNNING")
        self.sessions.record_event(session_id, "RESUMED", actor="lab")
        return "RUNNING"

    async def stop(self, session_id: str, policy_override: str | None = None) -> str:
        row = self.sessions.get_session(session_id)
        if row is None:
            raise KeyError(f"unknown session {session_id}")
        status = row["status"]
        if status == "CREATED":
            self.sessions.set_status(session_id, "ABORTED")
            self.sessions.record_event(session_id, "STOP_REQUESTED", actor="lab",
                                       detail={"policy": "abort-from-created"})
            return "ABORTED"
        if status not in ("RUNNING", "PAUSED"):
            raise LifecycleError(f"illegal transition STOP from {status}")

        policy = (policy_override or self._cfg_of(row).on_stop_policy).upper()
        if policy not in ("FLATTEN", "HOLD"):
            raise LifecycleError(f"bad stop policy {policy!r}")
        self.sessions.record_event(session_id, "STOP_REQUESTED", actor="lab",
                                   detail={"policy": policy})

        runner = self.runners.get(session_id)
        if policy == "HOLD":
            self.sessions.set_status(session_id, "STOPPED", terminal_state=TerminalState.HELD.value)
            self._finalize_row_times(session_id)
            self.sessions.record_event(session_id, "STOPPED", actor="lab",
                                       detail={"terminal_state": "HELD"})
            if runner:
                runner.stop_policy = "HOLD"
                runner.stopped_event.set()
            await self._cancel_task(session_id)
            return "STOPPED"

        # FLATTEN: hand control to the runner; it cancels working orders, emits
        # SESSION_FLATTEN exits, waits for fills, then signals stopped_event.
        if runner is None:
            raise LifecycleError("no live runner to flatten session")
        self.sessions.set_status(session_id, "STOPPING")
        self.sessions.record_event(session_id, "STOPPING", actor="lab",
                                   detail={"policy": "FLATTEN"})
        runner.pause_flag = False
        runner.stop_policy = "FLATTEN"
        try:
            await asyncio.wait_for(runner.stopped_event.wait(), timeout=FLATTEN_TIMEOUT_S)
        except asyncio.TimeoutError:
            # Timeout correctness (audit v2): NEVER claim FLATTENED while
            # positions may remain. Flat after timeout -> honest STOPPED;
            # positions remain -> ABORTED with terminal_state HELD + incident.
            graph = self.graphs.get(session_id)
            remaining = [p.symbol for p in
                         (graph.broker.get_positions(session_id) if graph else [])]
            if remaining:
                self.conn.execute(
                    "INSERT INTO incidents(session_id, ts, severity, kind, detail_json,"
                    " resolved_at) VALUES(?,?,?,?,?,NULL)",
                    (session_id, dt.datetime.now(dt.timezone.utc).isoformat(), "ERROR",
                     "FLATTEN_TIMEOUT_POSITIONS_HELD",
                     json.dumps({"open_positions": remaining, "policy": "FLATTEN"})),
                )
                self.conn.commit()
                self.sessions.set_status(session_id, "ABORTED",
                                         terminal_state=TerminalState.HELD.value)
                self._finalize_row_times(session_id)
                self.sessions.record_event(session_id, "STOPPED", actor="lab",
                                           detail={"status": "ABORTED",
                                                   "terminal_state": "HELD",
                                                   "reason": "FLATTEN_TIMEOUT_POSITIONS_HELD"})
                await self._cancel_task(session_id)
                return "ABORTED"
        self.sessions.set_status(session_id, "STOPPED", terminal_state=TerminalState.FLATTENED.value)
        self._finalize_row_times(session_id)
        self.sessions.record_event(session_id, "STOPPED", actor="lab",
                                   detail={"terminal_state": "FLATTENED"})
        await self._cancel_task(session_id)
        return "STOPPED"

    def _finalize_row_times(self, session_id: str) -> None:
        self.conn.execute("UPDATE sessions SET ended_at=? WHERE id=?",
                          (dt.datetime.now(dt.timezone.utc).isoformat(), session_id))
        self.conn.commit()

    async def _cancel_task(self, session_id: str) -> None:
        task = self.tasks.pop(session_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.runners.pop(session_id, None)

    # ------------------------------------------------------------ recovery
    def recover_on_boot(self) -> list[str]:
        """Re-spawn runners for sessions that were RUNNING at boot.

        Broker ledgers are rebuilt from the journal (RepoSink replay):

            cash_rebuilt = capital_initial
                - SUM_over_BUY_fills(px*qty + cost_total)
                + SUM_over_SELL_fills(px*qty - cost_total)

        realized_pnl rebuilt by FIFO-pairing each symbol's fills;
        hwm = max account_snapshots.hwm ever journaled. Orphaned WORKING
        orders (their placement context is unknowable post-crash) are marked
        CANCELLED — fail-closed per the v1.1 §14 playbook.
        """
        recovered: list[str] = []
        for row in self.sessions.list_sessions():
            sid = row["id"]
            if row["status"] == "RUNNING":
                self._restore_ledger(sid, row)
                self._spawn_runner(sid, row)
                recovered.append(sid)
                self.sessions.record_event(sid, "RECOVERED", actor="system",
                                           detail={"positions_restored": True})
                alert("RECOVERY_DONE", f"session {sid} recovered", severity="INFO")
            elif row["status"] == "PAUSED":
                continue  # stays paused
            # STOPPED / ABORTED untouched
        return recovered

    def _restore_ledger(self, sid: str, row: dict) -> None:
        repo = TradingRepo(self.conn, sid)
        cfg = self._cfg_of(row)
        graph = self.graphs.get(sid) or build_session_graph(cfg, self.conn, sid,
                                                            cost_path=self.cost_path)
        self.graphs[sid] = graph

        fills = self.conn.execute(
            "SELECT f.px, f.qty, f.cost_breakdown_json, o.side, o.broker_order_id,"
            " i.symbol FROM fills f JOIN orders o ON o.id=f.order_id"
            " LEFT JOIN intents i ON i.id=o.intent_id"
            " WHERE f.session_id=? ORDER BY f.ts, f.id",
            (sid,),
        ).fetchall()

        cash = float(row["capital_initial"])
        lots: dict[str, list[tuple[float, int]]] = {}   # symbol -> [(px, qty), ...]
        realized = 0.0
        for f in fills:
            px, qty = float(f["px"]), int(f["qty"])
            sym = f["symbol"]
            if sym is None:
                # pseudo directive orders carry 'dir:{symbol}:{REASON}:{ts}' ids
                bid = str(f["broker_order_id"])
                sym = bid.split(":")[1] if bid.startswith("dir:") else None
            if not sym:
                continue
            costs_total = float((json.loads(f["cost_breakdown_json"] or "{}")).get("total", 0.0))
            if f["side"] == "BUY":
                cash -= px * qty + costs_total
                lots.setdefault(sym, []).append((px, qty))
            else:
                cash += px * qty - costs_total
                remaining = qty
                sym_lots = lots.setdefault(sym, [])
                while remaining > 0 and sym_lots:
                    lot_px, lot_qty = sym_lots[0]
                    take = min(lot_qty, remaining)
                    realized += (px - lot_px) * take - (costs_total * take / qty)
                    if lot_qty > take:
                        sym_lots[0] = (lot_px, lot_qty - take)
                    else:
                        sym_lots.pop(0)
                    remaining -= take

        snaps = self.conn.execute(
            "SELECT MAX(hwm) AS hwm FROM account_snapshots WHERE session_id=?", (sid,)
        ).fetchone()
        hwm = float(snaps["hwm"]) if snaps and snaps["hwm"] else None

        positions: list[dict] = []
        open_orders: dict[str, object] = {}
        for p in repo.open_positions():
            mult = float(cfg.trail_mult_atr) or 1.5
            stop = float(p["stop"])
            opened = p["opened_at"]
            opened_dt = dt.datetime.fromisoformat(opened) if isinstance(opened, str) else None
            positions.append({
                "symbol": p["symbol"], "qty": int(p["qty"]),
                "avg_entry": float(p["avg_entry"]), "stop_px": stop,
                "target1_px": None, "target2_px": float(p["target2"]) if p["target2"] else None,
                "trail_mult_atr": mult,
                "atr": abs(float(p["avg_entry"]) - stop) / mult,
                "hh_since_t1": float(p["avg_entry"]),
                "t1_done": False, "trail_active": False, "pending_time_exit": False,
                "opened_ts": opened_dt,
                "last_seen_date": opened_dt.date() if opened_dt else dt.date.today(),
                "held_days": 0,
            })
        # orphaned WORKING orders: fail-closed cancel
        for o in self.conn.execute(
            "SELECT id FROM orders WHERE session_id=? AND status='WORKING'", (sid,)
        ).fetchall():
            self.conn.execute("UPDATE orders SET status='CANCELLED' WHERE id=?", (o["id"],))
        self.conn.commit()

        graph.broker.restore(sid, cash=cash, positions_list=positions,
                             open_orders=open_orders, realized=realized, hwm=hwm)
