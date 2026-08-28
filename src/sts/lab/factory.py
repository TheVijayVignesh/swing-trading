"""build_session_graph — ONE fully isolated graph per session (V1.2 §2/§5).

Every instance here is per-session: TradingRepo bound to the session id, an
independent PaperBroker ledger, its own OrderManager/RiskEngine/strategy
binding. NO mutable state is shared between graphs.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from pathlib import Path

from sts.brokers.costs import CostSchedule, load_cost_schedule
from sts.brokers.fillmodel import SlippageModel, SpreadModel
from sts.brokers.paper import OrderSink, PaperBroker
from sts.config import SessionConfig, content_hash
from sts.contracts import FillRecord, PortfolioState, Side, TradeIntent
from sts.execution.order_manager import OrderManager
from sts.portfolio import selector as portfolio_selector
from sts.risk import engine as risk_engine
from sts.storage.repos import TradingRepo
from sts.strategy.registry import get_strategy


def find_costs_yaml(explicit: str | Path | None = None) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates += [Path("configs/costs.yaml"),
                   Path(__file__).resolve().parents[3] / "configs" / "costs.yaml"]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError("configs/costs.yaml not found")


class RiskEngine:
    """Thin stateful wrapper binding the pure risk engine to one session cfg."""

    def __init__(self, cfg: SessionConfig) -> None:
        self.cfg = cfg

    def evaluate(self, intent: TradeIntent, portfolio: PortfolioState,
                 day_pnl: float, hwm: float, *, avg_daily_volume: float | None = None):
        return risk_engine.evaluate(intent, portfolio, self.cfg, day_pnl, hwm,
                                    avg_daily_volume=avg_daily_volume)


class RepoSink:
    """PaperBroker OrderSink wired to this session's TradingRepo."""

    def __init__(self, repo: TradingRepo) -> None:
        self.repo = repo
        self._order_rows: dict[str, int] = {}     # broker_order_id -> orders.id
        self.current_intent_id: int | None = None  # runner sets before placing
        self._open_since: dict[str, object] = {}   # symbol -> first-seen PositionView
        self._pending_close: dict[str, str] = {}   # symbol -> exit_reason
        self.exit_reason_hint: str | None = None   # runner sets before placing exits
        # Bar-close coalescing (audit v2): runner stamps the originating bar ts
        # before broker.on_bar; account_snapshot + metrics are written AT MOST
        # ONCE per unique key (= one per 5m bar-close ts per session), instead
        # of once per symbol-bar (~14,800/day amplification on a 200-symbol
        # universe). None => always write (manual placement paths).
        self.snapshot_key: object | None = None
        self._last_snapshot_key: object | None = object()  # sentinel != None
        self.last_snapshot_wallclock: float | None = None
        # ---- per-position accumulation (schema v3 trade-level columns)
        self._pos_meta: dict[str, dict] = {}   # symbol -> accumulators below

    def _meta(self, symbol: str) -> dict:
        return self._pos_meta.setdefault(symbol, {
            "risk_per_share": None,
            "buy_notional": 0.0, "sell_notional": 0.0,
            "buy_qty": 0, "sell_qty": 0,
            "fees": 0.0, "slippage": 0.0,
            "fill_ids": [], "order_ids": [],
        })

    def order_row_id(self, broker_order_id: str) -> int | None:
        return self._order_rows.get(broker_order_id)

    # -- OrderSink protocol -------------------------------------------------
    def on_order(self, ov) -> None:
        row = self._order_rows.get(ov.order_id)
        if row is None:
            row = self.repo.insert_order({
                "intent_id": self.current_intent_id,
                "broker_order_id": ov.order_id,
                "symbol": getattr(ov, "symbol", None),
                "side": ov.side.value,
                "type": ov.order_type.value,
                "qty": ov.qty,
                "limit_px": ov.limit_price,
                "status": ov.status.value,
                "submitted_at": _iso(ov.created_ts),
                "idempotency_key": ov.correlation_id or ov.order_id,
            })
            self._order_rows[ov.order_id] = row
        else:
            self.repo.update_order(
                row,
                status=ov.status.value if hasattr(ov.status, "value") else str(ov.status),
                replaced_by_id=ov.replaced_by,
            )

    def on_fill(self, fill: FillRecord) -> None:
        row = self._order_rows.get(fill.order_id)
        if row is None:
            # Broker-internal directive fills (STOP/TARGET/TIME_STOP/FLATTEN)
            # use synthetic 'dir:' ids — journal a pseudo-order row so the
            # fill chain stays queryable and recovery replay works.
            row = self.repo.insert_order({
                "intent_id": None,
                "broker_order_id": fill.order_id,
                "symbol": fill.symbol,
                "side": fill.side.value,
                "type": "LIMIT",
                "qty": fill.qty,
                "limit_px": fill.px,
                "status": "FILLED",
                "submitted_at": _iso(fill.ts),
                "idempotency_key": fill.order_id,
            })
            self._order_rows[fill.order_id] = row
        else:
            self.repo.update_order(row, filled_qty=fill.qty, avg_fill_px=fill.px)
        fee, slip = TradingRepo.split_costs(fill.cost_breakdown)
        meta = self._meta(fill.symbol)
        if row not in meta["order_ids"]:
            meta["order_ids"].append(row)
        fid = self.repo.insert_fill(row, fill.ts, fill.px, fill.qty, fill.cost_breakdown)
        meta["fill_ids"].append(fid)
        meta["fees"] += fee
        meta["slippage"] += slip
        notional = float(fill.px) * int(fill.qty)
        if fill.side is Side.BUY:
            meta["buy_notional"] += notional
            meta["buy_qty"] += int(fill.qty)
        else:
            meta["sell_notional"] += notional
            meta["sell_qty"] += int(fill.qty)
        if fill.side is Side.SELL:
            # dir ids look like 'dir:{symbol}:{REASON}:{ts}' (PaperBroker);
            # explicit SELL-limit exits use the runner's reason hint.
            parts = fill.order_id.split(":")
            if len(parts) >= 4 and parts[0] == "dir":
                reason = parts[2]
            else:
                reason = self.exit_reason_hint or "EXIT"
                self.exit_reason_hint = None
            self._pending_close.setdefault(fill.symbol, reason)

    def _close_metrics(self, symbol: str) -> dict:
        """Position-close columns from accumulated fills (schema v3).

        realized_pnl = sell_notional − buy_notional − total costs — exact for
        a FULLY closed position regardless of entry averaging; r_multiple
        divides by the initial per-share risk captured at open.
        """
        m = self._pos_meta.get(symbol) or {}
        out: dict = {}
        total_cost = m.get("fees", 0.0) + m.get("slippage", 0.0)
        if m.get("sell_qty"):
            out["exit_avg_px"] = m["sell_notional"] / m["sell_qty"]
            realized = m["sell_notional"] - m["buy_notional"] - total_cost
            out["realized_pnl"] = realized
            rps = m.get("risk_per_share")
            if rps and m.get("buy_qty"):
                out["r_multiple"] = realized / (rps * m["buy_qty"])
        if total_cost:
            out["total_cost"] = total_cost
        return out

    def on_update(self, state: PortfolioState) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        if self.snapshot_key is not None and self.snapshot_key == self._last_snapshot_key:
            pass  # dedupe: snapshot for this bar-close ts already journaled
        else:
            self.repo.record_account_snapshot(
                now, cash=state.cash, invested=state.invested, unrealized=state.unrealized,
                realized=state.realized, equity=state.equity, hwm=state.hwm,
                drawdown=state.drawdown_pct,
            )
            self.repo.record_metric("equity", state.equity, ts=now)
            self.repo.record_metric("drawdown_pct", state.drawdown_pct, ts=now)
            self.repo.record_metric("exposure", state.gross_exposure, ts=now)
            self._last_snapshot_key = self.snapshot_key
            self.last_snapshot_wallclock = time.time()
        # ---- mirror broker positions into the positions table
        seen: set[str] = set()
        for p in state.positions:
            seen.add(p.symbol)
            self._open_since.setdefault(p.symbol, p)
            prev = self._open_since[p.symbol]
            meta = self._meta(p.symbol)
            if meta["risk_per_share"] is None and p.stop_px:
                # initial per-share risk, captured at position open
                meta["risk_per_share"] = abs(p.avg_entry - p.stop_px)
            pid = self.repo.upsert_position(
                p.symbol, p.qty, p.avg_entry, p.stop_px,
                target2=p.target2_px, trail_px=p.trail_px,
                opened_at=getattr(prev, "_opened_wallclock", None),
            )
            if meta["fill_ids"] or meta["order_ids"]:
                self.repo.tag_position(fill_ids=meta["fill_ids"],
                                       order_ids=meta["order_ids"], position_id=pid)
                meta["fill_ids"], meta["order_ids"] = [], []
        for sym in set(self._open_since) - seen:
            reason = self._pending_close.pop(sym, "EXIT")
            try:
                self.repo.close_position(sym, reason, closed_at=now,
                                         **self._close_metrics(sym))
            except KeyError:  # pragma: no cover — already closed
                pass
            self._open_since.pop(sym, None)
            self._pos_meta.pop(sym, None)
        self._pending_close.clear()


@dataclass(slots=True)
class SessionGraph:
    session_id: str
    cfg: SessionConfig
    repo: TradingRepo
    broker: PaperBroker
    order_manager: OrderManager
    risk_engine: RiskEngine
    selector: object                      # portfolio_selector.select callable
    strategy: object                      # registered evaluate() callable
    sink: RepoSink
    costs: CostSchedule
    config_hash: str


def build_session_graph(cfg: SessionConfig, conn, session_id: str,
                        cost_path: str | Path | None = None) -> SessionGraph:
    """Assemble the isolated per-session object graph."""
    repo = TradingRepo(conn, session_id)
    costs = load_cost_schedule(find_costs_yaml(cost_path))
    clock = lambda: dt.datetime.now(tz=dt.timezone.utc)  # noqa: E731
    sink = RepoSink(repo)
    spread = SpreadModel()
    slippage = SlippageModel()
    broker = PaperBroker(
        costs, spread, slippage,
        clock=clock,
        sink=sink,
        starting_cash=float(cfg.capital_initial),
        time_stop_days=int(cfg.time_stop_days),
    )
    return SessionGraph(
        session_id=session_id,
        cfg=cfg,
        repo=repo,
        broker=broker,
        order_manager=OrderManager(broker),
        risk_engine=RiskEngine(cfg),
        selector=portfolio_selector.select,
        strategy=get_strategy(cfg.strategy_id),
        sink=sink,
        costs=costs,
        config_hash=content_hash(cfg),
    )


def _iso(d: dt.datetime) -> str:
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).isoformat()
