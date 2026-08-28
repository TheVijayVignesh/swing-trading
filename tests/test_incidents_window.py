"""incidents_24h window: [now − 24h, now], boundary INCLUSIVE, all sessions.

The system block counts incidents across ALL sessions (no scoping filter) —
that is the pre-existing semantic under test; per-session views are separate
endpoints.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import Request

from sts.api.routes_api import _count_incidents_since, _system_block
from sts.config import SessionConfig
from sts.storage.db import init_db
from sts.storage.repos import SessionRepo, TradingRepo

HOUR = timedelta(hours=1)


def _seed_incident(conn, repo, dt_utc):
    repo.record_incident("WARN", "FEED_STALE", ts=dt_utc)


def test_23h_included(tmp_path):
    conn = init_db(str(tmp_path / "j.db"))
    sid = SessionRepo(conn).create_session(SessionConfig(name="a", capital_initial=1000.0))
    now = datetime.now(timezone.utc)
    _seed_incident(conn, TradingRepo(conn, sid), now - 23 * HOUR)
    cutoff = (now - 24 * HOUR).isoformat()
    assert _count_incidents_since(conn, cutoff) == 1


def test_25h_excluded(tmp_path):
    conn = init_db(str(tmp_path / "j.db"))
    sid = SessionRepo(conn).create_session(SessionConfig(name="b", capital_initial=1000.0))
    now = datetime.now(timezone.utc)
    _seed_incident(conn, TradingRepo(conn, sid), now - 25 * HOUR)
    cutoff = (now - 24 * HOUR).isoformat()
    assert _count_incidents_since(conn, cutoff) == 0


def test_exactly_24h_boundary_inclusive(tmp_path):
    conn = init_db(str(tmp_path / "j.db"))
    sid = SessionRepo(conn).create_session(SessionConfig(name="c", capital_initial=1000.0))
    fixed_now = datetime(2026, 8, 26, 7, 0, tzinfo=timezone.utc)
    _seed_incident(conn, TradingRepo(conn, sid), fixed_now - 24 * HOUR)
    # deterministic: evaluate against the SAME fixed now the row was seeded from
    assert _count_incidents_since(conn, (fixed_now - 24 * HOUR).isoformat()) == 1


def test_mixed_ages_counts_only_window(tmp_path):
    conn = init_db(str(tmp_path / "j.db"))
    sid = SessionRepo(conn).create_session(SessionConfig(name="d", capital_initial=1000.0))
    repo = TradingRepo(conn, sid)
    now = datetime.now(timezone.utc)
    for hours in (1, 23, 24.5, 48):
        _seed_incident(conn, repo, now - timedelta(hours=hours))
    assert _count_incidents_since(conn, (now - 24 * HOUR).isoformat()) == 2


def test_system_block_counts_all_sessions(tmp_path):
    conn = init_db(str(tmp_path / "j.db"))
    srepo = SessionRepo(conn)
    now = datetime.now(timezone.utc)
    for name in ("s1", "s2"):
        sid = srepo.create_session(SessionConfig(name=name, capital_initial=1000.0))
        _seed_incident(conn, TradingRepo(conn, sid), now - 2 * HOUR)

    class _FakeRequest(Request):
        pass  # never instantiated; only .app.state is touched

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        marketdata=SimpleNamespace(feed_status=lambda: "CLOSED", last_tick_age_s=None),
        conn=conn,
        lab=SimpleNamespace(sessions=srepo),
    )))
    block = _system_block(request)
    assert block["incidents_24h"] == 2
    assert datetime.fromisoformat(block["heartbeat"]).tzinfo is not None
