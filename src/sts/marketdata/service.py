"""MarketDataService — the ONE shared, read-only market-data singleton.

Responsibilities (ARCHITECTURE_V1.2 §2 shared column):
- run a FailoverPoller (daemon thread) polling 5m bars for the universe
  (primary: NSE quotes; fallback: Yahoo v8 chart batches);
- maintain {symbol: latest completed 5m Bar} + last_prices and a canonical
  'current bar window' state;
- publish bar-close events: every poll cycle, build {symbol: latest completed
  bar} diff and push ONE batch event (list[Bar]) to every subscriber's
  asyncio.Queue (maxsize=1000; never dropped silently — overflow increments
  the dropped_events incident counter);
- track feed_status OPEN/STALE/CLOSED and last_tick_age_s via calendar.market_phase
  (a never-ticking feed is stale-after-STALE_ALERT_AFTER_S since phase became OPEN);
- expose get_daily_frame(symbol) from the parquet cache with an in-memory LRU,
  index_frame('nifty50') / vix_frame() from the bootstrapped index series
  (_NSEI.parquet / _INDIAVIX.parquet), plus a session-aware daily refresh:
  poll_cycle calls maybe_refresh_daily() (30-min spacing) which runs
  refresh_daily_if_stale() in a background thread when the ^NSEI parquet is
  missing the expected latest trading session; status via daily_refresh_status().

Testability: clock and poller are injectable; poll_cycle() is public so tests
can drive cycles synchronously without threads or network.

Thread-safety note: subscribe() captures the caller's running loop ONCE;
_poll_batch always uses that stored loop via call_soon_threadsafe — polling
runs in a foreign thread where asyncio.get_running_loop() would raise.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Protocol

import pandas as pd

from sts.contracts import Bar
from sts.data import calendar as cal
from sts.data.history import COLUMNS, FRESHNESS_SECONDS
from sts.observability.alerts import alert
from sts.observability.logs import get_logger

log = get_logger("sts.marketdata")

QUEUE_MAXSIZE = 1000
STALE_AFTER_S = 300.0          # feed considered STALE after this tick age in OPEN phase
STALE_ALERT_AFTER_S = 600.0    # normative alert threshold (>10 min)
INDEX_TICKERS = {"nifty50": "^NSEI", "indiavix": "^INDIAVIX"}
DAILY_REFRESH_CHECK_INTERVAL_S = 1800.0   # evaluate refresh-due at most every 30 min
DAILY_REFRESH_CUTOFF = dt.time(16, 30)    # IST: bhavcopy window done -> today's session expected
DAILY_STALE_WARN_INTERVAL_S = 3600.0      # market-hours stale-daily WARN at most hourly


class Poller(Protocol):
    def poll_once(self) -> tuple[int, int]: ...
    def get_bars(self) -> dict[str, Bar]: ...


def _default_clock() -> dt.datetime:
    return dt.datetime.now(tz=cal.IST)


def _safe_parquet_name(symbol: str) -> str:
    return symbol.replace("^", "_").replace("/", "_")


def _iso(now: dt.datetime) -> str:
    return now.isoformat()


class MarketDataService:
    def __init__(
        self,
        symbols: list[str],
        *,
        poller: Poller | None = None,
        clock: Callable[[], dt.datetime] | None = None,
        daily_dir: str | Path = "data/parquet/candles_1d",
        poll_seconds: float = 60.0,
        lru_size: int = 256,
    ) -> None:
        self.symbols = list(symbols)
        self.clock = clock or _default_clock
        self.daily_dir = Path(daily_dir)
        self.poll_seconds = poll_seconds
        if poller is None:
            # Failover stack: primary NSE quotes API (1 request covers the whole
            # index), fallback Yahoo v8 chart batches (rate-limited but real OHLC).
            try:
                from sts.data.live import FailoverPoller, NSEQuotePoller, YahooChartPoller
                poller = FailoverPoller([
                    NSEQuotePoller(list(self.symbols), poll_seconds=poll_seconds),
                    YahooChartPoller(
                        [f"{s}.NS" if "." not in s and not s.startswith("^") else s
                         for s in self.symbols],
                        poll_seconds=poll_seconds),
                ])
            except Exception:  # pragma: no cover
                poller = None
        self.poller: Poller = poller

        self._lock = threading.Lock()
        self._latest: dict[str, Bar] = {}           # latest COMPLETED bar per symbol
        self.last_prices: dict[str, float] = {}
        self._subscribers: list[asyncio.Queue] = []
        self._subscriber_loops: list[asyncio.AbstractEventLoop] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_tick_at: dt.datetime | None = None
        self._forced_stale = False
        self._stale_alerted = False
        self._open_phase_since: dt.datetime | None = None
        self._dropped_events = 0
        self._daily_cache: OrderedDict[str, pd.DataFrame] = OrderedDict()
        self._lru_size = lru_size
        # ---- daily refresh state (see maybe_refresh_daily / daily_refresh_status)
        self._daily_refresh_lock = threading.Lock()
        self._last_refresh_check: dt.datetime | None = None   # last DUE evaluation
        self._refresh_in_flight = False
        self._last_refresh_attempt: str | None = None
        self._last_refresh_success: str | None = None
        self._last_refresh_error: str | None = None
        self._sessions_added = 0
        # ---- daily-data observability surface (see daily_data_status);
        # ok stays None until the first refresh attempt completes.
        self._last_refresh_ok: bool | None = None
        self._symbols_updated = 0
        self._daily_as_of: dt.date | None = None               # latest ^NSEI date seen on disk
        self._stale_data_warned_at: dt.datetime | None = None  # hourly WARN latch

    # ------------------------------------------------------------ lifecycle
    def start_thread(self) -> None:
        """Run the poll loop in a daemon thread (production path)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_forever, name="marketdata", daemon=True)
        self._thread.start()

    def stop_thread(self) -> None:
        self._stop.set()

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            started = dt.datetime.now()
            try:
                self.poll_cycle()
            except Exception:  # noqa: BLE001 — loop must survive anything
                log.exception("poll_cycle crashed (loop survives)")
            elapsed = (dt.datetime.now() - started).total_seconds()
            self._stop.wait(max(0.0, self.poll_seconds - elapsed))

    # ------------------------------------------------------------ pub/sub
    def subscribe(self) -> asyncio.Queue:
        """Register a bounded queue. MUST be called from a running event loop.

        The caller's loop is captured ONCE here; _poll_batch later publishes
        from the feed thread via loop.call_soon_threadsafe."""
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()  # pragma: no cover — defensive
        with self._lock:
            self._subscribers.append(q)
            self._subscriber_loops.append(loop)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                i = self._subscribers.index(q)
                self._subscribers.pop(i)
                self._subscriber_loops.pop(i)

    @property
    def dropped_events(self) -> int:
        """Incident counter: bar events lost to subscriber-queue overflow.
        Overflow is NEVER silent — every drop is counted and alerted."""
        with self._lock:
            return self._dropped_events

    def _publish_batch(self, bars: list[Bar]) -> None:
        """Publish ONE batch event ("bars", list[Bar]) per subscriber.

        Event shape is the v2 queue contract consumed by SessionRunner
        (_bars_from_event): ("bars", [Bar, ...]); legacy single-bar tuples
        remain readable by older consumers.

        Called from the feed thread: always uses the loop captured at
        subscribe() time via call_soon_threadsafe (get_running_loop() is
        unavailable outside the app loop and previously dropped EVERY event)."""
        if not bars:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None                      # feed thread / no loop here
        with self._lock:
            pairs = list(zip(self._subscribers, self._subscriber_loops))
        for q, loop in pairs:
            item = ("bars", list(bars))
            if loop is running and running is not None:
                self._enqueue_or_count_drop(self, q, item)   # same loop: sync path
                continue
            try:
                loop.call_soon_threadsafe(self._enqueue_or_count_drop, self, q, item)
            except RuntimeError:  # pragma: no cover — loop closed
                continue

    @staticmethod
    def _enqueue_or_count_drop(service: "MarketDataService", q: asyncio.Queue,
                               item: tuple[str, list[Bar]]) -> None:
        """Runs on the subscriber's loop. A full queue drops the NEWEST batch
        (older ones are about to be consumed) — the loss is counted in
        service.dropped_events and alerted; never silent."""
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            with service._lock:
                service._dropped_events += 1
            alert("QUEUE_OVERFLOW_DROP",
                  f"subscriber queue full; dropped batch of {len(item)} bar events",
                  severity="WARN")

    # ------------------------------------------------------------ polling
    def poll_cycle(self) -> tuple[int, int]:
        """One cycle: poll -> detect advanced bars -> publish ONE batch event
        per subscriber (coalesced {symbol: latest completed bar} diff)."""
        updated, failed = self.poller.poll_once()
        now = self.clock()
        bars = self.poller.get_bars()
        batch: list[Bar] = []
        with self._lock:
            for sym, bar in bars.items():
                old = self._latest.get(sym)
                if old is None or bar.ts > old.ts:
                    self._latest[sym] = bar
                    self.last_prices[sym] = bar.close
                    batch.append(bar)
            if batch:
                self._last_tick_at = now
        if batch:
            self._publish_batch(batch)
        try:
            self.maybe_refresh_daily(now)   # lightweight due-check (30-min spacing)
        except Exception:  # noqa: BLE001 — feed loop must survive anything
            log.exception("daily refresh scheduling failed (feed loop survives)")
        return len(batch), failed

    # ------------------------------------------------------------ state API
    @property
    def last_tick_age_s(self) -> int | None:
        with self._lock:
            last = self._last_tick_at
        if last is None:
            return None
        return int((self.clock() - last).total_seconds())

    def feed_status(self) -> str:
        """OPEN / STALE / CLOSED using calendar.market_phase(clock())."""
        phase = cal.market_phase(self.clock())
        if phase != "OPEN":
            self._open_phase_since = None
            return "CLOSED"
        now = self.clock()
        if self._open_phase_since is None:
            self._open_phase_since = now
        stale = self._forced_stale
        age = self.last_tick_age_s
        if not stale:
            stale = age is None or age > STALE_AFTER_S
        if stale and not self._stale_alerted and not self._forced_stale:
            # A never-ticking feed has age None — measure staleness from when
            # the phase became OPEN instead (otherwise the alert can NEVER fire).
            overdue = (
                age > STALE_ALERT_AFTER_S if age is not None
                else (now - self._open_phase_since).total_seconds() > STALE_ALERT_AFTER_S
            )
            if overdue:
                self._stale_alerted = True
                alert("FEED_STALE_OVER_10MIN", "feed stale for over 10 minutes",
                      severity="ERROR")
        return "STALE" if stale else "OPEN"

    def clear_stale_alert_latch(self) -> None:
        self._stale_alerted = False

    def get_bar(self, symbol: str) -> Bar | None:
        with self._lock:
            return self._latest.get(symbol)

    def current_window(self) -> dict[str, list[Bar]]:
        """Canonical 'current bar window' snapshot: latest bar per symbol."""
        with self._lock:
            return {s: b for s, b in self._latest.items()}

    def force_stale(self, stale: bool = True) -> None:
        """Test/drill hook: force feed_status STALE regardless of tick age."""
        self._forced_stale = stale

    def feed_health(self) -> dict:
        """One-call feed-health snapshot for /api/system/health & dashboard.

        Consumes FailoverPoller.source_health()/active_source_name DEFENSIVELY
        (getattr guards): a poller without them (or a teammate's partial
        rollout) degrades to UNKNOWN sides and the default source label
        instead of raising. _latest/_dropped_events read under self._lock."""
        try:
            state = self.feed_status()
        except Exception:  # noqa: BLE001 — health must never 500
            state = "CLOSED"
        try:
            phase = cal.market_phase(self.clock())
        except Exception:  # noqa: BLE001
            phase = None
        active = getattr(self.poller, "active_source_name", None)
        # active_source_name may be a @property (FailoverPoller: the attr IS
        # the str, not callable) or a plain method — support both shapes.
        source = active() if callable(active) else active
        if not source:
            source = "NSEQuotePoller"
        raw = {}
        src_health = getattr(self.poller, "source_health", None)
        if callable(src_health):
            try:
                raw = dict(src_health() or {})
            except Exception:  # noqa: BLE001
                raw = {}

        def _side(name: str) -> dict:
            d = raw.get(name)
            if not isinstance(d, dict):
                return {"name": name.upper(), "status": "UNKNOWN",
                        "consecutive_failures": 0, "last_error": None,
                        "last_success_ts": None}
            return {
                "name": str(d.get("name") or name.upper()),
                "status": str(d.get("status") or "UNKNOWN").upper(),
                "consecutive_failures": int(d.get("consecutive_failures") or 0),
                "last_error": d.get("last_error"),
                "last_success_ts": d.get("last_success_ts"),
            }

        with self._lock:
            latest = dict(self._latest)
            dropped = int(self._dropped_events)
        last_bar = None
        if latest:
            sym, bar = max(latest.items(), key=lambda kv: kv[1].ts)
            try:
                age = int((self._as_ist(self.clock()) - self._as_ist(bar.ts))
                          .total_seconds())
            except Exception:  # noqa: BLE001 — unparseable ts must not 500
                age = None
            last_bar = {"symbol": sym, "ts": bar.ts.isoformat(), "age_s": age}
        return {
            "state": state,
            "phase": phase,
            "source": str(source),
            "primary": _side("primary"),
            "fallback": _side("fallback"),
            "last_tick_age_s": self.last_tick_age_s,
            "last_bar": last_bar,
            "dropped_events": dropped,
        }

    # ------------------------------------------------------------ daily frames
    def get_daily_frame(self, symbol: str) -> pd.DataFrame:
        """Daily OHLCV frame from the parquet cache (LRU). Empty when absent."""
        key = _safe_parquet_name(symbol)
        with self._lock:
            hit = self._daily_cache.get(key)
            if hit is not None:
                self._daily_cache.move_to_end(key)
                return hit
        path = self.daily_dir / f"{key}.parquet"
        if path.exists():
            df = pd.read_parquet(path)
        else:
            df = pd.DataFrame(columns=COLUMNS)
        if len(df):
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
        with self._lock:
            self._daily_cache[key] = df
            while len(self._daily_cache) > self._lru_size:
                self._daily_cache.popitem(last=False)
        return df

    def invalidate_daily_cache(self) -> None:
        with self._lock:
            self._daily_cache.clear()

    def index_frame(self, name: str) -> pd.DataFrame:
        """Daily index series, e.g. nifty50 -> data/parquet/candles_1d/_NSEI.parquet
        (written by scripts/bootstrap_index.py; may carry source='proxy_ew20')."""
        ticker = INDEX_TICKERS.get(name)
        if ticker is None:
            raise KeyError(f"unknown index '{name}'; known: {sorted(INDEX_TICKERS)}")
        return self.get_daily_frame(ticker)

    def vix_frame(self) -> pd.DataFrame:
        """Daily ^INDIAVIX series (data/parquet/candles_1d/_INDIAVIX.parquet)."""
        return self.index_frame("indiavix")

    def vix_now(self) -> float | None:
        df = self.index_frame("indiavix")
        if df.empty:
            return None
        return float(df["close"].iloc[-1])

    def refresh_daily_if_stale(self, symbols: list[str] | None = None,
                               max_age_s: float = FRESHNESS_SECONDS) -> dict[str, str]:
        """Nightly/intraday hook: incrementally refresh (via update_daily) every
        daily parquet that is NOT file-fresh (mtime older than max_age_s).
        Always considers the index series (^NSEI, ^INDIAVIX). Fresh symbols are
        NEVER re-downloaded, so repeated calls are cheap."""
        from sts.data.history import update_daily
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        wanted = list(symbols) if symbols is not None else list(self.symbols)
        for ticker in INDEX_TICKERS.values():
            if ticker not in wanted:
                wanted.append(ticker)
        now_ts = time.time()
        stale: list[str] = []
        for sym in wanted:
            path = self.daily_dir / f"{_safe_parquet_name(sym)}.parquet"
            if not path.exists() or (now_ts - path.stat().st_mtime) > max_age_s:
                stale.append(sym)
        if not stale:
            log.info("daily refresh: all %d series fresh (<=%.0fs) — nothing to do",
                     len(wanted), max_age_s)
            return {}
        statuses = update_daily(stale, out_dir=self.daily_dir)
        self.invalidate_daily_cache()
        log.info("daily refresh: refreshed=%d fresh_skipped=%d statuses=%s",
                 len(stale), len(wanted) - len(stale), statuses)
        return statuses

    # ---------------------------------------------------- daily refresh (session-aware)
    @staticmethod
    def _as_ist(now: dt.datetime) -> dt.datetime:
        if now.tzinfo is None:
            return now.replace(tzinfo=cal.IST)
        return now.astimezone(cal.IST)

    def expected_latest_session(self, now: dt.datetime | None = None) -> dt.date:
        """Latest trading session whose daily bar we should already have.

        Today counts only on a trading day AND after the ~16:30 IST bhavcopy
        window; otherwise (and on weekends/holidays) it is the most recent
        prior trading day."""
        now_ist = self._as_ist(now if now is not None else self.clock())
        today = now_ist.date()
        post_window = now_ist.time() >= DAILY_REFRESH_CUTOFF
        day = today
        for _ in range(60):
            if cal.is_trading_day(day) and (day != today or post_window):
                return day
            day -= dt.timedelta(days=1)
        raise RuntimeError("no trading day found within 60 days")

    def _nsei_session_dates(self) -> set[dt.date]:
        path = self.daily_dir / f"{_safe_parquet_name('^NSEI')}.parquet"
        if not path.exists():
            self._daily_as_of = None
            return set()
        try:
            df = pd.read_parquet(path, columns=["date"])
        except Exception as exc:  # noqa: BLE001 — unreadable file means "not current"
            log.warning("cannot read index parquet %s: %s", path, exc)
            self._daily_as_of = None
            return set()
        if df.empty:
            self._daily_as_of = None
            return set()
        dates = set(pd.to_datetime(df["date"]).dt.date)
        self._daily_as_of = max(dates)   # memoized for daily_data_status()
        return dates

    def _nsei_as_of(self) -> dt.date | None:
        """Latest ^NSEI session date on disk, cached at due-check time (every
        parquet read refreshes it; the first status call pays one cheap read)."""
        if self._daily_as_of is None:
            self._nsei_session_dates()
        return self._daily_as_of

    def daily_refresh_due(self, now: dt.datetime | None = None) -> bool:
        """True when the ^NSEI parquet is missing the expected latest session."""
        try:
            expected = self.expected_latest_session(now)
        except RuntimeError:
            return False
        return expected not in self._nsei_session_dates()

    def _warn_stale_daily_during_open(self, now: dt.datetime, due: bool) -> None:
        """Stale daily data during market-open hours is operator-relevant
        (wrong prior-day triggers); WARN at most once per hour, never per
        poll cycle. Silent when CLOSED or when data is current."""
        if not due or cal.market_phase(now) != "OPEN":
            return
        last = self._stale_data_warned_at
        if last is not None and \
                (now - last).total_seconds() < DAILY_STALE_WARN_INTERVAL_S:
            return
        self._stale_data_warned_at = now
        as_of = self._daily_as_of
        log.warning("daily data STALE during market hours: as_of=%s expected=%s",
                    as_of.isoformat() if as_of else "none",
                    self.expected_latest_session(now).isoformat())

    def maybe_refresh_daily(self, now: dt.datetime | None = None,
                            force: bool = False,
                            sync: bool = False) -> dict | None:
        """Lifecycle hook called from poll_cycle: at most every 30 min, evaluate
        whether a daily refresh is DUE and if so run it in a background thread
        (sync=True runs inline — used by tests and scripts/refresh_daily.py).

        force=True bypasses both the due-check and the 30-min cooldown.
        Returns the refresh result dict when a refresh ran inline, else None."""
        check_at = now if now is not None else self.clock()
        with self._daily_refresh_lock:
            if self._refresh_in_flight:
                return None
            if not force:
                last = self._last_refresh_check
                if last is not None and \
                        (check_at - last).total_seconds() < DAILY_REFRESH_CHECK_INTERVAL_S:
                    return None
                self._last_refresh_check = check_at
                due = self.daily_refresh_due(check_at)
                self._warn_stale_daily_during_open(check_at, due)
                if not due:
                    return None
            self._refresh_in_flight = True
        if sync:
            return self._run_daily_refresh()
        threading.Thread(target=self._run_daily_refresh,
                         name="daily-refresh", daemon=True).start()
        return None

    def _run_daily_refresh(self) -> dict:
        try:
            before = self._nsei_session_dates()
            statuses = self.refresh_daily_if_stale()
            added = sorted(self._nsei_session_dates() - before)
            unavailable = sorted(s for s, v in statuses.items() if v == "unavailable")
            updated = sum(1 for v in statuses.values() if v != "unavailable")
            with self._daily_refresh_lock:
                self._last_refresh_attempt = _iso(self.clock())
                self._sessions_added = len(added)
                if statuses and len(unavailable) == len(statuses):
                    self._last_refresh_ok = False
                    self._symbols_updated = 0
                    self._last_refresh_error = f"all {len(unavailable)} series unavailable"
                    log.warning("daily refresh FAILED: %s", self._last_refresh_error)
                    result = {"ok": False, "error": self._last_refresh_error}
                else:
                    self._last_refresh_ok = True
                    self._symbols_updated = updated
                    self._last_refresh_success = _iso(self.clock())
                    # partial failures stay visible; full success clears the error
                    self._last_refresh_error = (
                        f"partial: unavailable={unavailable}" if unavailable else None
                    )
                    result = {"ok": True, "statuses": statuses,
                              "sessions_added": [d.isoformat() for d in added]}
                    log.info("daily refresh ok: +%d session(s) %s", len(added),
                             result["sessions_added"])
            return result
        except Exception as exc:  # noqa: BLE001 — never crash; retry next cycle
            with self._daily_refresh_lock:
                self._last_refresh_attempt = _iso(self.clock())
                self._last_refresh_ok = False
                self._symbols_updated = 0
                self._last_refresh_error = f"{type(exc).__name__}: {exc}"
            log.warning("daily refresh error (will retry next cycle): %s", exc)
            return {"ok": False, "error": str(exc)}
        finally:
            with self._daily_refresh_lock:
                self._refresh_in_flight = False

    def daily_refresh_status(self) -> dict:
        """Status for API/dashboard consumers: {last_attempt, last_success,
        last_error, sessions_added, in_flight, due}."""
        with self._daily_refresh_lock:
            out = {
                "last_attempt": self._last_refresh_attempt,
                "last_success": self._last_refresh_success,
                "last_error": self._last_refresh_error,
                "sessions_added": self._sessions_added,
                "in_flight": self._refresh_in_flight,
            }
        out["due"] = self.daily_refresh_due()
        return out

    def daily_data_status(self) -> dict:
        """Health-API surface for daily-data currency (key names are a FIXED
        contract — consumed by /api/system/health):

            {"as_of": iso-date|None,     # latest date present in ^NSEI parquet
             "expected_session": iso|None,
             "stale": bool,              # fail-closed: unknown == stale
             "last_refresh": {"at": iso|None, "ok": bool|None,
                              "error": str|None, "symbols_updated": int}}

        Cheap by design: as_of is memoized at due-check time (see
        _nsei_session_dates); only expected_latest_session is recomputed."""
        as_of = self._nsei_as_of()
        try:
            expected: dt.date | None = self.expected_latest_session()
        except RuntimeError:
            expected = None
        # fail-closed: missing/unreadable data or undeterminable expectation
        # must never be reported as current.
        stale = as_of is None or expected is None or as_of < expected
        with self._daily_refresh_lock:
            last_refresh = {
                "at": self._last_refresh_attempt,
                "ok": self._last_refresh_ok,
                "error": self._last_refresh_error,
                "symbols_updated": self._symbols_updated,
            }
        return {
            "as_of": as_of.isoformat() if as_of is not None else None,
            "expected_session": expected.isoformat() if expected is not None else None,
            "stale": stale,
            "last_refresh": last_refresh,
        }

    # ------------------------------------------------------------ misc
    @staticmethod
    def universe_snapshot_id(symbols: list[str]) -> str:
        return hashlib.sha256(",".join(sorted(symbols)).encode()).hexdigest()[:16]
