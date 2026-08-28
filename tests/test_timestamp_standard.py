"""Timestamp-standard lock-in tests (see docs/TIMESTAMP_STANDARD.md).

Complements tests/test_timezone_fix.py (converter units + migration v5) with:
- exact-instant persistence through the funnel writer at market-critical times,
- UTC midnight crossing + ORDER BY ts DESC chronology,
- tz-safety: aware inputs pass through untouched; ANY fixed offset converts
  correctly (India has no DST, so a fixed +05:30 rule must be date-independent),
- API presentation contract: the server returns canonical tz-aware UTC ISO
  strings VERBATIM; every IST rendering happens client-side in JS.
"""
from __future__ import annotations

import datetime as dt
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import sts.api
from sts.api.app import create_app
from sts.config import SessionConfig
from sts.contracts import ScanFunnel
from sts.marketdata.service import MarketDataService
from sts.storage.db import init_db
from sts.storage.repos import SessionRepo, TradingRepo, iso_utc, utc_iso

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture()
def conn(tmp_path):
    c = init_db(str(tmp_path / "journal.db"))
    yield c
    c.close()


@pytest.fixture()
def repo(conn):
    sid = SessionRepo(conn).create_session(
        SessionConfig(name="std", capital_initial=100000.0))
    return TradingRepo(conn, sid)


def _funnel_ts(conn, sid: str) -> str:
    return conn.execute(
        "SELECT ts FROM scan_funnels WHERE session_id=? ORDER BY id DESC LIMIT 1",
        (sid,)).fetchone()[0]


# ------------------------------------------------------------- instant fidelity
class TestInstantRoundTrip:
    def test_0915_ist_market_open_persists_as_0345z(self, repo, conn):
        original = datetime(2026, 8, 26, 9, 15)
        repo.record_funnel(ScanFunnel(ts=original))
        stored = _funnel_ts(conn, repo.session_id)
        assert stored == "2026-08-26T03:45:00+00:00"
        readback = datetime.fromisoformat(stored)
        # equality of AWARE datetimes compares instants, not wall clocks
        assert readback.tzinfo is not None
        assert readback == datetime(2026, 8, 26, 3, 45, tzinfo=timezone.utc)

    def test_0930_ist_first_bar_close_persists_as_0400z(self, repo, conn):
        original = datetime(2026, 8, 26, 9, 30)
        repo.record_funnel(ScanFunnel(ts=original))
        stored = _funnel_ts(conn, repo.session_id)
        assert stored == "2026-08-26T04:00:00+00:00"
        readback = datetime.fromisoformat(stored)
        assert readback == datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)
        # reconstructing the IST wall clock gives back the exact original
        assert readback.astimezone(IST).replace(tzinfo=None) == original


# ------------------------------------------------------------ midnight crossing
class TestMidnightCrossing:
    def test_2359_ist_stays_same_utc_date(self):
        assert utc_iso(datetime(2026, 8, 26, 23, 59)) == "2026-08-26T18:29:00+00:00"

    def test_0005_ist_lands_on_previous_utc_date(self):
        assert utc_iso(datetime(2026, 8, 27, 0, 5)) == "2026-08-26T18:35:00+00:00"

    def test_order_by_ts_desc_chronological_across_midnight(self, repo, conn):
        # inserted deliberately out of chronological order
        late_night = datetime(2026, 8, 26, 23, 59)   # -> 18:29Z on Aug 26
        just_past_midnight = datetime(2026, 8, 27, 0, 5)  # -> 18:35Z on Aug 26
        next_morning = datetime(2026, 8, 27, 9, 15)  # -> 03:45Z on Aug 27
        repo.record_funnel(ScanFunnel(ts=next_morning))
        repo.record_funnel(ScanFunnel(ts=late_night))
        repo.record_funnel(ScanFunnel(ts=just_past_midnight))
        rows = conn.execute(
            "SELECT ts FROM scan_funnels WHERE session_id=? ORDER BY ts DESC",
            (repo.session_id,)).fetchall()
        assert [r[0] for r in rows] == [
            "2026-08-27T03:45:00+00:00",   # next_morning
            "2026-08-26T18:35:00+00:00",   # just_past_midnight (UTC date rolled BACK)
            "2026-08-26T18:29:00+00:00",   # late_night
        ]
        tss = [datetime.fromisoformat(r[0]) for r in rows]
        assert tss == sorted(tss, reverse=True)


# ---------------------------------------------------------------- tz-safety
class TestTimezoneSafety:
    def test_aware_utc_passes_through_unchanged(self, repo, conn):
        aware = datetime(2026, 8, 26, 3, 45, tzinfo=timezone.utc)
        repo.record_funnel(ScanFunnel(ts=aware))
        assert _funnel_ts(conn, repo.session_id) == "2026-08-26T03:45:00+00:00"

    def test_fixed_offset_plus_0530_converts_correctly(self, repo, conn):
        aware = datetime(2026, 8, 26, 9, 15, tzinfo=IST)
        repo.record_funnel(ScanFunnel(ts=aware))
        assert _funnel_ts(conn, repo.session_id) == "2026-08-26T03:45:00+00:00"

    def test_arbitrary_fixed_offset_is_honored(self):
        # +02:00 input must NOT be treated as IST — offsets convert generically
        plus2 = timezone(timedelta(hours=2))
        assert utc_iso(datetime(2026, 8, 26, 5, 45, tzinfo=plus2)) == \
            "2026-08-26T03:45:00+00:00"

    def test_india_has_no_dst_date_independence(self):
        for day in ((2026, 1, 15), (2026, 7, 15)):   # winter & summer
            assert utc_iso(datetime(*day, 9, 15)) == f"{day[0]:04d}-{day[1]:02d}-{day[2]:02d}T03:45:00+00:00"

    def test_naive_boundary_conventions_differ_by_exactly_ist_offset(self):
        # DOCUMENTS THE CONVENTION PAIR: utc_iso reads naive input as IST
        # (runner frame); iso_utc reads naive input as UTC (wall-clock 'now').
        naive = datetime(2026, 8, 26, 9, 15)
        delta = datetime.fromisoformat(iso_utc(naive)) - \
            datetime.fromisoformat(utc_iso(naive))
        assert delta == timedelta(hours=5, minutes=30)


# --------------------------------------------------- API presentation contract
class _Mgr:
    """Minimal lab-manager surface needed by GET /api/sessions/{id}."""

    def __init__(self, conn):
        self.sessions = SessionRepo(conn)
        self.runners = {}
        self.graphs = {}


def _client(conn, tmp_path) -> httpx.AsyncClient:
    md = MarketDataService(["AAA"], poller=None,
                           clock=lambda: dt.datetime.now(dt.timezone.utc),
                           daily_dir=tmp_path, poll_seconds=60)
    app = create_app(_Mgr(conn), md, conn, recover_on_startup=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


class TestApiPresentationCanonicalUtc:
    async def test_api_returns_stored_utc_verbatim(self, conn, repo, tmp_path):
        # one snapshot at an aware-UTC midnight-edge instant + one funnel from
        # the naive-IST runner frame; explicit journal ts keeps both instants
        # deterministic (API exposes the JOURNAL ts as funnel_latest.ts)
        repo.record_account_snapshot(
            datetime(2026, 8, 26, 18, 35, tzinfo=timezone.utc),
            cash=100000.0, invested=0.0, unrealized=0.0, realized=0.0,
            equity=100000.0, hwm=100000.0, drawdown=0.0)
        repo.record_funnel(ScanFunnel(ts=datetime(2026, 8, 26, 9, 15)),
                           ts=datetime(2026, 8, 26, 3, 45, tzinfo=timezone.utc))
        async with _client(conn, tmp_path) as client:
            resp = await client.get(f"/api/sessions/{repo.session_id}")
        assert resp.status_code == 200
        body = resp.json()
        # persisted true UTC returned byte-for-byte; NO server-side IST conversion
        assert body["funnel_latest"]["ts"] == "2026-08-26T03:45:00+00:00"
        assert body["equity_curve"][0][0] == "2026-08-26T18:35:00+00:00"

    async def test_every_api_timestamp_is_tz_aware_utc(self, conn, repo, tmp_path):
        repo.record_account_snapshot(
            datetime(2026, 8, 26, 3, 45, tzinfo=timezone.utc),
            cash=100000.0, invested=0.0, unrealized=0.0, realized=0.0,
            equity=100000.0, hwm=100000.0, drawdown=0.0)
        repo.record_funnel(ScanFunnel(ts=datetime(2026, 8, 26, 9, 30)))
        async with _client(conn, tmp_path) as client:
            detail = (await client.get(f"/api/sessions/{repo.session_id}")).json()
            health = (await client.get("/api/system/health")).json()
        blob = repr(detail) + repr(health)
        assert "+05:30" not in blob          # server never renders IST
        assert body_ts_fields_ok(detail)
        assert health["heartbeat"].endswith("+00:00")


def body_ts_fields_ok(detail: dict) -> bool:
    ts_values = [detail["funnel_latest"]["ts"], detail["equity_curve"][0][0],
                 detail["drawdown_curve"][0][0]]
    return all(str(v).endswith("+00:00") for v in ts_values)


# ------------------------------------------------- JS humanizer documentation
class TestJsHumanizersDocumented:
    """The dashboard converts UTC->viewer-local ONLY in browser JS. These
    assertions pin the formatter names/locations cited in
    docs/TIMESTAMP_STANDARD.md so the docs cannot silently rot."""

    def _js(self, name: str) -> str:
        return (Path(sts.api.__file__).parent / "static" / "js" / name).read_text()

    def test_reltime_parses_iso_and_hydrates_data_rel_nodes(self):
        src = self._js("lab.js")
        assert "function relTime(" in src                    # lab.js relTime
        assert "new Date(iso).getTime()" in src              # parses +00:00 ISO
        assert '"[data-rel]"' in src or "'[data-rel]'" in src or "[data-rel]" in src

    def test_charts_fmt_time_uses_browser_locale_only(self):
        src = self._js("charts.js")
        assert "function fmtTime(" in src
        assert "toLocaleTimeString" in src                   # viewer-local render
