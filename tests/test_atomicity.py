"""Atomicity / crash-recovery — diagnostic finding #6 (2026-08-26).

Invariant under test (ARCHITECTURE_V1.2 §4 durability intent):

  * intent + order rows commit together or not at all;
  * fills chain to their order;
  * a crash mid-chain leaves recoverable state — no orphan order without
    intent; an orphan INTENT alone is acceptable only as a documented
    REJECTED/DEFERRED decision row.

Mechanism: transaction-scoped commit suppression. While a TradingRepo.
transaction() is open on a connection, every repo write auto-defers its
commit REGARDLESS of the passed `commit` kwarg (thread-local, conn-scoped
depth counter) and the outermost transaction exit issues the single COMMIT
(or ROLLBACK). Nested transaction() is reference-counted.

Cases: A success chain, B mid-chain failure rollback, C simulated crash +
recovery-manager health, D idempotency retry under transaction scope,
E cross-session independence, F nested-transaction refcounting.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3

import pytest

from sts.brokers.errors import BrokerTimeoutError, OrderStateError
from sts.brokers.paper import OrderView
from sts.config import SessionConfig, to_yaml
from sts.contracts import FillRecord, OrderStatus, OrderType, Side, TradeIntent
from sts.execution.order_manager import OrderManager
from sts.lab.factory import RepoSink
from sts.lab.manager import LabManager
from sts.storage.db import init_db
from sts.storage.repos import SessionRepo, TradingRepo

TS = dt.datetime(2026, 8, 25, 10, 0, tzinfo=dt.timezone.utc)
SID = "sessatomic0001"
SID_B = "sessatomic0002"


@pytest.fixture()
def conn(tmp_path):
    c = init_db(str(tmp_path / "journal.db"))
    # realistic rows: RUNNING sessions carry a parseable config (boot recovery
    # re-reads config_yaml for any session it finds RUNNING post-crash)
    c.execute("INSERT INTO sessions(id, name, status, mode, capital_initial,"
              " config_yaml) VALUES(?,?,?,?,?,?)",
              (SID, "atomic-a", "RUNNING", "paper", 25_000.0,
               to_yaml(SessionConfig(name="atomic-a", capital_initial=25_000.0))))
    c.execute("INSERT INTO sessions(id, name, status, mode, capital_initial,"
              " config_yaml) VALUES(?,?,?,?,?,?)",
              (SID_B, "atomic-b", "RUNNING", "paper", 25_000.0,
               to_yaml(SessionConfig(name="atomic-b", capital_initial=25_000.0))))
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def repo(conn):
    return TradingRepo(conn, SID)


def _intent_row(decision: str = "ENTER") -> dict:
    return {
        "ts": TS.isoformat(), "symbol": "RELIANCE",
        "market_state_ref": json.dumps({"ts": TS.isoformat()}),
        "feature_vector_json": "{}", "signals_json": "[]",
        "ml_score": None, "ml_prob": None,
        "risk_checks_json": "[]", "decision": decision,
        "rejection_reason": "", "portfolio_snapshot_json": "{}",
        "versions_json": "{}",
    }


def _order_view(correlation_id: str, broker_order_id: str = "brd-1") -> OrderView:
    return OrderView(
        order_id=broker_order_id, session_id=SID, symbol="RELIANCE",
        side=Side.BUY, order_type=OrderType.LIMIT, qty=10, limit_price=100.0,
        status=OrderStatus.WORKING, correlation_id=correlation_id,
        created_ts=TS,
    )


def _counts(conn, table: str, sid: str = SID) -> int:
    return conn.execute(
        f"SELECT COUNT(*) n FROM {table} WHERE session_id=?", (sid,)
    ).fetchone()["n"]


# --------------------------------------------------------------------- Case A
class TestCaseASuccessChain:
    def test_intent_and_order_both_persist(self, conn, repo):
        """Runner-like chain: insert_intent then broker sink mints the order
        row with its default commit=True — both must land at txn exit."""
        sink = RepoSink(repo)
        with repo.transaction():
            iid = repo.insert_intent(_intent_row())  # default commit=True: suppressed
            sink.current_intent_id = iid
            sink.on_order(_order_view(f"{SID}:ENTER:RELIANCE:{TS.isoformat()}"))
        assert _counts(conn, "intents") == 1
        assert _counts(conn, "orders") == 1
        order = conn.execute("SELECT * FROM orders").fetchone()
        assert order["intent_id"] == iid
        assert order["session_id"] == SID

    def test_fill_chains_to_order(self, conn, repo):
        sink = RepoSink(repo)
        with repo.transaction():
            iid = repo.insert_intent(_intent_row())
            sink.current_intent_id = iid
            ov = _order_view(f"{SID}:ENTER:RELIANCE:{TS.isoformat()}")
            sink.on_order(ov)
            fill = FillRecord(
                order_id=ov.order_id, session_id=SID, symbol="RELIANCE",
                side=Side.BUY, px=100.05, qty=10, ts=TS,
                cost_breakdown={"total": 1.5},
            )
            sink.on_fill(fill)
        row = conn.execute(
            "SELECT o.id oid, f.id fid FROM orders o JOIN fills f ON f.order_id=o.id"
        ).fetchone()
        assert row is not None
        assert row["oid"] and row["fid"]


# --------------------------------------------------------------------- Case B
class TestCaseBMidchainFailure:
    def test_order_failure_rolls_back_intent_too(self, conn, repo, monkeypatch):
        """insert_order raises inside the transaction (with an EXPLICIT
        commit=True already fired by insert_intent) — NEITHER row persists."""
        def boom(*a, **k):
            raise RuntimeError("simulated persistence failure")
        monkeypatch.setattr(TradingRepo, "insert_order", boom)

        sink = RepoSink(repo)
        with pytest.raises(RuntimeError, match="simulated"):
            with repo.transaction():
                iid = repo.insert_intent(_intent_row(), commit=True)
                assert _counts(conn, "intents") == 1  # visible inside the txn
                sink.current_intent_id = iid
                sink.on_order(_order_view(f"{SID}:ENTER:X"))
        assert _counts(conn, "intents") == 0
        assert _counts(conn, "orders") == 0

    def test_timeout_unknown_then_resubmit_no_duplicate_order(self, conn, repo):
        """Broker-side UNKNOWN playbook against a real journal: the chaos knob
        records the attempt as UNKNOWN and raises BEFORE touching the broker;
        resubmit() is legal ONLY from UNKNOWN and persists exactly ONE order
        row keyed by the correlation id; any later duplicate submission
        returns the SAME order id without a second broker call or row."""
        class _RecordingBroker:
            def __init__(self) -> None:
                self.place_calls = 0

            def place_order(self, session_id, intent):
                self.place_calls += 1
                return f"brd-{self.place_calls}"

            def cancel_order(self, session_id, order_id):
                return True

            def modify_order(self, session_id, order_id, new_limit):
                return f"{order_id}-m"

            def get_account_state(self, session_id): ...
            def get_positions(self, session_id): ...
            def on_bar(self, session_id, bar): ...
            def capabilities(self): ...

        stub = _RecordingBroker()
        om = OrderManager(stub)
        sink = RepoSink(repo)
        corr = f"{SID}:ENTER:RELIANCE:{TS.isoformat()}"
        intent = TradeIntent(session_id=SID, ts=TS, symbol="RELIANCE",
                             side=Side.BUY, order_type=OrderType.LIMIT,
                             qty=10, limit_price=100.0, correlation_id=corr)

        # Attempt 1: chaos timeout inside a runner-like txn — exception caught,
        # intent downgraded to a journaled REJECT, NO order row ever minted.
        om.fail_next_place = True
        with repo.transaction():
            iid = repo.insert_intent(_intent_row(), commit=False)
            sink.current_intent_id = iid
            try:
                om.place_order(SID, intent)
            except BrokerTimeoutError:
                repo.update_intent_decision(iid, "REJECT",
                                            rejection_reason="ORDER_REJECTED:timeout",
                                            commit=False)
        assert stub.place_calls == 0              # timeout fired BEFORE broker
        assert om.state_of(corr) == "UNKNOWN"
        assert _counts(conn, "orders") == 0
        decisions = {r["decision"] for r in
                     conn.execute("SELECT decision FROM intents").fetchall()}
        assert decisions == {"REJECT"}

        # Recovery: query-before-retry — legal ONLY from UNKNOWN.
        oid = om.resubmit(SID, corr)
        assert om.state_of(corr) == "PLACED"

        # The recovered placement persists exactly ONE row via the sink.
        with repo.transaction():
            iid2 = repo.insert_intent(_intent_row())
            sink.current_intent_id = iid2
            sink.on_order(_order_view(corr, broker_order_id=oid))
        assert _counts(conn, "intents") == 2      # REJECT + recovered ENTER
        assert _counts(conn, "orders") == 1
        order = conn.execute("SELECT * FROM orders").fetchone()
        assert order["idempotency_key"] == corr
        assert order["broker_order_id"] == oid

        # Duplicate submission afterwards: SAME id, no second broker call,
        # no second row; resubmit is now illegal (no longer UNKNOWN).
        assert om.place_order(SID, intent) == oid
        assert stub.place_calls == 1
        assert om.counters["duplicates"] == 1
        assert _counts(conn, "orders") == 1
        with pytest.raises(OrderStateError):
            om.resubmit(SID, corr)


# --------------------------------------------------------------------- Case C
class TestCaseCSimulatedCrash:
    def test_crash_leaves_recoverable_db(self, tmp_path, conn, repo, monkeypatch):
        """Exception propagates out of runner-like code (BaseException path);
        DB must reopen cleanly with zero partial chains and the recovery
        manager must still boot."""
        path = str(tmp_path / "journal.db")

        def crash_mid_chain(self, fields, *, commit=True):
            raise KeyboardInterrupt("simulated hard crash between inserts")

        monkeypatch.setattr(TradingRepo, "insert_order", crash_mid_chain)
        sink = RepoSink(repo)
        try:
            with repo.transaction():
                iid = repo.insert_intent(_intent_row())
                sink.current_intent_id = iid
                sink.on_order(_order_view(f"{SID}:ENTER:CRASH"))
        except KeyboardInterrupt:
            pass

        # reopen fresh (as main.py does on boot)
        conn.close()
        conn2 = init_db(path)
        try:
            assert conn2.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            # zero partial chains: every order has a live intent parent
            orphans = conn2.execute(
                "SELECT COUNT(*) n FROM orders o LEFT JOIN intents i ON i.id=o.intent_id"
                " WHERE o.intent_id IS NOT NULL AND i.id IS NULL"
            ).fetchone()["n"]
            assert orphans == 0
            assert _counts(conn2, "intents") == 0   # whole chain rolled back
            assert _counts(conn2, "orders") == 0

            # recovery manager still works on the recovered journal: the
            # crashed sessions are RUNNING, so boot must respawn them cleanly
            class _StubMarketData:
                symbols = ["RELIANCE"]

                @staticmethod
                def subscribe():
                    return asyncio.Queue()

                @staticmethod
                def unsubscribe(q):
                    return None

                universe_snapshot_id = staticmethod(lambda symbols: "snap-test")

            async def _recover(conn2):
                mgr = LabManager(conn2, _StubMarketData())
                recovered = mgr.recover_on_boot()
                assert sorted(recovered) == sorted([SID, SID_B])
                # cancel the respawned runners before they ever trade
                for tsk in list(mgr.tasks.values()):
                    tsk.cancel()
                    try:
                        await tsk
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                # and can still create sessions post-recovery
                cfg = SessionConfig(name="post-crash", capital_initial=50_000.0)
                return mgr.create_session(cfg)

            new_sid = asyncio.run(_recover(conn2))
            assert SessionRepo(conn2).get_status(new_sid) == "CREATED"
        finally:
            conn2.close()

    def test_crash_after_order_row_minted_before_commit(self, tmp_path, conn, repo):
        """Hard crash AFTER sink.on_order minted the order row but BEFORE the
        outer COMMIT: both rows already exist inside the open transaction, so
        the rollback must discard BOTH — atomicity holds even past mint."""
        path = str(tmp_path / "journal.db")
        sink = RepoSink(repo)
        try:
            with repo.transaction():
                iid = repo.insert_intent(_intent_row())
                sink.current_intent_id = iid
                sink.on_order(_order_view(f"{SID}:ENTER:MINT-CRASH"))
                # decisive pre-crash state: BOTH rows minted but uncommitted
                assert _counts(conn, "intents") == 1
                assert _counts(conn, "orders") == 1
                raise KeyboardInterrupt(
                    "simulated hard crash after order-row mint, before outer COMMIT")
        except KeyboardInterrupt:
            pass

        conn.close()
        conn2 = init_db(path)
        try:
            assert conn2.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            orphans = conn2.execute(
                "SELECT COUNT(*) n FROM orders o LEFT JOIN intents i ON i.id=o.intent_id"
                " WHERE o.intent_id IS NOT NULL AND i.id IS NULL"
            ).fetchone()["n"]
            assert orphans == 0
            assert _counts(conn2, "intents") == 0   # minted intent rolled back
            assert _counts(conn2, "orders") == 0    # minted order rolled back too
        finally:
            conn2.close()


# --------------------------------------------------------------------- Case D
class TestCaseDIdempotency:
    def test_duplicate_correlation_retry_no_duplicate_order(self, conn, repo):
        """Retry with the same correlation_id after a committed first attempt:
        UNIQUE(session_id, idempotency_key) fires INSIDE the transaction, the
        same txn downgrades to a journaled NOOP, and no duplicate order row
        exists."""
        corr = f"{SID}:ENTER:RELIANCE:{TS.isoformat()}"
        sink = RepoSink(repo)

        with repo.transaction():  # first attempt commits cleanly
            iid = repo.insert_intent(_intent_row())
            sink.current_intent_id = iid
            sink.on_order(_order_view(corr))

        with repo.transaction():  # duplicate retry in a later txn
            iid2 = repo.insert_intent(_intent_row())
            sink.current_intent_id = iid2
            try:
                # fresh broker id forces the insert path -> UNIQUE(idempotency_key)
                sink.on_order(_order_view(corr, broker_order_id="brd-retry"))
            except sqlite3.IntegrityError:
                repo.update_intent_decision(iid2, "NOOP",
                                            rejection_reason="DUPLICATE_ATTEMPT")
        assert _counts(conn, "orders") == 1
        decisions = {r["decision"] for r in conn.execute(
            "SELECT decision FROM intents").fetchall()}
        assert decisions == {"ENTER", "NOOP"}


# --------------------------------------------------------------------- Case E
class TestCaseECrossSession:
    def test_rollback_in_a_preserves_b_committed_rows(self, tmp_path):
        path = str(tmp_path / "cross.db")
        conn_a = init_db(path)
        conn_b = init_db(path)
        try:
            for sid in (SID, SID_B):
                conn_a.execute("INSERT INTO sessions(id, name, status) VALUES(?,?,?)",
                               (sid, sid, "RUNNING"))
            conn_a.commit()
            repo_a = TradingRepo(conn_a, SID)
            repo_b = TradingRepo(conn_b, SID_B)

            # B commits independently FIRST (its own connection)
            repo_b.insert_intent({"ts": TS.isoformat(), "symbol": "TCS",
                                  "market_state_ref": "{}",
                                  "feature_vector_json": "{}",
                                  "signals_json": "[]", "risk_checks_json": "[]",
                                  "decision": "REJECT",
                                  "rejection_reason": "MARKET_CLOSED",
                                  "portfolio_snapshot_json": "{}",
                                  "versions_json": "{}"})
            base_b = _counts(conn_b, "intents", SID_B)

            # A opens a transaction and crashes it
            with pytest.raises(RuntimeError, match="a-crash"):
                with repo_a.transaction():
                    repo_a.insert_intent(_intent_row())
                    raise RuntimeError("a-crash")

            assert _counts(conn_a, "intents", SID) == 0          # A rolled back
            assert _counts(conn_b, "intents", SID_B) == base_b   # B untouched
        finally:
            conn_a.close()
            conn_b.close()


# --------------------------------------------------------------------- Case F
class TestCaseFNestedTransactions:
    def test_inner_commit_suppressed_exactly_once_outer(self, conn):
        class CommitCountingConn:
            """Delegating proxy counting COMMIT/ROLLBACK statements."""

            def __init__(self, inner):
                self._inner = inner
                self.commits = 0
                self.rollbacks = 0

            def execute(self, *a, **k):
                return self._inner.execute(*a, **k)

            def commit(self):
                self.commits += 1
                self._inner.commit()

            def rollback(self):
                self.rollbacks += 1
                self._inner.rollback()

        proxy = CommitCountingConn(conn)
        repo = TradingRepo(proxy, SID)

        with repo.transaction():                 # outermost
            repo.insert_intent(_intent_row())
            with repo.transaction():             # nested no-op
                repo.insert_intent(_intent_row(decision="DEFERRED"))
                with repo.transaction():         # doubly nested no-op
                    repo.insert_intent(_intent_row(decision="REJECT"))
            assert proxy.commits == 0            # nothing committed mid-way
        assert proxy.commits == 1                # exactly once at OUTER exit
        assert proxy.rollbacks == 0
        assert _counts(conn, "intents") == 3

        # failure path: inner exception rolls back everything exactly once
        proxy2 = CommitCountingConn(conn)
        repo2 = TradingRepo(proxy2, SID)
        with pytest.raises(ValueError):
            with repo2.transaction():
                repo2.insert_intent(_intent_row())
                with repo2.transaction():
                    raise ValueError("inner blowup")
        assert proxy2.commits == 0
        assert proxy2.rollbacks == 1             # single ROLLBACK at outermost
        assert _counts(conn, "intents") == 3     # unchanged

    def test_depth_counter_cleans_up_after_exit(self, conn, repo):
        from sts.storage.repos import _txn_active
        with repo.transaction():
            assert _txn_active(conn)
        assert not _txn_active(conn)
        # top-level writes commit immediately again after the txn closes
        repo.insert_intent(_intent_row())
        assert _counts(conn, "intents") == 1
