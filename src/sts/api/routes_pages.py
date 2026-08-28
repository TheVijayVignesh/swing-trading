"""Page routes — Jinja templates owned by the frontend agent; this module only
renders the exact template names + documented context. If a template file is
missing, a CLEAR JSON error is returned (tiny fallback, never a blank page).

Detail payloads are enriched with CONTRACT ADDENDUM v2 fields via
sts.api.routes_lab.enrich_detail() (activity, dossier extras, rejections…).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()

_TEMPLATE_DIR_CANDIDATES = (
    Path(__file__).resolve().parent / "templates",
    Path("dashboard/templates"),
    Path(__file__).resolve().parents[3] / "dashboard" / "templates",
)


def _templates() -> tuple[Jinja2Templates | None, Path | None]:
    for d in _TEMPLATE_DIR_CANDIDATES:
        if d.exists():
            return Jinja2Templates(directory=str(d)), d
    return None, None


def _render(request: Request, template_name: str, context: dict):
    tpl, d = _templates()
    if tpl is None or not (d / template_name).exists():
        return JSONResponse(
            status_code=500,
            content={
                "error": f"template {template_name!r} missing",
                "searched": [str(p) for p in _TEMPLATE_DIR_CANDIDATES],
                "hint": "frontend agent owns dashboard/templates/",
            },
        )
    return tpl.TemplateResponse(request=request, name=template_name, context=context)


@router.get("/")
async def lab_overview(request: Request, include_archived: int = 0):
    # Board payload (ADDENDUM v2): archived flags + activity + recent decisions.
    from sts.api.routes_lab import board_payload
    payload = board_payload(request, include_archived=bool(include_archived))
    system = payload["system"]
    # human-readable heartbeat for server-rendered strip (JS re-formats on poll)
    hb = system.get("heartbeat")
    if hb:
        try:
            from datetime import datetime, timezone
            dt_obj = datetime.fromisoformat(hb)
            secs = max(0, int((datetime.now(timezone.utc) - dt_obj).total_seconds()))
            system["heartbeat"] = f"{dt_obj.strftime('%H:%M:%S')} UTC · {secs}s ago"
        except Exception:
            pass
    return _render(request, "lab_overview.html", {
        "sessions": payload["sessions"],
        "best": payload["best"],
        "system": system,
        "recent_decisions": payload["recent_decisions"],
        "show_archived": bool(include_archived),
        "title": "SWING LAB",
    })


@router.get("/sessions/new")
async def session_new(request: Request, clone: str = ""):
    from sts.config import RISK_PROFILES  # noqa: F401 — form documents profiles
    ctx = {
        "universes": ["NIFTY50", "NIFTY200"],
        "strategies": ["pullback-v1", "random-k"],
        "risk_profiles": ["micro", "small", "standard"],
        "stop_policies": ["FLATTEN", "HOLD"],
        "modes": ["paper"],
        "recommended_lineup": [
            {"name": "hybrid-main", "strategy_id": "pullback-v1", "ml_enabled": True},
            {"name": "det-only", "strategy_id": "pullback-v1", "ml_enabled": False},
            {"name": "random-k", "strategy_id": "random-k", "ml_enabled": False},
        ],
        "clone_source": None,
    }
    if clone:
        lab = request.app.state.lab
        row = lab.sessions.get_session(clone)
        if row is not None:
            from sts.api.routes_api import session_detail
            from sts.api.routes_lab import enrich_detail
            detail = await session_detail(clone, request)
            detail = enrich_detail(request, clone, detail)
            cfg = detail.get("config") or {}
            ctx["clone_source"] = {
                "id": clone,
                "name": row["name"],
                "config": cfg,
                "params": cfg.get("params") or {},
                "risk_profile": cfg.get("risk_profile") or "standard",
                "capital_initial": cfg.get("capital_initial"),
                "ml_enabled": bool(row["ml_model_id"]
                                   and row["ml_model_id"] != "deterministic"),
                "on_stop_policy": cfg.get("on_stop_policy") or "FLATTEN",
                "universe": cfg.get("universe") or "NIFTY200",
                "strategy_id": cfg.get("strategy_id") or "pullback-v1",
            }
    return _render(request, "session_new.html", ctx)


@router.get("/sessions/{sid}")
async def session_detail_page(sid: str, request: Request):
    lab = request.app.state.lab
    row = lab.sessions.get_session(sid)
    if row is None:
        raise HTTPException(404, "unknown session")
    # Call the API handler directly (Starlette 1.6 nests routers, so
    # app.routes no longer flattens APIRoute objects — introspection broke).
    from sts.api.routes_api import session_detail
    from sts.api.routes_lab import enrich_detail
    detail = await session_detail(sid, request)
    detail = enrich_detail(request, sid, detail)  # ADDENDUM v2 dossier fields
    return _render(request, "session_detail.html", {"session": detail})


@router.get("/compare")
async def compare_page(request: Request, ids: str = ""):
    payload = {"sessions": []}
    if ids:
        from sts.api.routes_api import compare as lab_compare
        payload = await lab_compare(request, ids=ids)
    return _render(request, "compare.html", {"ids": ids, **payload})
