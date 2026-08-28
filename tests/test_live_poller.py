"""LivePoller tests — fully offline via mocked HTTP (incl. malformed responses)."""
from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo
import threading

import pytest
import requests

from sts.contracts import Bar
from sts.data import calendar as cal
from sts.data import live


def good_payload(sym: str, minutes_ago_closed: int = 5) -> dict:
    """One completed 5m bar closing `minutes_ago_closed` minutes before now."""
    now_utc = dt.datetime.now(tz=ZoneInfo("UTC"))
    open_ts = int((now_utc - dt.timedelta(minutes=minutes_ago_closed)).timestamp())
    return {
        "chart": {
            "result": [{
                "meta": {"symbol": sym},
                "timestamp": [open_ts, open_ts + 300],
                "indicators": {"quote": [{
                    "open": [100.0, 101.0], "high": [102.0, 103.0],
                    "low": [99.0, 100.5], "close": [101.0, 102.0],
                    "volume": [500, 700],
                }]},
            }],
            "error": None,
        }
    }


class FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        if self._payload is None:
            raise ValueError("malformed body")
        if self._payload == "RAISE":
            raise RuntimeError("corrupt json")
        return self._payload


def make_poller(monkeypatch, symbols: list[str], responses: dict[str, FakeResp]) -> live.LivePoller:
    sess = requests.Session()
    def fake_get(url, **kwargs):
        for sym, resp in responses.items():
            if sym in url:
                return resp
        return FakeResp(None)
    monkeypatch.setattr(sess, "get", fake_get)
    return live.LivePoller(symbols, poll_seconds=60, session=sess)


def force_open_phase(monkeypatch):
    """Pin market_phase to OPEN regardless of real clock."""
    monkeypatch.setattr(cal, "market_phase", lambda *a, **k: "OPEN")


# --------------------------------------------------------------- parsing
def test_parse_last_bar_picks_completed_bar():
    bar = live.parse_last_bar("RELIANCE.NS", good_payload("RELIANCE.NS", minutes_ago_closed=6))
    assert isinstance(bar, Bar)
    assert bar.symbol == "RELIANCE.NS"
    assert bar.timeframe == "5m"
    assert bar.close == 101.0  # first element is fully closed; last is forming -> skipped
    assert bar.ts.tzinfo is None  # IST-naive per Bar contract


def test_parse_last_bar_none_on_malformed():
    assert live.parse_last_bar("X.NS", {}) is None
    assert live.parse_last_bar("X.NS", {"chart": {"result": []}}) is None
    assert live.parse_last_bar("X.NS", {"chart": {"result": [{"meta": {}}]}}) is None


# --------------------------------------------------------------- polling behaviour
def test_poll_once_populates_bars(monkeypatch):
    force_open_phase(monkeypatch)
    p = make_poller(monkeypatch, ["AAA.NS"], {"AAA.NS": FakeResp(good_payload("AAA.NS"))})
    updated, failed = p.poll_once()
    assert updated == 1 and failed == 0
    assert p.status == "FEED_OPEN"
    bars = p.get_bars()
    assert "AAA.NS" in bars and bars["AAA.NS"].timeframe == "5m"


def test_poll_skips_when_market_closed(monkeypatch):
    monkeypatch.setattr(cal, "market_phase", lambda *a, **k: "CLOSED")
    called = {"n": 0}
    p = live.LivePoller(["AAA.NS"])
    monkeypatch.setattr(p, "_fetch_batch", lambda b: called.__setitem__("n", called["n"] + 1) or {})
    updated, failed = p.poll_once()
    assert updated == 0 and called["n"] == 0
    assert p.status == "CLOSED"


def test_malformed_batch_skipped_without_dying(monkeypatch):
    force_open_phase(monkeypatch)
    p = make_poller(
        monkeypatch, ["BAD.NS", "GOOD.NS"],
        {"BAD.NS": FakeResp("RAISE"), "GOOD.NS": FakeResp(good_payload("GOOD.NS"))},
    )
    updated, failed = p.poll_once()  # must not raise
    assert updated >= 1              # the healthy symbol still landed
    assert "GOOD.NS" in p.get_bars()


def test_stale_status_after_300s(monkeypatch):
    force_open_phase(monkeypatch)
    p = live.LivePoller(["AAA.NS"])
    assert p.status == "FEED_STALE"          # never polled
    with p._lock:
        p._last_success_ts = dt.datetime.now(tz=cal.IST) - dt.timedelta(seconds=301)
    assert p.status == "FEED_STALE"          # too old while OPEN
    with p._lock:
        p._last_success_ts = dt.datetime.now(tz=cal.IST)
    assert p.status == "FEED_OPEN"


def test_batch_size_cap():
    with pytest.raises(ValueError):
        live.LivePoller(["A.NS"], batch_size=101)


def test_run_forever_stops_promptly(monkeypatch):
    force_open_phase(monkeypatch)
    p = make_poller(monkeypatch, ["AAA.NS"], {"AAA.NS": FakeResp(good_payload("AAA.NS"))})
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    stop = threading.Event()
    stop.set()  # exit immediately after first iteration check... ensure at most one loop pass
    t = threading.Thread(target=p.run_forever, args=(stop,), daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive()


def test_thread_safety_concurrent_reads_and_writes(monkeypatch):
    force_open_phase(monkeypatch)
    p = make_poller(monkeypatch, [f"S{i}.NS" for i in range(3)],
                    {f"S{i}.NS": FakeResp(good_payload(f"S{i}.NS")) for i in range(3)})
    errors: list[Exception] = []
    def reader():
        try:
            for _ in range(200):
                p.get_bars()
                _ = p.last_success_ts
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    p.poll_once()
    for t in threads:
        t.join()
    assert errors == []


# --------------------------------------------------------------- impersonated default
def test_default_session_is_impersonated_when_none_injected(monkeypatch):
    """No session injected => the TLS-impersonating session is used by default
    (Yahoo 429s plain python-requests fingerprints outright)."""
    class SentinelSession:
        def __init__(self):
            self.headers = {}
    monkeypatch.setattr(live, "_impersonating_session", lambda: SentinelSession())
    p = live.LivePoller(["X.NS"])
    assert isinstance(p._session, SentinelSession)


def test_default_session_falls_back_to_plain_requests_without_curl_cffi(monkeypatch):
    monkeypatch.setattr(live, "_impersonating_session", lambda: None)
    p = live.LivePoller(["X.NS"])
    assert isinstance(p._session, requests.Session)


def test_real_default_session_is_curl_cffi_when_installed():
    try:
        from curl_cffi.requests import Session as CffiSession
    except ImportError:
        pytest.skip("curl_cffi not installed")
    p = live.LivePoller(["X.NS"])
    assert isinstance(p._session, CffiSession)


# --------------------------------------------------------------- failure visibility
def test_batch_failures_emit_one_warning_per_cycle_not_debug(caplog, monkeypatch):
    force_open_phase(monkeypatch)
    p = make_poller(monkeypatch, ["BAD.NS"], {})   # unknown symbol -> malformed body
    with caplog.at_level(logging.DEBUG):
        p.poll_once()
        p.poll_once()
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "batch failures" in r.getMessage()]
    debugs = [r for r in caplog.records
              if r.levelno == logging.DEBUG and "live fetch failed" in r.getMessage()]
    assert len(warnings) == 2            # exactly ONE summary per cycle
    assert len(debugs) >= 2              # per-symbol detail stays DEBUG
    msg = warnings[-1].getMessage()
    assert "1/1" in msg                          # n_failed/n_total
    assert "last_error_type=ValueError" in msg
    assert "action=retry_next_cycle" in msg
    assert "consecutive_failures=2" in msg       # streak grows across cycles


def test_successful_cycle_clears_last_error_and_streak(caplog, monkeypatch):
    force_open_phase(monkeypatch)
    responses = {"AAA.NS": FakeResp(good_payload("AAA.NS"))}
    p = make_poller(monkeypatch, ["AAA.NS"], dict(responses))
    with caplog.at_level(logging.DEBUG):
        p.poll_once()                            # healthy cycle
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []                        # no failure -> no warning
    assert p.last_error is None


def forming_only_payload() -> dict:
    """HTTP-200-shaped payload where every bar is still forming (stamps in the
    future) => parse_last_bar returns None for the whole batch."""
    now_epoch = int(dt.datetime.now(tz=ZoneInfo("UTC")).timestamp()) + 3600
    return {"chart": {"result": [{
        "meta": {"symbol": "X"},
        "timestamp": [now_epoch, now_epoch + 300],
        "indicators": {"quote": [{
            "open": [1.0, 1.0], "high": [1.0, 1.0],
            "low": [1.0, 1.0], "close": [1.0, 1.0], "volume": [0, 0],
        }]},
    }]}}


def test_http_200_with_zero_valid_bars_is_a_failed_cycle(monkeypatch):
    force_open_phase(monkeypatch)
    p = make_poller(monkeypatch, ["AAA.NS"],
                    {"AAA.NS": FakeResp(forming_only_payload())})
    updated, failed = p.poll_once()
    assert updated == 0 and failed == 1          # NOT a success despite HTTP 200
    assert p.last_error is None                  # nothing raised; just no bars
