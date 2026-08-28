"""Canonical timestamp standard + scan_funnels IST-as-UTC repair (v5).

Standard (docs/API_CONTRACT.md — Timestamp standard): absolute instants are
persisted as tz-aware UTC ISO strings. Naive datetimes are the runners'
internal IST clock convention -> true instant = naive − 5:30 UTC.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sts.config import SessionConfig
from sts.contracts import ScanFunnel
from sts.storage.db import SCHEMA_VERSION, init_db
from sts.storage.migrations import MIGRATIONS, migrate
from sts.storage.repos import SessionRepo, TradingRepo, utc_iso

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture()
def conn(tmp_path):
    c = init_db(str(tmp_path / "journal.db"))
    yield c
    c.close()


@pytest.fixture()
def repo(conn):
    sid = SessionRepo(conn).create_session(
        SessionConfig(name="tz", capital_initial=100000.0))
    return TradingRepo(conn, sid)


def _stored_ts(conn) -> str:
    return conn.execute("SELECT ts FROM scan_funnels").fetchone()[0]


def _payload_ts(conn) -> str:
    row = conn.execute(
        "SELECT detail_json FROM session_events WHERE event='SCAN_FUNNEL'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0])["ts"]


# ------------------------------------------------------- utc_iso conversions
class TestUtcIso:
    def test_market_open_0915_ist_is_0345z(self):
        assert utc_iso(datetime(2026, 8, 26, 9, 15)) == "2026-08-26T03:45:00+00:00"

    def test_first_bar_close_0930_ist_is_0400z(self):
        assert utc_iso(datetime(2026, 8, 26, 9, 30)) == "2026-08-26T04:00:00+00:00"

    def test_midnight_boundary_rolls_date_back(self):
        # 00:15 IST is still the previous UTC day
        assert utc_iso(datetime(2026, 8, 26, 0, 15)) == "2026-08-25T18:45:00+00:00"

    def test_aware_utc_passes_through(self):
        dt = datetime(2026, 8, 26, 3, 45, tzinfo=timezone.utc)
        assert utc_iso(dt) == "2026-08-26T03:45:00+00:00"

    def test_aware_non_utc_converts(self):
        dt = datetime(2026, 8, 26, 9, 15, tzinfo=IST)
        assert utc_iso(dt) == "2026-08-26T03:45:00+00:00"

    def test_microseconds_preserved(self):
        out = utc_iso(datetime(2026, 8, 26, 11, 13, 9, 858884))
        assert out == "2026-08-26T05:43:09.858884+00:00"

    def test_none(self):
        assert utc_iso(None) is None


# ------------------------------------------------------- funnel writer paths
class TestFunnelWriter:
    def test_naive_ist_funnel_ts_persisted_as_true_utc_instant(self, repo, conn):
        f = ScanFunnel(ts=datetime(2026, 8, 26, 11, 13, 9), scanned=200, eligible=197)
        repo.record_funnel(f)
        expected = "2026-08-26T05:43:09+00:00"
        assert _stored_ts(conn) == expected          # scan_funnels.ts column
        assert _payload_ts(conn) == expected         # SCAN_FUNNEL journal payload

    def test_read_back_reconstructs_instant(self, repo, conn):
        f = ScanFunnel(ts=datetime(2026, 8, 26, 9, 15))
        repo.record_funnel(f)
        stored = datetime.fromisoformat(_stored_ts(conn))
        assert stored.tzinfo is not None             # aware on read-back
        assert stored.astimezone(IST).replace(tzinfo=None) == datetime(2026, 8, 26, 9, 15)

    def test_aware_funnel_ts_unaffected(self, repo, conn):
        aware = datetime(2026, 8, 26, 3, 45, tzinfo=timezone.utc)
        f = ScanFunnel(ts=aware)
        repo.record_funnel(f)
        assert _stored_ts(conn) == "2026-08-26T03:45:00+00:00"

    def test_upsert_funnel_payload_also_fixed(self, repo, conn):
        f = ScanFunnel(ts=datetime(2026, 8, 26, 12, 31, 15, 875983), scanned=0)
        repo.upsert_funnel(f, explanation="no data")
        assert _payload_ts(conn) == "2026-08-26T07:01:15.875983+00:00"

    def test_order_by_ts_desc_chronological_across_mixed_legacy_corrected(self,
                                                                          repo, conn):
        # legacy corrupt row (naive IST stamped as-if-UTC) + rows written by
        # the fixed path; after v5 repair, ts DESC must be chronological.
        conn.execute(
            "INSERT INTO session_events(session_id, ts, event, actor, detail_json)"
            " VALUES(?, '2026-08-25T15:40:10.161427+00:00', 'SCAN_FUNNEL','runner','{}')",
            (repo.session_id,))
        conn.execute(
            "INSERT INTO scan_funnels(session_id, ts, scanned)"
            " VALUES(?, '2026-08-25T21:10:10.161427+00:00', 200)",
            (repo.session_id,))
        repo.record_funnel(ScanFunnel(ts=datetime(2026, 8, 26, 3, 50)))   # earlier true UTC
        repo.record_funnel(ScanFunnel(ts=datetime(2026, 8, 26, 4, 55)))   # later true UTC
        from sts.storage.migrations import migrate as _migrate
        conn.execute("DROP TABLE scan_funnels_tz_audit")
        conn.execute("DELETE FROM _schema_migrations WHERE version=5")
        conn.commit()
        _migrate(conn)
        rows = conn.execute(
            "SELECT ts FROM scan_funnels WHERE session_id=? ORDER BY ts DESC",
            (repo.session_id,)).fetchall()
        tss = [datetime.fromisoformat(r[0]) for r in rows]
        assert tss == sorted(tss, reverse=True)
        assert rows[0][0] == "2026-08-25T23:25:00+00:00"      # 04:55 IST
        assert rows[-1][0] == "2026-08-25T15:40:10.161427+00:00"  # repaired row


# ------------------------------------------------------------------ migration
def _drop_v5(conn: sqlite3.Connection) -> None:
    """Rewind a fresh DB to pre-v5 state so migrate() re-applies v5."""
    conn.execute("DROP TABLE IF EXISTS scan_funnels_tz_audit")
    conn.execute("DELETE FROM _schema_migrations WHERE version=5")
    conn.commit()


class TestMigrationV5:
    def _seed(self, conn, sid):
        now = datetime.now(timezone.utc)
        # clean grid row: funnel ts == journal event ts exactly (both correct UTC)
        clean_evt = "2026-08-26T07:05:00+00:00"
        # corrupt watchdog row: naive IST wall clock stamped as-if-UTC;
        # journal event carries the TRUE UTC with identical microsecond fraction
        corrupt_funnel = "2026-08-26T12:31:15.875983+00:00"
        corrupt_event = "2026-08-26T07:01:15.875983+00:00"
        for evt in (clean_evt, corrupt_event):
            conn.execute(
                "INSERT INTO session_events(session_id, ts, event, actor, detail_json)"
                " VALUES(?,?, 'SCAN_FUNNEL','runner','{}')", (sid, evt))
        conn.execute(
            "INSERT INTO scan_funnels(session_id, ts, scanned) VALUES(?,?,200)",
            (sid, clean_evt))
        conn.execute(
            "INSERT INTO scan_funnels(session_id, ts, scanned) VALUES(?,?,0)",
            (sid, corrupt_funnel))
        # future-dated but NO confirming evidence -> must stay UNCORRECTED
        future = (now + timedelta(hours=2)).isoformat()
        conn.execute(
            "INSERT INTO scan_funnels(session_id, ts, scanned) VALUES(?,?,1)",
            (sid, future))
        conn.commit()
        return {"clean": clean_evt, "corrupt_old": corrupt_funnel,
                "corrupt_new": "2026-08-26T07:01:15.875983+00:00",
                "future": future}

    def test_migration_corrects_confirmed_preserves_clean_lists_uncorrected(self, tmp_path):
        conn = init_db(str(tmp_path / "j.db"))
        _drop_v5(conn)
        sid = SessionRepo(conn).create_session(
            SessionConfig(name="m", capital_initial=100000.0))
        seed = self._seed(conn, sid)

        applied = migrate(conn)
        assert applied == [5]
        assert SCHEMA_VERSION == len(MIGRATIONS)

        rows = {r["ts"] for r in conn.execute("SELECT ts FROM scan_funnels")}
        # corrupt row shifted back exactly +05:30 -> the journal-event instant
        assert "2026-08-26T07:01:15.875983+00:00" in rows
        # clean row untouched
        assert seed["clean"] in rows
        # unconfirmed future row left as-is
        assert seed["future"] in rows

        audit = {(r["scan_funnel_id"], r["old_ts"], r["new_ts"], r["method"])
                 for r in conn.execute("SELECT * FROM scan_funnels_tz_audit")}
        methods = {m for *_, m in audit}
        assert methods == {"IST_AS_UTC_CORRECTED", "UNCORRECTED"}
        corrected = [a for a in audit if a[3] == "IST_AS_UTC_CORRECTED"]
        assert len(corrected) == 1
        assert corrected[0][1] == seed["corrupt_old"]
        assert corrected[0][2] == "2026-08-26T07:01:15.875983+00:00"

    def test_migration_idempotent_re_run(self, tmp_path):
        conn = init_db(str(tmp_path / "j.db"))
        _drop_v5(conn)
        sid = SessionRepo(conn).create_session(
            SessionConfig(name="m2", capital_initial=100000.0))
        self._seed(conn, sid)
        migrate(conn)
        after_first = conn.execute("SELECT id, ts FROM scan_funnels").fetchall()
        audit_count_first = conn.execute(
            "SELECT COUNT(*) FROM scan_funnels_tz_audit").fetchone()[0]
        assert audit_count_first == 2

        assert migrate(conn) == []
        after_second = conn.execute("SELECT id, ts FROM scan_funnels").fetchall()
        assert [tuple(r) for r in after_first] == [tuple(r) for r in after_second]
        assert conn.execute(
            "SELECT COUNT(*) FROM scan_funnels_tz_audit").fetchone()[0] == 2

    def test_migration_skips_audited_uncorrected_rows_once_future_passes(self, tmp_path):
        conn = init_db(str(tmp_path / "j.db"))
        _drop_v5(conn)
        sid = SessionRepo(conn).create_session(
            SessionConfig(name="m3", capital_initial=100000.0))
        seed = self._seed(conn, sid)
        migrate(conn)
        # simulate time passing beyond the future-dated row: it stays untouched
        # because its funnel id is already audited
        assert conn.execute(
            "SELECT ts FROM scan_funnels WHERE ts=?", (seed["future"],)).fetchone()


# ------------------------------------------------------------------ migration v6
def _drop_v6(conn: sqlite3.Connection) -> None:
    """Rewind past v6 only (pure data repair: no DDL to drop) so migrate()
    re-applies it over rows inserted after the fresh init."""
    conn.execute("DELETE FROM _schema_migrations WHERE version=6")
    conn.commit()


class TestMigrationV6:
    """v6 = residual IST-as-UTC repair for rows written AFTER v5 ran by the
    stale pre-fix server process. Same shared rule/guard as v5."""

    def test_repairs_post_v5_stale_process_row_once(self, tmp_path):
        # Post-v5 watchdog heartbeat pattern: funnel ts is naive IST wall
        # time mislabeled +00:00; the paired same-session SCAN_FUNNEL journal
        # event carries the TRUE UTC with identical microseconds and its
        # payload echoes the corrupt funnel ts.
        conn = init_db(str(tmp_path / "j.db"))
        sid = SessionRepo(conn).create_session(
            SessionConfig(name="v6", capital_initial=100000.0))
        corrupt_funnel = "2026-08-26T13:01:15.911947+00:00"
        true_event = "2026-08-26T07:31:15.911947+00:00"
        conn.execute(
            "INSERT INTO session_events(session_id, ts, event, actor, detail_json)"
            " VALUES(?,?, 'SCAN_FUNNEL','runner',?)",
            (sid, true_event, json.dumps({"ts": corrupt_funnel})))
        conn.execute(
            "INSERT INTO scan_funnels(session_id, ts, scanned) VALUES(?,?,0)",
            (sid, corrupt_funnel))
        conn.commit()
        _drop_v6(conn)

        assert migrate(conn) == [6]
        assert SCHEMA_VERSION == len(MIGRATIONS)

        fixed = conn.execute("SELECT ts FROM scan_funnels").fetchone()[0]
        assert fixed == true_event               # shifted back exactly 05:30

        # journal payload left exactly as written (v5 precedent)
        payload = json.loads(conn.execute(
            "SELECT detail_json FROM session_events"
            " WHERE event='SCAN_FUNNEL'").fetchone()[0])
        assert payload["ts"] == corrupt_funnel

        audit = [dict(r) for r in conn.execute("SELECT * FROM scan_funnels_tz_audit")]
        assert len(audit) == 1                   # exactly one new audit row
        assert audit[0]["method"] == "IST_AS_UTC_CORRECTED"
        assert audit[0]["old_ts"] == corrupt_funnel
        assert audit[0]["new_ts"] == true_event

        # idempotent re-run: audit-row existence guard => no-op
        assert migrate(conn) == []
        assert conn.execute(
            "SELECT COUNT(*) FROM scan_funnels_tz_audit").fetchone()[0] == 1

    def test_clean_bar_close_row_untouched(self, tmp_path):
        conn = init_db(str(tmp_path / "j.db"))
        sid = SessionRepo(conn).create_session(
            SessionConfig(name="v6b", capital_initial=100000.0))
        clean = "2026-08-25T09:20:00+00:00"      # true UTC bar-close, past
        conn.execute(
            "INSERT INTO session_events(session_id, ts, event, actor, detail_json)"
            " VALUES(?,?, 'SCAN_FUNNEL','runner','{}')", (sid, clean))
        conn.execute(
            "INSERT INTO scan_funnels(session_id, ts, scanned) VALUES(?,?,200)",
            (sid, clean))
        conn.commit()
        _drop_v6(conn)

        assert migrate(conn) == [6]
        assert conn.execute("SELECT ts FROM scan_funnels").fetchone()[0] == clean
        assert conn.execute(
            "SELECT COUNT(*) FROM scan_funnels_tz_audit").fetchone()[0] == 0

    def test_no_double_repair_when_v5_and_v6_apply_in_same_pass(self, tmp_path):
        conn = init_db(str(tmp_path / "j.db"))
        sid = SessionRepo(conn).create_session(
            SessionConfig(name="v6c", capital_initial=100000.0))
        corrupt_funnel = "2026-08-26T12:31:15.875983+00:00"
        true_event = "2026-08-26T07:01:15.875983+00:00"
        conn.execute(
            "INSERT INTO session_events(session_id, ts, event, actor, detail_json)"
            " VALUES(?,?, 'SCAN_FUNNEL','runner','{}')", (sid, true_event))
        conn.execute(
            "INSERT INTO scan_funnels(session_id, ts, scanned) VALUES(?,?,0)",
            (sid, corrupt_funnel))
        conn.commit()
        _drop_v5(conn)
        _drop_v6(conn)

        assert migrate(conn) == [5, 6]
        # v5 audited+fixed the row; v6 must see the audit marker and skip it
        assert conn.execute("SELECT ts FROM scan_funnels").fetchone()[0] == true_event
        assert conn.execute(
            "SELECT COUNT(*) FROM scan_funnels_tz_audit").fetchone()[0] == 1
