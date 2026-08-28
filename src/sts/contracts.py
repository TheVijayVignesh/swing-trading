"""Shared contracts for the Swing Lab. ALL modules build against these types.

Dependency rule (enforced by review):
    data -> contracts <- {strategy, ml, portfolio, risk, execution, brokers, lab}
    lab -> everything; nothing knows about `lab` except main.py.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


# ---------------------------------------------------------------- enums
class SessionStatus(str, enum.Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ABORTED = "ABORTED"


class TerminalState(str, enum.Enum):
    FLATTENED = "FLATTENED"
    HELD = "HELD"


class Mode(str, enum.Enum):
    PAPER = "paper"
    SANDBOX = "sandbox"   # reserved (Alpaca adapter later); never live in this build
    LIVE = "live"         # interlocked OFF


class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    LIMIT = "LIMIT"
    STOP_LOSS_LIMIT = "SL_LIMIT"


class OrderStatus(str, enum.Enum):
    WORKING = "WORKING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class DecisionAction(str, enum.Enum):
    ENTER = "ENTER"
    EXIT = "EXIT"
    REJECT = "REJECT"
    NOOP = "NOOP"


class ExitReason(str, enum.Enum):
    STOP = "STOP"
    TRAIL_STOP = "TRAIL_STOP"
    TARGET1 = "TARGET1"
    TARGET2 = "TARGET2"
    TIME_STOP = "TIME_STOP"
    REGIME_EXIT = "REGIME_EXIT"
    SESSION_FLATTEN = "SESSION_FLATTEN"


# ---------------------------------------------------------------- market data
@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    ts: datetime          # bar OPEN time, IST-naive
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str        # "1d" | "5m"


@dataclass(frozen=True, slots=True)
class SymbolMeta:
    symbol: str           # NSE trading symbol, e.g. "RELIANCE"
    yahoo_symbol: str     # e.g. "RELIANCE.NS"
    security_id: str      # placeholder for future broker mapping
    sector: str = ""
    isin: str = ""


# ---------------------------------------------------------------- strategy
@dataclass(slots=True)
class RuleResult:
    rule_id: str
    description: str
    observed: str
    threshold: str
    passed: bool


@dataclass(slots=True)
class CandidateSignal:
    """A deterministic setup. Produced by strategy.evaluate() — pure function of data."""
    symbol: str
    ts: datetime                      # decision time (bar close that generated it)
    entry_trigger_price: float        # prior-day-high / range-top breakout level
    atr: float                        # daily ATR(14) at signal time
    stop_px: float                    # entry - 1.5*ATR (set at intent time from trigger+buffer)
    rules: list[RuleResult] = field(default_factory=list)


@dataclass(slots=True)
class ExitDirective:
    symbol: str
    reason: ExitReason
    limit_price: float | None         # None => next-bar-open exit per fill model


# ---------------------------------------------------------------- execution
@dataclass(slots=True)
class TradeIntent:
    session_id: str
    ts: datetime
    symbol: str
    side: Side
    order_type: OrderType
    qty: int
    limit_price: float
    stop_px: float | None = None
    target1_px: float | None = None   # half position
    target2_px: float | None = None   # remainder
    trail_mult_atr: float = 1.5
    correlation_id: str = ""          # idempotency key
    features_json: str = "{}"
    signals_json: str = "[]"
    ml_score: float | None = None
    versions_json: str = "{}"


@dataclass(slots=True)
class RiskCheck:
    check: str
    threshold: str
    observed: str
    passed: bool


@dataclass(slots=True)
class RiskVerdict:
    approved: bool
    checks: list[RiskCheck] = field(default_factory=list)
    rejection_reason: str = ""


@dataclass(slots=True)
class FillRecord:
    order_id: str
    session_id: str
    symbol: str
    side: Side
    px: float
    qty: int
    ts: datetime
    cost_breakdown: dict = field(default_factory=dict)


# ---------------------------------------------------------------- portfolio / positions
@dataclass(slots=True)
class PositionView:
    symbol: str
    qty: int
    avg_entry: float
    last_px: float
    stop_px: float
    target1_px: float | None
    target2_px: float | None
    trail_px: float | None
    unrealized_pnl: float
    pnl_pct: float
    held_days: int
    risk_amount: float
    t1_done: bool


@dataclass(slots=True)
class PortfolioState:
    cash: float
    invested: float
    unrealized: float
    realized: float
    equity: float
    hwm: float
    drawdown_pct: float
    gross_exposure: float
    total_open_risk: float
    positions: list[PositionView] = field(default_factory=list)


# ---------------------------------------------------------------- broker protocol
class Broker(Protocol):
    """The ONLY trading surface. Strategy/risk/lab must not know which implementation."""
    def get_account_state(self, session_id: str) -> PortfolioState: ...
    def get_positions(self, session_id: str) -> list[PositionView]: ...
    def place_order(self, session_id: str, intent: TradeIntent) -> str: ...   # returns order_id
    def cancel_order(self, session_id: str, order_id: str) -> bool: ...
    def modify_order(self, session_id: str, order_id: str,
                     new_limit: float) -> str: ...                            # returns NEW order id (replace semantics)
    def on_bar(self, session_id: str, bar: Bar) -> list[FillRecord]: ...      # drive fills
    def capabilities(self) -> dict: ...


# ---------------------------------------------------------------- funnel (decision pipeline)
@dataclass(slots=True)
class ScanFunnel:
    ts: datetime
    scanned: int = 0
    eligible: int = 0
    setups: int = 0
    ml_passed: int = 0
    portfolio_ok: int = 0
    risk_ok: int = 0
    selected: int = 0
