"""SessionRunner — one asyncio task per RUNNING session.

Per-session loop = consume bar-close events -> drive broker fills -> manage
exits -> (entries only when RUNNING and feed OPEN) -> scan funnel -> select ->
risk -> execute -> journal EVERYTHING.

Semantics implemented here (audit v2):
- Exits bypass the risk engine by design (journaled as action EXIT).
- PAUSED blocks NEW entries; exit management continues (V1.2 §1).
- STOPPING(FLATTEN): cancel working orders, SELL everything at next actionable
  prices, wait for fills, then terminal STOPPED/FLATTENED.
- Fail-closed: when feed_status != OPEN during the OPEN phase, entries are
  blocked (exits still managed, with a widened-slippage note on exit intents)
  and an incident is written once per staleness episode.
- A runner exception never mutates lifecycle status: it sets an internal
  faulted flag + FAULTED incident + alert; the session remains for inspection.
- BAR-CLOSE COALESCING: the entry scan runs ONCE per unique 5m bar-close
  timestamp (triggered by the FIRST symbol-bar carrying that ts), using
  whatever frames have arrived for that ts — later symbols' same-ts bars are
  folded into the next scan. Exits/stop management remain PER-SYMBOL-BAR.
  Account snapshots/metrics are journaled at most once per bar-close ts.
- WATCHDOG: a 60s timer alongside the queue consumer journals liveness
  (FEED_STALE incidents, snapshot/funnel floors, hourly closed-market
  heartbeat, ACTIVITY state events) so the API can always answer "is it
  alive?" even with zero bars. All timing uses the injected clock.
  Bar-emission freshness is emission-aware (>=420s = stale); a barless OPEN
  market inside the boot grace reports INITIALIZING, beyond it fires
  FEED_INIT_TIMEOUT once; every closed stale/init episode journals exactly
  one FEED_RECOVERED INFO before health flips back to ok.
- SCAN-NOW: run_scan_now(reason) replays the full pipeline against cached
  data; candidates that would be entered are persisted with decision
  DEFERRED / MARKET_CLOSED when the market is not OPEN.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
from collections import Counter

import pandas as pd

from sts.config import SessionConfig
from sts.contracts import (
    Bar,
    DecisionAction,
    ExitReason,
    ScanFunnel,
    Side,
    TradeIntent,
    OrderType,
)
from sts.data import calendar as cal
from sts.lab.factory import SessionGraph
from sts.lab.policies import bar_closes_in_window, cancel_all_working, flatten_intents
from sts.marketdata.service import MarketDataService
from sts.observability.alerts import alert
from sts.observability.logs import get_logger

log = get_logger("sts.lab.runner")
from sts.portfolio.selector import ScoredCandidate
from sts.strategy.pullback_v1 import StrategyContext, prescreen_daily, regime_rules

STRATEGY_VERSION = "v1.0.0"
DECISION_WINDOW_START = "09:30"
DECISION_WINDOW_END = "15:25"

# ---- watchdog tuning (all intervals evaluated against the injected clock)
WATCHDOG_INTERVAL_S = 60.0          # tick cadence
# Bar-EMISSION freshness model (runner watchdog). A completed 5m bar can only
# be DELIVERED when a poll lands AFTER its 5-minute window closes, so the
# worst legal inter-delivery gap is:
#   BAR_PERIOD_S (300)            window must complete before emission
# + POLL_CADENCE_S (60)           next poll after the boundary; matches
#                                 MarketDataService(poll_seconds=60)
# + EMISSION_LATENCY_MARGIN_S(60) network/processing jitter allowance
# = WATCHDOG_STALE_BAR_AFTER_S = 420s; only silence beyond that is genuine
# staleness. This deliberately differs from the SERVICE-level poll-tick
# freshness (MarketDataService.STALE_AFTER_S = 300, untouched), which measures
# raw quote ticks rather than completed-bar emissions.
BAR_PERIOD_S = 300.0                # 5m window completion precedes emission
POLL_CADENCE_S = 60.0               # == MarketDataService.poll_seconds default
EMISSION_LATENCY_MARGIN_S = 60.0    # latency/jitter margin on first delivery
WATCHDOG_STALE_BAR_AFTER_S = (
    BAR_PERIOD_S + POLL_CADENCE_S + EMISSION_LATENCY_MARGIN_S)          # 420.0
# Boot grace == emission bound: from runner start, the FIRST bar legally needs
# up to WATCHDOG_STALE_BAR_AFTER_S to arrive (window + next poll + latency).
# Inside this grace a barless OPEN market reports INITIALIZING (no incident);
# at/after it, FEED_INIT_TIMEOUT fires once.
BOOT_INIT_GRACE_S = WATCHDOG_STALE_BAR_AFTER_S
WATCHDOG_SNAPSHOT_FLOOR_S = 900.0   # account_snapshot + metrics >= every 15min
WATCHDOG_FUNNEL_FLOOR_S = 1800.0    # SCAN_FUNNEL heartbeat >= every 30min
WATCHDOG_HEARTBEAT_INTERVAL_S = 3600.0  # closed-market heartbeat max hourly

# Activity states (CONTRACT ADDENDUM v2 — persisted as ACTIVITY session_events)
ACTIVITY_STATES = ("TRADING", "SCANNING", "NO_SETUPS", "RISK_BLOCKED",
                   "WAITING_MARKET_OPEN", "FEED_STALE", "FAULTED",
                   "INITIALIZING")

DEFERRED_DECISION = "DEFERRED"


def ist_naive_now() -> dt.datetime:
    """Default runner clock: wall clock rendered as NAIVE IST datetimes.

    Bar timestamps are IST-naive (see contracts.Bar); producing the same frame
    keeps bar-ts comparisons, decision windows and calendar lookups consistent.
    """
    return dt.datetime.now(cal.IST).replace(tzinfo=None)


def _aware(d: dt.datetime) -> dt.datetime:
    """Interpret a runner-frame (naive IST) datetime as IST for storage."""
    return d.replace(tzinfo=cal.IST) if d.tzinfo is None else d


def _iso(d: dt.datetime) -> str:
    """Journal ISO string in true UTC from the runner's naive-IST frame."""
    return _aware(d).astimezone(dt.timezone.utc).isoformat()


def _rules_json(rules) -> str:
    return json.dumps([{"rule_id": r.rule_id, "description": r.description,
                        "observed": r.observed, "threshold": r.threshold,
                        "passed": r.passed} for r in rules])


def _checks_json(checks) -> str:
    return json.dumps([{"check": c.check, "threshold": c.threshold,
                        "observed": c.observed, "passed": c.passed} for c in checks])


class SessionRunner:
    def __init__(
        self,
        graph: SessionGraph,
        marketdata: MarketDataService,
        symbols: list[str],
        *,
        clock: callable | None = None,
        watchdog_interval_s: float = WATCHDOG_INTERVAL_S,
    ) -> None:
        self.graph = graph
        self.marketdata = marketdata
        self.symbols = list(symbols)
        self.session_id = graph.session_id
        self.cfg: SessionConfig = graph.cfg
        self.clock = clock or ist_naive_now
        self.watchdog_interval_s = float(watchdog_interval_s)
        self._booted_at: dt.datetime = self.clock()   # init-grace reference
        self._open_phase_since: dt.datetime | None = None  # first OPEN tick → grace anchor
        self.health = "ok"                      # ok | stale | faulted
        self.last_decision_at: dt.datetime | None = None
        self.faulted = False
        self.activity: dict | None = None       # latest computed activity state

        self.pause_flag = False                 # manager-controlled
        self.stop_policy: str | None = None     # FLATTEN/HOLD when stopping
        self.stopped_event = asyncio.Event()    # runner -> manager: terminal reached
        self.done_event = asyncio.Event()       # task finished

        self._queue: asyncio.Queue | None = None
        self._consumer_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._intraday: dict[str, list[Bar]] = {}
        self._current_date: dt.date | None = None
        self._day_start_equity: float | None = None
        self._stale_episode_open = False
        self._stale_episode_started_at: dt.datetime | None = None
        self._init_timeout_episode_open = False   # boot: no first bar in grace
        self._init_timeout_started_at: dt.datetime | None = None
        self._ml_fallback_noted = False
        self._flatten_started = False
        self._entry_attempted: set[tuple[str, dt.date]] = set()

        # ---- bar-close coalescing + watchdog bookkeeping
        self._scanned_close_ts: set[dt.datetime] = set()
        self._pending_scans: list[dt.datetime] = []   # ordered unique close_ts
        self._last_bar_at: dt.datetime | None = None     # clock() of last bar
        self._watchdog_last_snapshot_at: dt.datetime | None = None
        self._watchdog_last_funnel_at: dt.datetime | None = None
        self._last_heartbeat_at: dt.datetime | None = None
        self.last_scan_summary: dict = {}

    # ------------------------------------------------------------ main loop
    async def run(self) -> None:
        log.info("runner start", extra={"session": self.session_id})
        q = self.marketdata.subscribe()
        self._queue = q
        self._consumer_task = asyncio.create_task(self._consume(q),
                                                  name=f"session-{self.session_id}-consume")
        self._watchdog_task = asyncio.create_task(self._watchdog(),
                                                  name=f"session-{self.session_id}-watchdog")
        try:
            await self._consumer_task
        finally:
            if self._watchdog_task is not None and not self._watchdog_task.done():
                self._watchdog_task.cancel()
            self.marketdata.unsubscribe(q)
            self.done_event.set()
            log.info("runner exit", extra={"session": self.session_id})

    async def _consume(self, q: asyncio.Queue) -> None:
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=0.2)
            except asyncio.TimeoutError:
                if self.stopped_event.is_set():
                    return
                continue
            try:
                self._process_event(item)
            except Exception as exc:  # noqa: BLE001 — fault, don't die silently
                self._fault(exc)
            if self.stopped_event.is_set():
                return
            # Coalesced entry scans run when the queue is drained, so a whole
            # poll-cycle batch lands before the pipeline fires.
            if not q.empty():
                continue
            try:
                self._drain_pending_scans()
            except Exception as exc:  # noqa: BLE001
                self._fault(exc)

    def _drain_pending_scans(self) -> None:
        while self._pending_scans:
            close_ts = self._pending_scans.pop(0)
            if self.stopped_event.is_set() or self.pause_flag or self.faulted:
                continue
            if self.stop_policy == "FLATTEN":
                continue
            if self.marketdata.feed_status() != "OPEN":
                continue
            portfolio = self.graph.broker.get_account_state(self.session_id)
            if self._day_start_equity is None:
                self._day_start_equity = portfolio.equity
            day_pnl = portfolio.equity - (self._day_start_equity or portfolio.equity)
            self._scan_entries(close_ts, portfolio, day_pnl)

    # ------------------------------------------------------------ watchdog
    async def _watchdog(self) -> None:
        """Timer-driven liveness journaling (CRITICAL observability floor).

        Runs alongside the queue consumer so a starved/stalled feed still
        produces heartbeats, snapshots, funnel events and the ACTIVITY state
        the API reads. Never raises into the consumer.
        """
        while True:
            await asyncio.sleep(self.watchdog_interval_s)
            if self.stopped_event.is_set():
                continue
            try:
                self.watchdog_tick()
            except Exception as exc:  # noqa: BLE001 — watchdog must survive
                log.warning("watchdog tick failed", extra={
                    "session": self.session_id, "error": repr(exc)})

    def watchdog_tick(self) -> dict:
        """One watchdog tick. Public-ish for tests (fake clocks drive it)."""
        now = self.clock()
        repo = self.graph.repo
        phase = cal.market_phase(now)  # naive input interpreted as IST
        if phase == "OPEN":
            activity = self._tick_market_open(now)
        else:
            if self._last_heartbeat_at is None or \
                    (now - self._last_heartbeat_at).total_seconds() >= WATCHDOG_HEARTBEAT_INTERVAL_S:
                repo.record_session_event("WAITING_MARKET_OPEN", actor="watchdog",
                                          detail={"phase": phase}, ts=_aware(now))
                self._last_heartbeat_at = now
            activity = {
                "state": "WAITING_MARKET_OPEN",
                "explanation": f"market phase={phase}; session alive, awaiting open",
                "blocker_detail": {"phase": phase},
            }
        self.activity = activity
        repo.record_activity(activity["state"], activity.get("explanation", ""),
                             activity.get("blocker_detail", {}), ts=_aware(now))
        return activity

    def _tick_market_open(self, now: dt.datetime) -> dict:
        repo = self.graph.repo
        age = None if self._last_bar_at is None else (now - self._last_bar_at).total_seconds()

        # Boot-grace anchor: a session that boots pre-open would otherwise burn
        # BOOT_INIT_GRACE_S while CLOSED and false-trip a FEED_INIT_TIMEOUT at
        # the first OPEN tick. Use the later of construction time and the
        # first tick observed with market phase OPEN.
        phase_open = cal.market_phase(now) == "OPEN"
        if phase_open and self._open_phase_since is None:
            self._open_phase_since = now
        grace_origin = self._open_phase_since or self._booted_at

        if self._last_bar_at is None:
            # ---- boot state: no bar has EVER arrived. A healthy emission
            # pipeline legally needs up to BOOT_INIT_GRACE_S (window completion
            # + next poll + latency) before the first bar lands, so silence
            # inside the grace is INITIALIZING (no incident); at/after it the
            # feed genuinely failed to deliver -> FEED_INIT_TIMEOUT once.
            waited = (now - grace_origin).total_seconds()
            if waited >= BOOT_INIT_GRACE_S and not self._init_timeout_episode_open:
                self._init_timeout_episode_open = True
                self._init_timeout_started_at = grace_origin
                self.health = "stale"
                repo.record_incident("WARN", "FEED_INIT_TIMEOUT",
                                     {"session": self.session_id,
                                      "detected_by": "initialization-watchdog",
                                      "waited_seconds": int(waited)}, ts=_aware(now))
                alert("FEED_INIT_TIMEOUT",
                      f"no bar within {int(BOOT_INIT_GRACE_S)}s of open "
                      "(entries blocked, fail-closed)",
                      detail={"session": self.session_id,
                              "waited_seconds": int(waited)})
        else:
            # ---- steady state: emission-aware bar freshness (>=420s stale).
            stale = age >= WATCHDOG_STALE_BAR_AFTER_S
            if stale and not self._stale_episode_open:
                self._stale_episode_open = True
                self._stale_episode_started_at = now
                self.health = "stale"
                repo.record_incident("WARN", "FEED_STALE_ENTRIES_BLOCKED",
                                     {"session": self.session_id,
                                      "detected_by": "watchdog",
                                      "seconds_since_bar": age}, ts=_aware(now))
                alert("FEED_STALE", f"no bar for >={int(WATCHDOG_STALE_BAR_AFTER_S)}s "
                                    "(entries blocked, fail-closed)",
                      detail={"session": self.session_id})
            elif not stale and (self._stale_episode_open or self._init_timeout_episode_open) \
                    and self.marketdata.feed_status() == "OPEN":
                # Belt-and-braces close (the common path recovers inline in
                # _process_bar); deduped by the episode flags. The OPEN-feed
                # guard prevents recover/reopen churn during forced-stale
                # drills (or any service-STALE period): bars may still flow
                # but the fail-closed episode must stay up until the feed
                # genuinely reopens.
                self._close_feed_episodes(now)

        # Liveness floor: snapshot + metrics at least once per 15 min even
        # with zero bars (bar-path writes go through RepoSink coalescing).
        snap_due = self._watchdog_last_snapshot_at is None or \
            (now - self._watchdog_last_snapshot_at).total_seconds() >= WATCHDOG_SNAPSHOT_FLOOR_S
        if snap_due:
            st = self.graph.broker.get_account_state(self.session_id)
            repo.record_account_snapshot(_aware(now), cash=st.cash, invested=st.invested,
                                         unrealized=st.unrealized, realized=st.realized,
                                         equity=st.equity, hwm=st.hwm,
                                         drawdown=st.drawdown_pct)
            repo.record_metric("equity", st.equity, ts=_aware(now))
            repo.record_metric("drawdown_pct", st.drawdown_pct, ts=_aware(now))
            repo.record_metric("exposure", st.gross_exposure, ts=_aware(now))
            self._watchdog_last_snapshot_at = now

        # Liveness floor: SCAN_FUNNEL at least once per 30 min. scanned=0 is
        # allowed but must carry explanation='no data'.
        funnel_due = self._watchdog_last_funnel_at is None or \
            (now - self._watchdog_last_funnel_at).total_seconds() >= WATCHDOG_FUNNEL_FLOOR_S
        if funnel_due:
            f = ScanFunnel(ts=now)
            f.scanned = 0
            repo.record_funnel(f, ts=_aware(now), explanation="no data")
            self._watchdog_last_funnel_at = now

        return self._compute_activity(now)

    def _close_feed_episodes(self, now: dt.datetime) -> None:
        """A valid bar arrived: close any open stale / init-timeout episode,
        journaling exactly ONE FEED_RECOVERED INFO per closed episode (flag-
        gated, so repeated ticks can never spam recovery rows) before health
        flips back to ok."""
        repo = self.graph.repo
        if self._stale_episode_open:
            started = self._stale_episode_started_at
            duration = None if started is None else int((now - started).total_seconds())
            repo.record_incident("INFO", "FEED_RECOVERED",
                                 {"session": self.session_id,
                                  "episode": "FEED_STALE",
                                  "duration_seconds": duration}, ts=_aware(now))
        if self._init_timeout_episode_open:
            started = self._init_timeout_started_at
            duration = None if started is None else int((now - started).total_seconds())
            repo.record_incident("INFO", "FEED_RECOVERED",
                                 {"session": self.session_id,
                                  "episode": "FEED_INIT_TIMEOUT",
                                  "duration_seconds": duration}, ts=_aware(now))
        if self._stale_episode_open or self._init_timeout_episode_open:
            self._stale_episode_open = False
            self._stale_episode_started_at = None
            self._init_timeout_episode_open = False
            self._init_timeout_started_at = None
            self.health = "ok"

    def _compute_activity(self, now: dt.datetime) -> dict:
        """Priority: FAULTED > FEED_STALE > WAITING_MARKET_OPEN > INITIALIZING >
        TRADING > RISK_BLOCKED > SCANNING > NO_SETUPS."""
        if self.faulted:
            activity = {"state": "FAULTED",
                        "explanation": "runner faulted; session preserved for inspection",
                        "blocker_detail": {}}
        elif self._stale_episode_open or self._init_timeout_episode_open:
            activity = {"state": "FEED_STALE",
                        "explanation": "market OPEN but no bars recently; entries blocked (fail-closed)",
                        "blocker_detail": {"seconds_since_bar": (
                            None if self._last_bar_at is None
                            else int((now - self._last_bar_at).total_seconds())),
                            **({"init_timeout": True}
                               if self._init_timeout_episode_open else {})}}
        elif cal.market_phase(now) != "OPEN":
            activity = {"state": "WAITING_MARKET_OPEN",
                        "explanation": f"market phase={cal.market_phase(now)}",
                        "blocker_detail": {"phase": cal.market_phase(now)}}
        elif self._last_bar_at is None:
            # Boot inside the init grace: market OPEN, awaiting the FIRST
            # legally-emitted completed bar. No incident by design.
            activity = {"state": "INITIALIZING",
                        "explanation": "market OPEN; awaiting first completed-bar "
                                       f"emission (boot grace {int(BOOT_INIT_GRACE_S)}s)",
                        "blocker_detail": {
                            "waited_seconds": int((now - self._booted_at).total_seconds())}}
        else:
            positions = self.graph.broker.get_positions(self.session_id)
            if positions:
                activity = {"state": "TRADING",
                            "explanation": f"{len(positions)} open position(s) under management",
                            "blocker_detail": {"open_positions": [p.symbol for p in positions]}}
            else:
                s = self.last_scan_summary
                candidates = int(s.get("candidates", 0))
                placed = int(s.get("placed", 0))
                if candidates > 0 and placed > 0:
                    activity = {"state": "SCANNING",
                                "explanation": "candidates found and executed",
                                "blocker_detail": {"ts": s.get("ts")}}
                elif candidates > 0:
                    activity = {"state": "RISK_BLOCKED",
                                "explanation": "candidates found but every one was rejected",
                                "blocker_detail": {"ts": s.get("ts"),
                                                   "top_rejections": s.get("top_rejections", [])}}
                else:
                    activity = {"state": "NO_SETUPS",
                                "explanation": "bars flowing; strategy produced 0 setups",
                                "blocker_detail": {"ts": s.get("ts")}}
        self.activity = activity
        return activity

    def _fault(self, exc: Exception) -> None:
        self.faulted = True
        self.health = "faulted"
        self.graph.repo.record_incident("ERROR", "FAULTED",
                                        {"error": repr(exc), "session": self.session_id})
        alert("SESSION_FAULTED", f"runner fault: {exc!r}", severity="ERROR",
              detail={"session": self.session_id})

    async def wait_until_drained(self) -> None:
        """Test helper: yield until the subscriber queue is empty."""
        while self._queue is not None and not self._queue.empty():
            await asyncio.sleep(0)

    # ------------------------------------------------------------ event path
    @staticmethod
    def _bars_from_event(item) -> list[Bar]:
        """Decode subscriber queue items defensively across BOTH interface
        generations: batch ('bars', [Bar, ...]) events (the documented v2
        contract) and legacy single (symbol, Bar) tuples."""
        try:
            _kind, payload = item
        except (TypeError, ValueError):
            return []
        if isinstance(payload, (list, tuple)):
            return [b for b in payload if isinstance(b, Bar)]
        if isinstance(payload, Bar):
            return [payload]
        return []

    def _process_event(self, item) -> None:
        for bar in self._bars_from_event(item):
            self._process_bar(bar)

    def _process_bar(self, bar: Bar) -> None:
        if bar.timeframe != "5m":
            return
        today = bar.ts.date()
        if self._current_date != today:
            self._intraday.clear()
            self._scanned_close_ts.clear()
            self._current_date = today
            self._day_start_equity = None
        self._intraday.setdefault(bar.symbol, []).append(bar)

        # One snapshot per unique bar-close ts: stamp the dedupe key BEFORE the
        # broker mutates state (RepoSink.on_update consults it).
        self.graph.sink.snapshot_key = bar.ts

        # broker drives ALL fills for this completed bar (stops/targets/trails/
        # time-stops/working limits) — exits are managed even while PAUSED.
        self.graph.broker.on_bar(self.session_id, bar)
        self._last_bar_at = self.clock()
        # A valid bar over an OPEN feed closes any open stale / init-timeout
        # episode immediately (ONE FEED_RECOVERED per episode, flag-gated)
        # before health restores. While feed_status != OPEN the fail-closed
        # episode stays up regardless of bar flow (_update_staleness governs).
        if (self._stale_episode_open or self._init_timeout_episode_open) \
                and self.marketdata.feed_status() == "OPEN":
            self._close_feed_episodes(self._last_bar_at)

        close_ts = bar.ts + dt.timedelta(minutes=5)
        if not bar_closes_in_window(bar, start=DECISION_WINDOW_START, end=DECISION_WINDOW_END):
            return

        portfolio = self.graph.broker.get_account_state(self.session_id)
        if self._day_start_equity is None:
            self._day_start_equity = portfolio.equity
        day_pnl = portfolio.equity - (self._day_start_equity or portfolio.equity)

        feed_open = self.marketdata.feed_status() == "OPEN"
        self._update_staleness(feed_open)

        # ---- STOPPING (FLATTEN): drain positions then terminalize (per-bar)
        if self.stop_policy == "FLATTEN":
            if not self._flatten_started:
                cancel_all_working(self.graph.broker, self.session_id)
                for intent in flatten_intents(self.graph.broker, self.session_id, close_ts):
                    self._place_exit(intent, reason=ExitReason.SESSION_FLATTEN,
                                     stale_note=not feed_open, ts=close_ts)
                self._flatten_started = True
            if not portfolio.positions:
                self.stopped_event.set()
            return

        # ---- regime exit management (runs PER-SYMBOL-BAR, even when PAUSED)
        self._manage_regime_exit(close_ts, feed_open)

        # ---- entries: RUNNING only, feed must be OPEN, never while paused;
        #      COALESCED once per unique 5m bar-close timestamp. A bar carrying
        #      a NEW close-ts SCHEDULES the scan; it executes once the consumer
        #      drains the queue, using whatever frames have arrived for that
        #      ts (a poll-cycle batch lands whole).
        if self.pause_flag or self.faulted:
            return
        if not feed_open:
            return
        if close_ts in self._scanned_close_ts:
            return
        self._scanned_close_ts.add(close_ts)
        self._pending_scans.append(close_ts)

    # ------------------------------------------------------------ fail-closed
    def _update_staleness(self, feed_open: bool) -> None:
        if not feed_open and not self._stale_episode_open:
            self._stale_episode_open = True
            self._stale_episode_started_at = self.clock()
            self.health = "stale"
            self.graph.repo.record_incident("WARN", "FEED_STALE_ENTRIES_BLOCKED",
                                            {"session": self.session_id})
            alert("FEED_STALE", "entries blocked (fail-closed)",
                  detail={"session": self.session_id})
        # Recovery path is owned by _close_feed_episodes (flag-gated, single
        # FEED_RECOVERED per episode, episode-opened_at timestamps). A bare
        # "feed_open and episode open" branch here used to silently flip
        # health without journaling, racing with the bar-path close — removed.

    # ------------------------------------------------------------ exits
    def _manage_regime_exit(self, close_ts: dt.datetime, feed_open: bool) -> None:
        positions = self.graph.broker.get_positions(self.session_id)
        if not positions:
            return
        idx = self.marketdata.index_frame("nifty50")
        ctx = StrategyContext(
            daily={}, intraday={}, index_daily=idx,
            vix_now=self.marketdata.vix_now(),
            now=close_ts, eligible=[], prev_day=close_ts.date(),
        )
        rules = regime_rules(ctx, self.cfg.params)
        if all(r.passed for r in rules):
            return
        for pos in positions:
            intent = TradeIntent(
                session_id=self.session_id, ts=close_ts, symbol=pos.symbol,
                side=Side.SELL, order_type=OrderType.LIMIT, qty=pos.qty,
                limit_price=round(pos.last_px * 0.995, 2),
                correlation_id=f"{self.session_id}:EXIT:REGIME_EXIT:{pos.symbol}:{close_ts.isoformat()}",
            )
            self._place_exit(intent, reason=ExitReason.REGIME_EXIT,
                             stale_note=not feed_open, ts=close_ts)

    def _place_exit(self, intent: TradeIntent, *, reason: ExitReason,
                    stale_note: bool, ts: dt.datetime) -> None:
        """Exits bypass the risk engine BY DESIGN; journal action=EXIT.

        intent + broker order row commit ATOMICALLY (single sqlite txn): a
        crash can never orphan an intent without its order, and an order
        rejection downgrades the SAME transaction to a journaled REJECT."""
        features = {
            "action": "EXIT", "exit_reason": reason.value,
            "limit_price": intent.limit_price, "qty": intent.qty,
            **({"slippage_note": "widened_stale_feed"} if stale_note else {}),
        }
        repo = self.graph.repo
        with repo.transaction():
            iid = repo.insert_intent({
                "ts": _iso(ts), "symbol": intent.symbol,
                "market_state_ref": json.dumps({"ts": _iso(ts)}),
                "feature_vector_json": json.dumps(features),
                "signals_json": "[]",
                "risk_checks_json": "[]",
                "decision": DecisionAction.EXIT.value,
                "rejection_reason": "",
                "portfolio_snapshot_json": "{}",
                "versions_json": self._versions_json(),
            }, commit=False)
            self.graph.sink.current_intent_id = iid
            self.graph.sink.exit_reason_hint = reason.value
            try:
                self.graph.order_manager.place_order(self.session_id, intent)
            except Exception as exc:  # noqa: BLE001
                repo.update_intent_decision(iid, "REJECT",
                                            rejection_reason=f"ORDER_REJECTED:{exc}",
                                            commit=False)
        self.last_decision_at = ts

    # ------------------------------------------------------------ scan-now
    async def run_scan_now(self, reason: str = "manual") -> dict:
        """Diagnostic full-pipeline scan (POST /api/sessions/{id}/scan).

        Runs eligibility -> strategy -> selector -> risk against the latest
        cached daily data + last known bars, persists funnel + intents, and
        returns {funnel, candidates, deferrals}. When the market is not OPEN,
        candidates that WOULD be entered get real persisted decisions
        decision='DEFERRED', rejection_reason='MARKET_CLOSED' (replayable);
        nothing is sent to the broker in that case.
        """
        now = self.clock()
        portfolio = self.graph.broker.get_account_state(self.session_id)
        if self._day_start_equity is None:
            self._day_start_equity = portfolio.equity
        day_pnl = portfolio.equity - (self._day_start_equity or portfolio.equity)
        return self._scan_entries(now, portfolio, day_pnl,
                                  defer_if_closed=True, scan_reason=reason)

    # ------------------------------------------------------------ entries
    def _daily_frames(self) -> dict[str, pd.DataFrame]:
        return {s: self.marketdata.get_daily_frame(s) for s in self.symbols}

    @staticmethod
    def _is_eligible(df: pd.DataFrame) -> bool:
        """Universe eligibility filter (recorded rule):
        price >= 50, >= 60 daily rows, median 20d rupee turnover >= 5e7
        (approximated by volume*close when rupee volume unavailable)."""
        if df is None or len(df) < 60:
            return False
        px = float(df["close"].iloc[-1])
        if px < 50.0:
            return False
        turnover = (df["volume"].tail(20) * df["close"].tail(20)).median()
        return float(turnover) >= 5e7

    def _corr_fn(self, daily: dict[str, pd.DataFrame]):
        rets: dict[str, pd.Series] = {}
        for s, df in daily.items():
            if df is not None and len(df) >= 61:
                rets[s] = df["close"].astype(float).pct_change().tail(60).reset_index(drop=True)

        def corr(a: str, b: str) -> float:
            ra, rb = rets.get(a), rets.get(b)
            if ra is None or rb is None or len(ra) != len(rb):
                return float("nan")   # fail closed
            return float(pd.concat([ra, rb], axis=1).corr().iloc[0, 1])
        return corr

    @staticmethod
    def _sector_fn(symbol: str) -> str:
        # No sector reference data bundled => unique pseudo-sector per symbol
        # disables the sector caps rather than failing every trade closed.
        return f"S_{symbol}"

    def _latest_bars(self) -> dict[str, Bar]:
        out: dict[str, Bar] = {}
        for sym, bars in self._intraday.items():
            if bars:
                out[sym] = bars[-1]
        return out

    def _scan_entries(self, close_ts: dt.datetime, portfolio, day_pnl: float,
                      *, defer_if_closed: bool = False,
                      scan_reason: str | None = None) -> dict:
        """Full entry pipeline for ONE coalesced bar-close timestamp.

        Returns {funnel, candidates:[{symbol,decision,...}], deferrals:[...]};
        also refreshes last_scan_summary (feeds the ACTIVITY computation) and
        the watchdog funnel-floor clock."""
        repo = self.graph.repo
        funnel = ScanFunnel(ts=close_ts)
        daily = self._daily_frames()
        funnel.scanned = len(self.symbols)
        eligible = [s for s in self.symbols if self._is_eligible(daily.get(s))]
        funnel.eligible = len(eligible)
        result = {"funnel": funnel, "candidates": [], "deferrals": []}
        if not eligible:
            repo.record_funnel(funnel, ts=_aware(close_ts))
            self._note_scan(close_ts, funnel, [])
            return result

        intraday = {
            s: pd.DataFrame({
                "ts": [b.ts for b in bars], "o": [b.open for b in bars],
                "h": [b.high for b in bars], "l": [b.low for b in bars],
                "c": [b.close for b in bars], "v": [b.volume for b in bars],
            })
            for s, bars in self._intraday.items() if bars
        }
        seed = int(hashlib.sha256(self.session_id.encode()).hexdigest()[:8], 16)
        ctx = StrategyContext(
            daily=daily, intraday=intraday,
            index_daily=self.marketdata.index_frame("nifty50"),
            vix_now=self.marketdata.vix_now(),
            now=close_ts, eligible=eligible,
            prev_day=self._current_date, rng_seed=seed,
            params=dict(self.cfg.params),
        )
        candidates = self.graph.strategy(ctx, dict(self.cfg.params))
        funnel.setups = len(candidates)

        # Closed-market diagnostic: with no intraday evidence, report how many
        # symbols are ARMED (pass all daily conditions, awaiting the breakout
        # trigger at next open). Never fabricated — daily data only.
        self._last_prescreen = []
        if not candidates and not any(self._intraday.values()):
            try:
                self._last_prescreen = prescreen_daily(ctx, dict(self.cfg.params))
            except Exception:  # noqa: BLE001 — diagnostic aid must never break the scan
                self._last_prescreen = []

        if self.cfg.ml_enabled:
            funnel.ml_passed = 0
            if not self._ml_fallback_noted:
                self._ml_fallback_noted = True
                repo.record_incident("INFO", "ML_NOT_AVAILABLE_DETERMINISTIC_FALLBACK",
                                     {"session": self.session_id})
                alert("ML_FALLBACK", "ml_enabled session has no trained model; deterministic-only",
                      detail={"session": self.session_id})

        if not candidates:
            repo.record_funnel(funnel, ts=_aware(close_ts))
            self._note_scan(close_ts, funnel, [])
            return result

        # stable scoring: preserve deterministic candidate order, score desc
        ordered = list(candidates)
        scored: list[ScoredCandidate] = []
        equity = portfolio.equity
        for i, cand in enumerate(ordered):
            score = float(len(ordered) - i)
            risk_amt = self.cfg.risk_per_trade * equity
            per_share = cand.entry_trigger_price - cand.stop_px
            qty = int(max(0.0, risk_amt / per_share)) if per_share > 0 else 0
            scored.append(ScoredCandidate(
                symbol=cand.symbol, score=score,
                entry_price=cand.entry_trigger_price, stop_px=cand.stop_px,
                qty=qty, risk_amount=qty * per_share if qty else 0.0,
                notional=qty * cand.entry_trigger_price if qty else 0.0,
            ))

        selected, rejections = self.graph.selector(
            scored, portfolio.positions, self._corr_fn(daily),
            self._sector_fn, equity, self.cfg,
        )
        reject_counter: Counter = Counter({code: 1 for _, code in rejections})
        for sym, code in rejections:
            self._journal_rejection(sym, close_ts, code, portfolio)
        funnel.portfolio_ok = len(selected)
        funnel.risk_ok = 0
        funnel.selected = 0

        adv = None  # ADV unavailable intraday -> risk engine fails closed on adv_size;
        # paper lab accepts this documented limitation via avg_daily_volume proxy:
        adv_proxy = self._adv_proxy(daily, [c.symbol for c in selected])

        defer_here = defer_if_closed and cal.market_phase(close_ts) != "OPEN"

        for cand in selected:
            src = next(c for c in ordered if c.symbol == cand.symbol)
            atr = getattr(src, "atr", None)
            intent = TradeIntent(
                session_id=self.session_id, ts=close_ts, symbol=cand.symbol,
                side=Side.BUY, order_type=OrderType.LIMIT, qty=cand.qty,
                limit_price=cand.entry_price, stop_px=cand.stop_px,
                target1_px=(cand.entry_price +
                            self.cfg.t1_multiple * (cand.entry_price - cand.stop_px)),
                target2_px=(cand.entry_price +
                            self.cfg.t2_multiple * (cand.entry_price - cand.stop_px)),
                trail_mult_atr=self.cfg.trail_mult_atr,
                correlation_id=f"{self.session_id}:ENTER:{cand.symbol}:{close_ts.isoformat()}",
            )
            intent.features_json = json.dumps({
                "action": "ENTER", "score": cand.score,
                "trigger": cand.entry_price, "atr": atr,
                "stop": cand.stop_px, "qty": cand.qty,
                **({"scan_reason": scan_reason} if scan_reason else {}),
            })
            intent.signals_json = _rules_json(getattr(src, "rules", []))
            intent.versions_json = self._versions_json()

            key = (cand.symbol, close_ts.date())
            verdict = self.graph.risk_engine.evaluate(intent, portfolio, day_pnl,
                                                      portfolio.hwm,
                                                      avg_daily_volume=adv_proxy.get(cand.symbol))
            if not verdict.approved:
                reject_counter[verdict.rejection_reason] += 1

            # Per-candidate sizing math surfaced in risk_checks_json so any
            # rejection is explainable post-hoc (qty/notional/cap/atr_frac).
            checks_list = json.loads(_checks_json(verdict.checks))
            atr_frac = (abs(cand.entry_price - cand.stop_px) / cand.entry_price
                        ) if cand.entry_price else None
            sizing = {
                "qty": cand.qty,
                "notional": round(cand.notional, 2),
                "cap_notional": round(self.cfg.max_position_pct * equity, 2),
                "min_notional": float(self.cfg.min_notional),
                "atr": atr, "atr_frac": (round(atr_frac, 6) if atr_frac is not None else None),
                "risk_amount": round(cand.risk_amount, 2),
                "equity": round(equity, 2),
                "risk_per_trade": float(self.cfg.risk_per_trade),
                "max_position_pct": float(self.cfg.max_position_pct),
            }
            checks_list.append({"check": "sizing_math",
                                "threshold": (f"min_notional<=notional<=cap "
                                              f"({sizing['min_notional']:.0f}..{sizing['cap_notional']:.0f})"),
                                "observed": json.dumps(sizing), "passed": True})
            risk_checks_json = json.dumps(checks_list)

            decision: str
            rejection_reason = "" if verdict.approved else verdict.rejection_reason
            placed = False
            if defer_here and verdict.approved:
                decision = DEFERRED_DECISION
                rejection_reason = "MARKET_CLOSED"
                result["deferrals"].append({"symbol": cand.symbol, "qty": cand.qty,
                                            "entry": cand.entry_price, "stop": cand.stop_px,
                                            "sizing": sizing})
            elif verdict.approved:
                decision = DecisionAction.ENTER.value
            else:
                decision = DecisionAction.REJECT.value

            row = {
                "ts": _iso(close_ts), "symbol": cand.symbol,
                "market_state_ref": json.dumps({"ts": _iso(close_ts)}),
                "feature_vector_json": intent.features_json,
                "signals_json": intent.signals_json,
                "ml_score": None, "ml_prob": None,
                "risk_checks_json": risk_checks_json,
                "decision": decision,
                "rejection_reason": rejection_reason,
                "portfolio_snapshot_json": json.dumps({
                    "cash": round(portfolio.cash, 2), "equity": round(portfolio.equity, 2),
                    "open_positions": len(portfolio.positions),
                    "open_risk": round(portfolio.total_open_risk, 2),
                }),
                "versions_json": intent.versions_json,
            }

            repo_obj = repo
            if decision == DEFERRED_DECISION:
                iid = repo_obj.insert_intent(row)
            else:
                # Atomic intent->order chain (single sqlite transaction): the
                # broker sink mints the order row inside place_order; a crash
                # between the two inserts commits NEITHER, and an order
                # rejection downgrades the same txn to a journaled REJECT.
                with repo_obj.transaction():
                    iid = repo_obj.insert_intent(row, commit=False)
                    if verdict.approved and cand.qty >= 1 and key not in self._entry_attempted:
                        self.graph.sink.current_intent_id = iid
                        try:
                            self.graph.order_manager.place_order(self.session_id, intent)
                            placed = True
                            self._entry_attempted.add(key)
                            funnel.risk_ok += 1
                            funnel.selected += 1
                        except Exception as exc:  # noqa: BLE001
                            repo_obj.update_intent_decision(
                                iid, "REJECT", rejection_reason=f"ORDER_REJECTED:{exc}",
                                commit=False)
                    elif verdict.approved:
                        repo_obj.update_intent_decision(iid, "NOOP",
                                                        rejection_reason="DUPLICATE_ATTEMPT",
                                                        commit=False)
            result["candidates"].append({
                "symbol": cand.symbol, "decision": decision,
                "rejection_reason": rejection_reason,
                "qty": cand.qty, "entry": cand.entry_price, "stop": cand.stop_px,
                "sizing": sizing,
            })
            self.last_decision_at = close_ts

        repo.record_funnel(funnel, ts=_aware(close_ts))
        self._watchdog_last_funnel_at = self.clock()
        self._note_scan(close_ts, funnel, result["candidates"], reject_counter)
        return result

    def _note_scan(self, close_ts: dt.datetime, funnel: ScanFunnel,
                   candidates: list[dict], reject_counter: Counter | None = None) -> None:
        self.last_scan_summary = {
            "ts": close_ts.isoformat(),
            "scanned": getattr(funnel, "scanned", 0),
            "setups": getattr(funnel, "setups", 0),
            "candidates": len(candidates),
            "placed": sum(1 for c in candidates if c["decision"] == DecisionAction.ENTER.value),
            "top_rejections": [[code, n] for code, n in
                               (reject_counter or Counter()).most_common(5)],
        }

    def _adv_proxy(self, daily: dict[str, pd.DataFrame], symbols: list[str]) -> dict[str, float]:
        """ADV proxy from cached daily volume (documented approximation)."""
        out = {}
        for s in symbols:
            df = daily.get(s)
            if df is not None and len(df) >= 20:
                out[s] = float(df["volume"].tail(20).mean())
        return out

    def _journal_rejection(self, symbol: str, ts: dt.datetime, code: str, portfolio) -> None:
        self.graph.repo.insert_intent({
            "ts": _iso(ts), "symbol": symbol,
            "market_state_ref": json.dumps({"ts": _iso(ts)}),
            "feature_vector_json": json.dumps({"stage": "portfolio_selection"}),
            "signals_json": "[]",
            "risk_checks_json": "[]",
            "decision": DecisionAction.REJECT.value,
            "rejection_reason": code,
            "portfolio_snapshot_json": json.dumps({"equity": round(portfolio.equity, 2)}),
            "versions_json": self._versions_json(),
        })
        self.last_decision_at = ts

    def _versions_json(self) -> str:
        return json.dumps({
            "strategy_version": STRATEGY_VERSION,
            "costs_version": self.graph.costs.version,
            "param_version": self.graph.config_hash[:12],
        })
