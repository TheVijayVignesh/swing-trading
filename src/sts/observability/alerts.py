"""Alerts: every alert is logged; optionally pushed to Telegram when the env
vars STS_TELEGRAM_BOT_TOKEN and STS_TELEGRAM_CHAT_ID are both set. Network
failures are swallowed (alerting must never take down the lab).

Alert-worthy events (normative): DRAWDOWN_KILL, feed stale > 10 min,
session FAULTED, recovery completed.
"""
from __future__ import annotations

import os
from typing import Any

import requests

from sts.observability.logs import get_logger

log = get_logger("sts.alerts")

TELEGRAM_TIMEOUT_S = 5.0


def _telegram_enabled() -> bool:
    return bool(os.environ.get("STS_TELEGRAM_BOT_TOKEN") and os.environ.get("STS_TELEGRAM_CHAT_ID"))


def _send_telegram(text: str) -> None:
    token = os.environ["STS_TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["STS_TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=TELEGRAM_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — best effort only
        log.warning("telegram alert failed: %s", exc)


def alert(kind: str, message: str, *, severity: str = "WARN", detail: dict[str, Any] | None = None) -> None:
    """Emit an alert: always logged (JSON), optionally sent to Telegram."""
    line = f"[{severity}] {kind}: {message}"
    if detail:
        line += f" :: {detail}"
    (log.error if severity in ("ERROR", "CRIT") else log.warning)(
        "alert", extra={"alert_kind": kind, "alert_severity": severity, "detail": detail or {}}
    )
    if _telegram_enabled():
        _send_telegram(line)


def incident(kind: str, session_id: str | None, detail: dict | None = None,
             repo=None, severity: str = "WARN") -> None:
    """Journal an incident row (when a repo is supplied) AND raise an alert."""
    alert(kind, kind.replace("_", " ").lower(), severity=severity, detail={"session": session_id, **(detail or {})})
    if repo is not None:
        try:
            repo.record_incident(severity=severity, kind=kind, detail=detail or {})
        except Exception as exc:  # noqa: BLE001
            log.error("incident journaling failed: %s", exc)
