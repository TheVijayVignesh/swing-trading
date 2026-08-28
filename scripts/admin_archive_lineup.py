#!/usr/bin/env python3
"""Admin cleanup (audit v2 work item 5): archive the duplicate `lineup-*`
sessions in data/sqlite/journal.db.

DOES NOT delete any experiment history — soft-archives via
SessionRepo.archive() only. Sessions stuck in RUNNING (no live runner after a
crash/reboot) are first transitioned to STOPPED with explicit journal events,
then archived through the normal path so every state change is auditable.

Usage: uv run python scripts/admin_archive_lineup.py [--db PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sts.storage.db import init_db                      # noqa: E402
from sts.storage.repos import SessionRepo               # noqa: E402


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/sqlite/journal.db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = init_db(args.db)
    repo = SessionRepo(conn)
    targets = [s for s in repo.list_sessions(include_archived=True)
               if s["name"].startswith("lineup-")]

    print(f"db={args.db}")
    print("BEFORE:")
    for s in repo.list_sessions(include_archived=True):
        mark = " <- target" if s["name"].startswith("lineup-") else ""
        print(f"  {s['id'][:12]}  {s['name']:22s} {s['status']}{mark}")

    if not targets:
        print("nothing to do: no lineup-* sessions found")
        return 0

    if args.dry_run:
        print("\ndry-run: no changes written")
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    print("\nactions:")
    for s in targets:
        sid = s["id"]
        if s["status"] == "RUNNING":
            # zombie RUNNING row (no live runner outside the API process):
            # honest STOP first, then archive — both journaled.
            repo.record_event(sid, "STOP_REQUESTED", actor="admin-archive",
                              detail={"reason": "lineup-duplicate-cleanup"})
            conn.execute(
                "UPDATE sessions SET status='STOPPED', ended_at=? WHERE id=?",
                (_now(), sid))
            conn.commit()
            repo.record_event(sid, "STOPPED", actor="admin-archive",
                              detail={"terminal_state": None,
                                      "reason": "lineup-duplicate-cleanup"})
            print(f"  stopped zombie RUNNING session {sid[:12]} ({s['name']})")
        repo.archive(sid, now=now)
        print(f"  archived {sid[:12]} ({s['name']})")

    print("\nAFTER:")
    for s in repo.list_sessions(include_archived=True):
        print(f"  {s['id'][:12]}  {s['name']:22s} {s['status']}"
              f"  archived_at={s['archived_at']}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
