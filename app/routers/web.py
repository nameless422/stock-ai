from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import settings
from app.services.market_service import get_quote_bundle_async


router = APIRouter()
INITIAL_QUOTE_PLACEHOLDER = "__INITIAL_QUOTE_JSON__"


@lru_cache(maxsize=16)
def _read_template(name: str) -> str:
    path = Path(settings.base_dir) / "templates" / name
    return path.read_text(encoding="utf-8")


def _serialize_for_script(data: object) -> str:
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    if not (request.query_params.get("code") or "").strip():
        return assistant_page()
    return await stock_page(request)


@router.get("/stock", response_class=HTMLResponse)
async def stock_page(request: Request) -> HTMLResponse:
    initial_quote = await get_quote_bundle_async("000001", "daily", "qfq", request.app.state.market_http_client)
    html = _read_template("index.html").replace(INITIAL_QUOTE_PLACEHOLDER, _serialize_for_script(initial_quote))
    return HTMLResponse(content=html, status_code=200)


@router.get("/screener", response_class=HTMLResponse)
def screener_page() -> HTMLResponse:
    return HTMLResponse(content=_read_template("screener.html"), status_code=200)


@router.get("/strategies", response_class=HTMLResponse)
def strategies_page() -> HTMLResponse:
    return HTMLResponse(content=_read_template("strategies.html"), status_code=200)


@router.get("/assistant", response_class=HTMLResponse)
def assistant_page() -> HTMLResponse:
    return HTMLResponse(content=_read_template("assistant.html"), status_code=200)
