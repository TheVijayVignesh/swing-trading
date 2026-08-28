"""FastAPI app factory: create_app(lab_manager, marketdata, conn)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from sts.api import routes_api, routes_pages


def _static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


def create_app(lab_manager, marketdata, conn: sqlite3.Connection,
               *, recover_on_startup: bool = True) -> FastAPI:
    app = FastAPI(title="Swing Lab", version="1.0")
    app.state.lab = lab_manager
    app.state.marketdata = marketdata
    app.state.conn = conn
    app.include_router(routes_api.router)
    app.include_router(routes_pages.router)
    from sts.api import routes_lab  # frontend page-support endpoints (ADDENDUM v2)
    app.include_router(routes_lab.router)
    app.mount("/static", StaticFiles(directory=str(_static_dir())), name="static")

    if recover_on_startup:
        @app.on_event("startup")
        async def _recover() -> None:  # pragma: no cover - exercised via tests directly
            recovered = lab_manager.recover_on_boot()
            if recovered:
                import logging
                logging.getLogger("sts.lab").info(
                    "boot recovery done", extra={"recovered": recovered})

    return app
