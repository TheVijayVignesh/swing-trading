"""Logging setup: rotating JSON-ish file log at logs/sts.log + stderr echo."""
from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path

_CONFIGURED = False

_STD_RECORD_ATTRS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
}


class JsonishFormatter(logging.Formatter):
    """One-JSON-object-per-line formatter; extra attrs merged at top level."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key not in _STD_RECORD_ATTRS and not key.startswith("_"):
                try:
                    json.dumps(val)
                    payload[key] = val
                except TypeError:
                    payload[key] = repr(val)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(log_dir: str | Path = "logs", level: int = logging.INFO) -> None:
    """Idempotent global logging config: rotating file + console."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    fmt = JsonishFormatter()

    file_h = logging.handlers.RotatingFileHandler(
        Path(log_dir) / "sts.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    stream_h = logging.StreamHandler()
    stream_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(stream_h)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
